# Vendored dependency

## mpegts.js 1.7.3

`mpegts.js` — https://github.com/xqq/mpegts.js — Apache-2.0 (the maintained fork of Bilibili's flv.js). Vendored rather than loaded from a CDN so the panel works on an isolated network and cannot break when a CDN does.

It demuxes the FLV stream the recorder serves and feeds it to the browser through Media Source Extensions. This is the same thing the recorder's own web player does, and it is why this integration needs no ffmpeg: the recorder already sends a container the browser can read, so nothing has to be transcoded or segmented server-side.

Replacing it means downloading `dist/mpegts.js` from that release and updating this note.

## mp4-muxer 5.2.2

`mp4-muxer` — https://github.com/Vanilagy/mp4-muxer — MIT. The ESM build (`build/mp4-muxer.mjs`), vendored unmodified for the same reasons as above.

It writes the MP4 file for a clip download. The recorder serves FLV, which no player outside VLC will open, but the video inside it is already H.264 or H.265 — so downloading a clip is a container change, not a re-encode: `clip.js` reads the FLV samples and this writes them into MP4 untouched. Nothing is decoded, nothing loses quality, and no ffmpeg appears on the server.

Two details of its contract are easy to get wrong in a way that still produces a file that opens, and `tests/frontend/test_clip.mjs` exists because of them:

- `addVideoChunkRaw`/`addAudioChunkRaw` take **microseconds**.
- The timestamp is the **presentation** time; the decode time is derived as `timestamp - compositionTimeOffset`. Passing FLV's timestamp (which *is* the decode time) yields negative decode times on any stream with B-frames.

It also writes no edit list, so a track that starts partway into the recording has nowhere to say so and its first sample lands at zero. `clip.js` pays that gap into the first audio frame's duration instead, which keeps every later frame aligned with the video.

Replacing it means downloading `build/mp4-muxer.mjs` from that release and updating this note. Its successor, `mediabunny`, supersedes it upstream but is MPL-2.0, so it is not used here: this project ships under MIT and vendoring keeps that uniform. Nothing is expected from the deprecation either way — the vendored file is frozen, and writing MP4 out of H.264 and AAC is a settled problem.
