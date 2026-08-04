/**
 * Tests for the FLV → MP4 clip writer, run under node.
 *
 * Binary format code is exactly where quiet mistakes live — a clip that plays for the
 * first second, a file whose audio drifts, a 1080p stream written as 1088 lines. So these
 * do not assert on internals: ffmpeg builds real FLV files, the panel's own code remuxes
 * them, and ffprobe reports what actually came out.
 *
 * Run via scripts/check.sh, which skips it when ffmpeg or node is unavailable.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  aacConfig,
  avcDimensions,
  FlvDemuxer,
  readAudioTag,
  readVideoTag,
  remuxFlvToMp4,
} from "../../custom_components/reolink_stamina/frontend/clip.js";

const work = mkdtempSync(join(tmpdir(), "reolink-clip-"));
let failures = 0;
let ran = 0;

function test(name, fn) {
  ran += 1;
  try {
    fn();
    process.stdout.write(`  ok ${name}\n`);
  } catch (error) {
    failures += 1;
    process.stdout.write(`  FAILED ${name}\n    ${error.message}\n`);
  }
}

async function asyncTest(name, fn) {
  ran += 1;
  try {
    await fn();
    process.stdout.write(`  ok ${name}\n`);
  } catch (error) {
    failures += 1;
    process.stdout.write(`  FAILED ${name}\n    ${error.message}\n`);
  }
}

/** Build an FLV the way the recorder would: H.264 video, optionally with AAC audio. */
function makeFlv({ name, seconds, size = "320x240", rate = 15, audio = true }) {
  const path = join(work, name);
  const args = [
    "-hide_banner",
    "-loglevel", "error",
    "-y",
    "-f", "lavfi",
    "-i", `testsrc2=size=${size}:rate=${rate}`,
  ];
  if (audio) args.push("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100");
  args.push(
    "-t", String(seconds),
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    "-g", "15",
    ...(audio ? ["-c:a", "aac", "-b:a", "64k"] : ["-an"]),
    "-f", "flv",
    path
  );
  execFileSync("ffmpeg", args);
  return new Uint8Array(readFileSync(path));
}

/** What ffprobe says about a file we wrote. */
function probe(bytes, name) {
  const path = join(work, name);
  writeFileSync(path, bytes);
  const out = execFileSync("ffprobe", [
    "-hide_banner",
    "-loglevel", "error",
    "-print_format", "json",
    "-show_format",
    "-show_streams",
    path,
  ]);
  return JSON.parse(out.toString());
}

/**
 * Reassemble a stream from tags, so awkward orderings can be built on purpose.
 *
 * `flags` is the header's promise about what the file contains — 0x01 video, 0x04 audio —
 * which recorders do not always tell the truth about.
 */
