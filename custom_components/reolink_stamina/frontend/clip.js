/**
 * Turn the recorder's FLV stream into a downloadable MP4 of exactly one clip.
 *
 * The recorder can only hand over whole recordings — half an hour of 24/7 footage for a
 * twenty-second event — and it refuses byte ranges outright. What it *can* do is start a
 * stream at any offset, which is how playback already seeks. So a clip download is: ask
 * for a stream at the clip's start, read until the clip's length has passed, stop.
 *
 * The container is the only thing that changes. FLV carries H.264 or H.265 and AAC, all
 * of which MP4 holds natively, so every sample is copied across byte for byte: nothing is
 * decoded, nothing is re-encoded, and the download is pixel-identical to what the recorder
 * stored. That is also why this belongs in the browser — the alternative was ffmpeg on the
 * Home Assistant host, which this integration deliberately does without.
 *
 * Everything here is pure apart from `remuxFlvToMp4`, which only needs an async iterable of
 * byte chunks, so the parsing can be tested against real ffmpeg-generated files.
 */

import { ArrayBufferTarget, Muxer } from "./vendor/mp4-muxer.mjs";

/** FLV tag types. */
const TAG_AUDIO = 8;
const TAG_VIDEO = 9;
const TAG_SCRIPT = 18;

/** FLV codec ids, from the legacy header nibble and the Enhanced RTMP extension. */
const VIDEO_AVC = 7;
const VIDEO_HEVC = 12;
const AUDIO_AAC = 10;

/** Sampling frequencies an AudioSpecificConfig can name, by index. */
const AAC_SAMPLE_RATES = [
  96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350,
];

/** A frame rate to fall back on when a stream's last sample has no successor to measure. */
const ASSUMED_FRAME_RATE = 15;

/** Samples an AAC frame always covers, for the same reason. */
const AAC_FRAME_SAMPLES = 1024;

/**
 * Video frames to hold while waiting to learn whether a stream has audio.
 *
 * In practice both codecs announce themselves in the first handful of tags, so this only
 * bounds the pathological case. Around four seconds of a 15fps stream.
 */
const MAX_QUEUED = 60;

/**
 * The muxer counts in microseconds, and takes *presentation* times.
 *
 * Both are easy to get wrong in the same direction and produce a file that still opens:
 * it derives each sample's decode time as `timestamp - compositionTimeOffset`, so passing
 * FLV's decode timestamp there yields negative decode times on any stream with B-frames.
 */
const US = 1000; // FLV counts milliseconds

// --------------------------------------------------------------------------- bits

/** Read unsigned Exp-Golomb coded numbers out of an H.264 bitstream. */
class BitReader {
  constructor(bytes) {
    this.bytes = bytes;
    this.bit = 0;
  }

  read(count) {
    let value = 0;
    for (let index = 0; index < count; index += 1) {
      const byte = this.bytes[this.bit >> 3];
      if (byte === undefined) throw new Error("H.264 parameter set ended early");
      value = (value << 1) | ((byte >> (7 - (this.bit & 7))) & 1);
      this.bit += 1;
    }
    return value;
  }

  /** Unsigned Exp-Golomb. */
  ue() {
    let zeros = 0;
    while (this.read(1) === 0) {
      zeros += 1;
      if (zeros > 31) throw new Error("H.264 parameter set is malformed");
    }
    if (zeros === 0) return 0;
    return (1 << zeros) - 1 + this.read(zeros);
  }

  /** Signed Exp-Golomb. */
  se() {
    const value = this.ue();
    return value & 1 ? (value + 1) >> 1 : -(value >> 1);
  }
}

/** Undo the emulation prevention bytes an SPS is stored with. */
function unescapeNalu(bytes) {
  const out = [];
  for (let index = 0; index < bytes.length; index += 1) {
    if (index >= 2 && bytes[index] === 3 && bytes[index - 1] === 0 && bytes[index - 2] === 0) {
      continue;
    }
    out.push(bytes[index]);
  }
  return Uint8Array.from(out);
}

/**
 * The coded size of an H.264 stream, from the SPS inside its avcC record.
 *
 * MP4 needs the dimensions up front and FLV does not have to state them anywhere else, so
 * they are read from the parameter set the stream carries. Cropping matters: a 1080p
 * stream is coded as 1088 lines and says "crop the last 8", and getting that wrong writes
 * a file that plays with a black band.
 */