function rebuildFlv(tags, flags) {
  const parts = [Uint8Array.from([0x46, 0x4c, 0x56, 1, flags, 0, 0, 0, 9, 0, 0, 0, 0])];
  for (const tag of tags) {
    const header = new Uint8Array(11);
    header[0] = tag.type;
    header[1] = (tag.data.length >> 16) & 0xff;
    header[2] = (tag.data.length >> 8) & 0xff;
    header[3] = tag.data.length & 0xff;
    header[4] = (tag.timestamp >> 16) & 0xff;
    header[5] = (tag.timestamp >> 8) & 0xff;
    header[6] = tag.timestamp & 0xff;
    header[7] = (tag.timestamp >>> 24) & 0xff;
    const size = 11 + tag.data.length;
    const trailer = Uint8Array.from([
      (size >> 24) & 0xff,
      (size >> 16) & 0xff,
      (size >> 8) & 0xff,
      size & 0xff,
    ]);
    parts.push(header, tag.data, trailer);
  }
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** Feed bytes in awkwardly sized pieces, the way a network hands them over. */
async function* inChunks(bytes, size) {
  for (let at = 0; at < bytes.length; at += size) {
    yield bytes.subarray(at, Math.min(bytes.length, at + size));
  }
}

// ------------------------------------------------------------------- unit level

const flv = makeFlv({ name: "source.flv", seconds: 4 });

test("the demuxer reads whole tags out of arbitrary chunk boundaries", () => {
  const whole = new FlvDemuxer().push(flv);
  // One byte at a time is the most hostile split there is.
  const drip = new FlvDemuxer();
  const dripped = [];
  for (let at = 0; at < flv.length; at += 1) {
    dripped.push(...drip.push(flv.subarray(at, at + 1)));
  }
  assert.equal(dripped.length, whole.length, "tag count must not depend on chunking");
  assert.deepEqual(
    dripped.map((tag) => [tag.type, tag.timestamp, tag.data.length]),
    whole.map((tag) => [tag.type, tag.timestamp, tag.data.length])
  );
});

test("timestamps are read as milliseconds and rise", () => {
  const tags = new FlvDemuxer().push(flv).filter((tag) => tag.type === 9);
  assert.ok(tags.length > 30, `expected many video tags, got ${tags.length}`);
  assert.equal(tags[0].timestamp, 0);
  const last = tags[tags.length - 1].timestamp;
  assert.ok(last > 3500 && last < 4200, `last video tag at ${last}ms, expected ~4000`);
});

test("the video dimensions come out of the SPS", () => {
  const config = new FlvDemuxer()
    .push(flv)
    .filter((tag) => tag.type === 9)
    .map((tag) => readVideoTag(tag.data))
    .find((video) => video && video.config);
  assert.ok(config, "no AVC configuration record in the stream");
  assert.deepEqual(avcDimensions(config.payload), { width: 320, height: 240 });
});

test("an odd frame size is not rounded up to whole macroblocks", () => {
  // 1080p is coded as 1088 lines with a crop; reporting 1088 writes a black band.
  const odd = makeFlv({ name: "odd.flv", seconds: 1, size: "354x1010" });
  const config = new FlvDemuxer()
    .push(odd)
    .filter((tag) => tag.type === 9)
    .map((tag) => readVideoTag(tag.data))
    .find((video) => video && video.config);
  assert.deepEqual(avcDimensions(config.payload), { width: 354, height: 1010 });
});

test("the audio format comes out of the AudioSpecificConfig", () => {
  const config = new FlvDemuxer()
    .push(flv)
    .filter((tag) => tag.type === 8)
    .map((tag) => readAudioTag(tag.data))
    .find((audio) => audio && audio.config);
  assert.ok(config, "no AAC configuration record in the stream");
  assert.deepEqual(aacConfig(config.payload), { sampleRate: 44100, numberOfChannels: 1 });
});

// ------------------------------------------------------------ end to end, probed

await asyncTest("a remuxed clip is a valid MP4 with both tracks intact", async () => {
  const { buffer } = await remuxFlvToMp4(inChunks(flv, 4096), { seconds: 60 });
  const info = probe(new Uint8Array(buffer), "whole.mp4");

  assert.equal(info.format.format_name.includes("mp4"), true, info.format.format_name);
  const video = info.streams.find((stream) => stream.codec_type === "video");
  const audio = info.streams.find((stream) => stream.codec_type === "audio");
  assert.ok(video, "no video stream in the output");
  assert.equal(video.codec_name, "h264");
  assert.equal(video.width, 320);
  assert.equal(video.height, 240);
  assert.ok(audio, "no audio stream in the output");
  assert.equal(audio.codec_name, "aac");
  assert.equal(Number(audio.sample_rate), 44100);

  const duration = Number(info.format.duration);
  assert.ok(duration > 3.5 && duration < 4.5, `duration ${duration}s, expected ~4s`);
});

await asyncTest("the clip stops at the requested length", async () => {
  const { buffer } = await remuxFlvToMp4(inChunks(flv, 4096), { seconds: 1.5 });
  const info = probe(new Uint8Array(buffer), "trimmed.mp4");
  const duration = Number(info.format.duration);
  assert.ok(duration > 1.2 && duration < 2.0, `duration ${duration}s, expected ~1.5s`);
  // A clip shorter than the wait for an audio track must still come out with its sound.
  assert.ok(
    info.streams.find((stream) => stream.codec_type === "audio"),
    "trimming lost the audio track"
  );
});

await asyncTest("video and audio stay in step", async () => {
  // The failure this guards against is a whole-clip audio shift, which is what happens if
  // each track is rebased on its own first timestamp, or if the leading gap of a track that
  // starts late is silently dropped. Track start times cannot show it — MP4 has no edit list
  // here, so both tracks necessarily begin at zero — so this compares where audio sits
  // relative to video *part way in*, against the same measurement on the source.
  const { buffer } = await remuxFlvToMp4(inChunks(flv, 1024), { seconds: 60 });
  const out = join(work, "sync.mp4");
  writeFileSync(out, new Uint8Array(buffer));

  const offsetIn = (path) => {
    const raw = execFileSync("ffprobe", [
      "-hide_banner",
      "-loglevel", "error",
      "-print_format", "json",
      "-show_packets",
      "-read_intervals", "%+5",
      path,
    ]);
    const packets = JSON.parse(raw.toString()).packets;
    const at = (kind, index) =>
      packets
        .filter((packet) => packet.codec_type === kind)
        .map((packet) => Number(packet.pts_time))
        .sort((a, b) => a - b)[index];
    return at("audio", 10) - at("video", 10);
  };

  const drift = Math.abs(offsetIn(join(work, "source.flv")) - offsetIn(out));
  // One AAC frame is ~23ms; anything beyond that is a real shift.
  assert.ok(drift < 0.025, `audio moved ${drift}s relative to video`);
});

await asyncTest("audio the header did not promise is still included", async () => {
  // Reolink recorders announce video-only in the FLV header and then send AAC anyway.
  // Trusting the header declared an MP4 with no audio track and then threw "No audio track
  // declared" on the first audio frame, which failed the whole download.
  const lying = rebuildFlv(new FlvDemuxer().push(flv), 0x01);
  const { buffer } = await remuxFlvToMp4(inChunks(lying, 4096), { seconds: 60 });
  const info = probe(new Uint8Array(buffer), "lying-header.mp4");

  const audio = info.streams.find((stream) => stream.codec_type === "audio");
  assert.ok(audio, "audio was dropped because the header did not mention it");
  assert.equal(audio.codec_name, "aac");
});

await asyncTest("audio that turns up too late is dropped, not fatal", async () => {
  // The wait for an audio track cannot be unbounded, so a stream that only reveals its
  // audio after a long run of video goes out silent — but it must still go out.
  const tags = new FlvDemuxer().push(flv);
  const video = tags.filter((tag) => tag.type !== 8);
  const audio = tags.filter((tag) => tag.type === 8);
  const late = rebuildFlv([...video, ...audio], 0x05);

  const { buffer } = await remuxFlvToMp4(inChunks(late, 4096), { seconds: 60 });
  const info = probe(new Uint8Array(buffer), "late-audio.mp4");
  assert.ok(info.streams.find((stream) => stream.codec_type === "video"), "video was lost");
});

await asyncTest("a stream with no sound still produces a playable clip", async () => {
  const silent = makeFlv({ name: "silent.flv", seconds: 2, audio: false });
  const { buffer } = await remuxFlvToMp4(inChunks(silent, 4096), { seconds: 60 });
  const info = probe(new Uint8Array(buffer), "silent.mp4");
  assert.ok(info.streams.find((stream) => stream.codec_type === "video"));
  assert.equal(
    info.streams.filter((stream) => stream.codec_type === "audio").length,
    0,
    "expected no audio track"
  );
});

await asyncTest("the byte ceiling is enforced rather than filling memory", async () => {
  await assert.rejects(
    () => remuxFlvToMp4(inChunks(flv, 4096), { seconds: 60, maxBytes: 8192 }),
    /larger than the download limit/
  );
});

await asyncTest("something that is not FLV is refused", async () => {
  const junk = new Uint8Array(64).fill(7);
  await assert.rejects(() => remuxFlvToMp4(inChunks(junk, 16), { seconds: 5 }), /FLV/);
});

process.stdout.write(`\n  ${ran - failures}/${ran} clip checks passed\n`);
process.exit(failures > 0 ? 1 : 0);