export function avcDimensions(avcC) {
  // avcC: [1] version, [1] profile, [1] compat, [1] level, [1] lengthSizeMinusOne,
  //       [1] numSPS, then per SPS: [2] length, [length] data
  if (avcC.length < 8) throw new Error("avcC record is too short");
  const spsCount = avcC[5] & 0x1f;
  if (spsCount < 1) throw new Error("avcC record holds no SPS");
  const spsLength = (avcC[6] << 8) | avcC[7];
  const sps = avcC.subarray(8, 8 + spsLength);
  if (sps.length < spsLength || spsLength < 4) throw new Error("avcC record holds no SPS");

  // Skip the NAL header byte, then undo emulation prevention.
  const reader = new BitReader(unescapeNalu(sps.subarray(1)));

  const profileIdc = reader.read(8);
  reader.read(8); // constraint flags + reserved
  reader.read(8); // level_idc
  reader.ue(); // seq_parameter_set_id

  let chromaFormatIdc = 1;
  if (profileIdc === 100 || profileIdc === 110 || profileIdc === 122 || profileIdc === 244) {
    chromaFormatIdc = reader.ue();
    if (chromaFormatIdc === 3) reader.read(1); // separate_colour_plane_flag
    reader.ue(); // bit_depth_luma_minus8
    reader.ue(); // bit_depth_chroma_minus8
    reader.read(1); // qpprime_y_zero_transform_bypass_flag
    if (reader.read(1) === 1) {
      // seq_scaling_matrix_present_flag
      const lists = chromaFormatIdc !== 3 ? 8 : 12;
      for (let list = 0; list < lists; list += 1) {
        if (reader.read(1) === 1) {
          let last = 8;
          let next = 8;
          const size = list < 6 ? 16 : 64;
          for (let index = 0; index < size; index += 1) {
            if (next !== 0) next = (last + reader.se() + 256) % 256;
            if (next !== 0) last = next;
          }
        }
      }
    }
  }

  reader.ue(); // log2_max_frame_num_minus4
  const pocType = reader.ue();
  if (pocType === 0) {
    reader.ue(); // log2_max_pic_order_cnt_lsb_minus4
  } else if (pocType === 1) {
    reader.read(1); // delta_pic_order_always_zero_flag
    reader.se(); // offset_for_non_ref_pic
    reader.se(); // offset_for_top_to_bottom_field
    const cycle = reader.ue();
    for (let index = 0; index < cycle; index += 1) reader.se();
  }

  reader.ue(); // max_num_ref_frames
  reader.read(1); // gaps_in_frame_num_value_allowed_flag
  const widthInMbs = reader.ue() + 1;
  const heightInMapUnits = reader.ue() + 1;
  const frameMbsOnly = reader.read(1);
  if (frameMbsOnly === 0) reader.read(1); // mb_adaptive_frame_field_flag
  reader.read(1); // direct_8x8_inference_flag

  let cropLeft = 0;
  let cropRight = 0;
  let cropTop = 0;
  let cropBottom = 0;
  if (reader.read(1) === 1) {
    // frame_cropping_flag
    cropLeft = reader.ue();
    cropRight = reader.ue();
    cropTop = reader.ue();
    cropBottom = reader.ue();
  }

  // Crop offsets are in chroma samples for 4:2:0/4:2:2, and doubled vertically when the
  // stream is not frame-only coded.
  const subWidth = chromaFormatIdc === 1 || chromaFormatIdc === 2 ? 2 : 1;
  const subHeight = chromaFormatIdc === 1 ? 2 : 1;
  const width = widthInMbs * 16 - (cropLeft + cropRight) * subWidth;
  const height =
    (2 - frameMbsOnly) * heightInMapUnits * 16 -
    (cropTop + cropBottom) * subHeight * (2 - frameMbsOnly);

  if (!(width > 0) || !(height > 0)) throw new Error("H.264 SPS gave no usable size");
  return { width, height };
}

/** Channel count and sample rate from an AudioSpecificConfig. */
export function aacConfig(asc) {
  if (asc.length < 2) throw new Error("AudioSpecificConfig is too short");
  const reader = new BitReader(asc);
  let objectType = reader.read(5);
  if (objectType === 31) objectType = 32 + reader.read(6);
  const rateIndex = reader.read(4);
  const sampleRate = rateIndex === 15 ? reader.read(24) : AAC_SAMPLE_RATES[rateIndex];
  const channels = reader.read(4);
  if (!sampleRate || !channels) throw new Error("AudioSpecificConfig gave no usable format");
  return { sampleRate, numberOfChannels: channels };
}

// --------------------------------------------------------------------------- AMF

/**
 * Width and height out of an FLV `onMetaData` tag, when it has them.
 *
 * The only route to the dimensions of an H.265 stream: its parameter sets are nested
 * inside hvcC behind a variable-length profile structure, and parsing that to learn two
 * numbers the stream usually announces anyway is not a trade worth making.
 */
export function scriptDataDimensions(data) {
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  let at = 0;
  const found = {};

  const readString = (length) => {
    const text = new TextDecoder().decode(data.subarray(at, at + length));
    at += length;
    return text;
  };

  // AMF0: the tag is a string (the event name) followed by its argument.
  const skipValue = () => {
    const type = data[at];
    at += 1;
    if (type === 0) {
      at += 8; // number
    } else if (type === 1) {
      at += 1; // boolean
    } else if (type === 2) {
      const length = view.getUint16(at);
      at += 2 + length;
    } else if (type === 3 || type === 8) {
      if (type === 8) at += 4; // ECMA array count
      readObject();
    } else if (type === 10) {
      const count = view.getUint32(at);
      at += 4;
      for (let index = 0; index < count; index += 1) skipValue();
    } else if (type === 11) {
      at += 10; // date
    } else {
      // null, undefined, unsupported: nothing more can be read reliably.
      at = data.length;
    }
  };

  const readObject = () => {
    while (at + 2 < data.length) {
      const nameLength = view.getUint16(at);
      at += 2;
      if (nameLength === 0) {
        at += 1; // object end marker
        return;
      }
      const name = readString(nameLength);
      if (data[at] === 0) {
        found[name] = view.getFloat64(at + 1);
        at += 9;
      } else {
        skipValue();
      }
    }
  };

  if (data[at] !== 2) return null;
  at += 1;
  const nameLength = view.getUint16(at);
  at += 2;
  readString(nameLength);
  skipValue();

  const width = Math.round(found.width || 0);
  const height = Math.round(found.height || 0);
  return width > 0 && height > 0 ? { width, height } : null;
}

// --------------------------------------------------------------------- demuxing

/**
 * Incremental FLV tag reader.
 *
 * Fed arbitrary chunks as they arrive off the network, it holds back whatever does not yet
 * form a whole tag. Nothing is buffered beyond that: a clip is measured in tens of
 * megabytes and the samples are handed straight to the muxer.
 */
export class FlvDemuxer {
  constructor() {
    this._buffer = new Uint8Array(0);
    this._headerRead = false;
    /**
     * What the header claims the stream contains — informational only.
     *
     * Reolink recorders announce video-only and then send AAC anyway, so nothing may be
     * decided from this: the tags themselves are the only trustworthy account of what a
     * stream holds.
     */
    this.hasAudio = false;
    this.hasVideo = false;
  }

  /** Append bytes, returning every complete tag they made. */
  push(chunk) {
    if (this._buffer.length === 0) {
      this._buffer = chunk;
    } else {
      const merged = new Uint8Array(this._buffer.length + chunk.length);
      merged.set(this._buffer, 0);
      merged.set(chunk, this._buffer.length);
      this._buffer = merged;
    }

    const tags = [];
    let at = 0;
    const bytes = this._buffer;

    if (!this._headerRead) {
      if (bytes.length < 13) return tags;
      if (bytes[0] !== 0x46 || bytes[1] !== 0x4c || bytes[2] !== 0x56) {
        throw new Error("The recorder did not return an FLV stream");
      }
      this.hasAudio = Boolean(bytes[4] & 0x04);
      this.hasVideo = Boolean(bytes[4] & 0x01);
      const headerSize = (bytes[5] << 24) | (bytes[6] << 16) | (bytes[7] << 8) | bytes[8];
      at = headerSize + 4; // header, then PreviousTagSize0
      this._headerRead = true;
    }

    while (at + 11 <= bytes.length) {
      const dataSize = (bytes[at + 1] << 16) | (bytes[at + 2] << 8) | bytes[at + 3];
      const end = at + 11 + dataSize + 4;
      if (end > bytes.length) break;

      // Unsigned 32-bit, with the high byte kept separately for historical reasons.
      const timestamp =
        (bytes[at + 7] * 0x1000000 +
          ((bytes[at + 4] << 16) | (bytes[at + 5] << 8) | bytes[at + 6])) >>>
        0;

      tags.push({
        type: bytes[at],
        timestamp,
        data: bytes.subarray(at + 11, at + 11 + dataSize),
      });
      at = end;
    }

    this._buffer = at > 0 ? bytes.subarray(at) : bytes;
    return tags;
  }
}

/** Split an FLV video tag into what MP4 needs from it. */
export function readVideoTag(data) {
  if (data.length < 1) return null;
  const codecId = data[0] & 0x0f;
  const keyframe = (data[0] >> 4) === 1;
  if (codecId !== VIDEO_AVC && codecId !== VIDEO_HEVC) return null;
  if (data.length < 5) return null;

  const packetType = data[1];
  // Signed 24-bit: the presentation time relative to the decode time.
  let composition = (data[2] << 16) | (data[3] << 8) | data[4];
  if (composition & 0x800000) composition -= 0x1000000;

  return {
    codec: codecId === VIDEO_HEVC ? "hevc" : "avc",
    // 0 announces the decoder configuration; 1 carries samples; 2 ends the stream.
    config: packetType === 0,
    keyframe,
    composition,
    payload: data.subarray(5),
  };
}

/** Split an FLV audio tag into what MP4 needs from it. */
export function readAudioTag(data) {
  if (data.length < 2) return null;
  if ((data[0] >> 4) !== AUDIO_AAC) return null;
  return { config: data[1] === 0, payload: data.subarray(2) };
}

// --------------------------------------------------------------------- remuxing

/**
 * Read an FLV stream and write the first `seconds` of it into an MP4.
 *
 * Samples are emitted one behind, because MP4 wants each one's duration and FLV only says
 * when the next one starts.
 *
 * Audio is dropped rather than fought over: Reolink recorders sometimes serve G.711, which
 * MP4 has no place for, and a silent clip of the right footage beats no clip at all.
 */
export async function remuxFlvToMp4(chunks, { seconds, maxBytes = 0, onProgress } = {}) {
  const demuxer = new FlvDemuxer();
  const limit = Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : Infinity;

  let muxer = null;
  let target = null;
  let videoCodec = null;
  let videoDescription = null;
  let dimensions = null;
  let audioDescription = null;
  let audioFormat = null;
  let base = null;
  let bytes = 0;
  let done = false;
  let lastElapsed = 0;
  let audioUnsupported = false;
  /** Whether the file being written actually has somewhere to put audio. */
  let audioTrack = false;
  /** Set once no more tags are coming, which settles the audio question for good. */
  let ended = false;

  /** Video samples seen before the muxer could be built. Emptied by `start`. */
  const queued = [];

  /**
   * Whether it is still worth waiting to find out if this stream has sound.
   *
   * The header cannot answer it — recorders lie about it — so the answer comes from the
   * tags, and MP4 needs it before the first sample is written. Waiting costs a few hundred
   * kilobytes of queued frames; guessing costs the audio, or the whole download.
   */
  const audioPending = () =>
    !ended && !audioFormat && !audioUnsupported && queued.length < MAX_QUEUED;

  /** The sample held back so its duration can be measured against the next one. */
  let pendingVideo = null;
  let pendingAudio = null;
  let lastVideoDuration = (1 / ASSUMED_FRAME_RATE) * 1e6;
  let lastAudioDuration = 0;
  /** Where the video track begins, which is the origin both tracks are aligned to. */
  let firstVideoDecode = null;
  let audioAligned = false;

  /**
   * Build the muxer once the shape of the file is known.
   *
   * MP4 declares its tracks before its samples, so this has to wait until the audio
   * question is settled: the header said there is none, its configuration has arrived, or
   * it turned out to be a codec MP4 cannot hold. Video samples that arrive during the wait
   * are queued rather than dropped — the first of them is the keyframe the clip opens on.
   */
  const start = () => {
    if (muxer || !videoCodec || !dimensions) return;
    if (audioPending()) return;
    target = new ArrayBufferTarget();
    // Fixed for the life of the file: a track cannot be added to an MP4 after its samples
    // have started, so audio arriving later than this has to be let go.
    audioTrack = Boolean(audioFormat);
    muxer = new Muxer({
      target,
      video: { codec: videoCodec, width: dimensions.width, height: dimensions.height },
      audio: audioTrack ? { codec: "aac", ...audioFormat } : undefined,
      // A stream seeked into the middle of a recording does not start at zero. This shifts
      // both tracks by the same amount; plain "offset" shifts each track by *its own* first
      // timestamp, which silently pulls the audio into sync with the wrong video frame
      // whenever the two do not begin on the same tag.
      firstTimestampBehavior: "cross-track-offset",
      // Progressive rather than fragmented: the whole clip is in hand before anything is
      // written, and a plain MP4 is what the widest range of players will open.
      fastStart: "in-memory",
    });

    const waiting = queued.splice(0, queued.length);
    for (const sample of waiting) emitVideo(sample);
  };

  /** Hand a video sample over, one behind, so its duration can be measured. */
  const emitVideo = (sample) => {
    if (!muxer) {
      queued.push(sample);
      return;
    }
    if (firstVideoDecode === null) firstVideoDecode = sample.decode;
    if (pendingVideo) flushVideo(Math.max(0, (sample.decode - pendingVideo.decode) * US));
    pendingVideo = sample;
  };

  const flushVideo = (duration) => {
    if (!pendingVideo || !muxer) return;
    muxer.addVideoChunkRaw(
      pendingVideo.data,
      pendingVideo.keyframe ? "key" : "delta",
      pendingVideo.presentation,
      duration,
      pendingVideo.meta,
      pendingVideo.composition
    );
    lastVideoDuration = duration;
    pendingVideo = null;
  };

  const flushAudio = (duration) => {
    if (!pendingAudio || !muxer || !audioTrack) return;

    // An MP4 track's timeline is the sum of its sample durations, and this muxer writes no
    // edit list — so a track that starts late in the recording has nowhere to say so, and
    // its first sample lands at zero. Left alone, audio that began 100ms after the video
    // plays 100ms early for the whole clip. Paying that gap into the first frame's duration
    // puts every later frame back where it belongs; only that one frame is early.
    let padded = duration;
    if (!audioAligned) {
      audioAligned = true;
      if (firstVideoDecode !== null && pendingAudio.decode > firstVideoDecode) {
        padded += (pendingAudio.decode - firstVideoDecode) * US;
      }
    }

    muxer.addAudioChunkRaw(
      pendingAudio.data,
      "key",
      pendingAudio.presentation,
      padded,
      pendingAudio.meta
    );
    lastAudioDuration = duration;
    pendingAudio = null;
  };

  for await (const chunk of chunks) {
    if (done) break;
    bytes += chunk.length;
    if (maxBytes && bytes > maxBytes) {
      throw new Error("This clip is larger than the download limit");
    }

    for (const tag of demuxer.push(chunk)) {
      if (base === null && (tag.type === TAG_VIDEO || tag.type === TAG_AUDIO)) {
        base = tag.timestamp;
      }
      const elapsed = base === null ? 0 : tag.timestamp - base;
      if (elapsed > limit) {
        done = true;
        break;
      }

      if (tag.type === TAG_SCRIPT) {
        if (!dimensions) {
          try {
            dimensions = scriptDataDimensions(tag.data);
          } catch {
            dimensions = null;
          }
        }
        continue;
      }

      if (tag.type === TAG_VIDEO) {
        const video = readVideoTag(tag.data);
        if (!video) continue;
        if (video.config) {
          videoCodec = video.codec;
          videoDescription = video.payload.slice();
          if (video.codec === "avc") {
            // Authoritative, and independent of whether the stream announced itself.
            dimensions = avcDimensions(videoDescription);
          }
          start();
          continue;
        }
        if (!videoCodec) continue; // samples before their own configuration are unusable
        // MP4 takes the same length-prefixed NALUs FLV already carries.
        emitVideo({
          data: video.payload.slice(),
          keyframe: video.keyframe,
          // Durations are decode-to-decode, which is what FLV's timestamps measure.
          decode: tag.timestamp,
          // Absolute, and normalised by the muxer against the earlier of the two tracks.
          presentation: (tag.timestamp + video.composition) * US,
          composition: video.composition * US,
          meta: { decoderConfig: { codec: videoCodec, description: videoDescription } },
        });
        start();
        lastElapsed = elapsed;
      } else if (tag.type === TAG_AUDIO) {
        const audio = readAudioTag(tag.data);
        if (!audio) {
          // Reolink recorders sometimes serve G.711, which MP4 has no place for. Stop
          // waiting for a track that can never be written and let the video through.
          audioUnsupported = true;
          start();
          continue;
        }
        if (audio.config) {
          try {
            audioFormat = aacConfig(audio.payload);
            audioDescription = audio.payload.slice();
          } catch {
            // A configuration this panel cannot read is not worth failing a clip over.
            audioUnsupported = true;
          }
          start();
          continue;
        }
        // Sound that arrived after the file's tracks were fixed, or that never announced
        // its format: the clip goes out silent rather than not at all.
        if (!muxer || !audioTrack) continue;
        if (pendingAudio) flushAudio(Math.max(0, (tag.timestamp - pendingAudio.decode) * US));
        pendingAudio = {
          data: audio.payload.slice(),
          decode: tag.timestamp,
          presentation: tag.timestamp * US,
          meta: { decoderConfig: { codec: "mp4a.40.2", description: audioDescription } },
        };
      }
    }

    if (onProgress) onProgress({ bytes, seconds: lastElapsed / 1000 });
  }

  // A clip shorter than the wait for audio ends while still holding its frames back. The
  // stream running out is itself the answer: there was no sound.
  ended = true;
  start();

  if (!muxer) {
    // An H.265 stream states its size only inside the parameter sets nested in its hvcC
    // record, so when the recorder also omits it from the stream's metadata there is no
    // honest width and height to write. Say which case this is rather than "it failed".
    if (videoCodec === "hevc" && !dimensions) {
      throw new Error(
        "This recorder does not report the size of its high-resolution stream, so it cannot" +
          " be packaged. Save the low-resolution clip instead."
      );
    }
    throw new Error("The recorder sent no video this panel can package");
  }

  // The last sample of each track has no successor to measure against.
  flushVideo(lastVideoDuration);
  flushAudio(
    lastAudioDuration ||
      (audioFormat ? (AAC_FRAME_SAMPLES / audioFormat.sampleRate) * 1e6 : 0)
  );
  muxer.finalize();

  return { buffer: target.buffer, bytes };
}

/**
 * A clip as an MP4 blob, read from a signed stream URL.
 *
 * The connection closing is what stops the recorder sending, so abandoning a download
 * costs it nothing — the same contract the playback proxy relies on.
 */
export async function downloadClip(url, { seconds, maxBytes, onProgress, signal } = {}) {
  const response = await fetch(url, { signal });
  if (!response.ok || !response.body) {
    throw new Error(`The device answered HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const contentType = response.headers.get("content-type")?.split(";", 1)[0];
  if (contentType === "video/mp4" || contentType === "application/octet-stream") {
    const chunks = [];
    let bytes = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value) continue;
        bytes += value.byteLength;
        if (maxBytes && bytes > maxBytes) {
          throw new Error("This clip is larger than the download limit");
        }
        chunks.push(value);
      }
    } finally {
      reader.cancel().catch(() => {});
    }
    onProgress?.({ seconds: seconds || 0, bytes });
    return new Blob(chunks, { type: "video/mp4" });
  }

  const chunks = {
    async *[Symbol.asyncIterator]() {
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) return;
          if (value) yield value;
        }
      } finally {
        // Whether the clip finished or the user gave up, the recorder must stop sending.
        reader.cancel().catch(() => {});
      }
    },
  };

  const { buffer } = await remuxFlvToMp4(chunks, { seconds, maxBytes, onProgress });
  return new Blob([buffer], { type: "video/mp4" });
}
