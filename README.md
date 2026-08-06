# Reolink Stamina

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz) [![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.1%2B-41BDF5.svg)](https://www.home-assistant.io) [![Release](https://img.shields.io/github/v/release/marl1w/reolink-stamina-ha?display_name=tag&sort=semver)](https://github.com/marl1w/reolink-stamina-ha/releases) [![Licence: MIT](https://img.shields.io/badge/Licence-MIT-blue.svg)](LICENSE)

*Your NVR has the memory. This has the stamina.*

Reolink hardware records diligently and then makes the footage tedious to get at: one camera at a time, one app or one folder at a time, and nothing that survives the recorder being stolen. **Reolink Stamina closes that gap** — it is about the experience of *using* the Reolink system you already own, from inside Home Assistant.

Two things so far:

- **One timeline across every device.** Every camera's detections in a single list, whatever recorder they hang off, with the clip one click away and the playhead already at the event.
- **An off-site copy of what mattered.** A clip of each detection uploaded to your own cloud storage, event by event, so the evidence outlives the recorder it was written on.

It is a companion to the official [Reolink integration][reolink], not a replacement. Every device, camera and recording comes from it, so there are **no extra credentials to enter**.

**Why "Stamina"?** Because everything else in this chain tires quickly, and a good experience has to outlast it. The recorder answers searches at its leisure, streams playback at walking pace, and drops your connection if you ask twice too fast. So: cached results appear instantly while it keeps asking in the background, clips queue instead of stampeding the recorder, a backlog survives a restart, and it will process the four hundredth cat of the evening without complaint.

### What you need

- Home Assistant 2026.1 or newer, any install type
- The official **Reolink integration**, with at least one **NVR** with a working HDD
- For cloud sync only: an existing **OneDrive** integration — its credentials are reused

Standalone cameras and Home Hubs are filtered out, unless you switch on the [beta that lists them](#beta-options).

### Install through HACS

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=marl1w&repository=reolink-stamina-ha&category=integration)

1. HACS → **⋮** → **Custom repositories** → add `https://github.com/marl1w/reolink-stamina-ha`, category **Integration**.
2. Install **Reolink Stamina**, then restart Home Assistant.

That puts the code in place; see [Configuration](#configuration) to switch it on.

> **Tested against:** Home Assistant 2026.8.4 · Reolink RLN8-410 (N7MB01) on firmware v3.6.5.562_26062933 · cameras B800, RLC-81MA, Duo 2 PoE, Duo 2v PoE. Other NVRs should work; other *models* of recorder are where surprises live, so reports are welcome.

## Features

### The panel

![reolink-stamina-panel.png](img/panel.png)

Pick cameras, then a day or a range. Filter chips toggle each kind of detection — person, vehicle, animal and other alerts are on by default, motion and scheduled recordings are off, because on a 24/7 recorder they outnumber real events many times over.

Rows say what fired and how often ("Person (2)"), the scrub bar marks each detection in the colour of what was detected, and clips open where the event is rather than at the top of a five-minute segment. **Download** saves the clip as MP4 in your choice of resolution.

Worth knowing:

- **Reach is about 30 days**, bounded by the Reolink search API and your HDD. Beyond that even stamina cannot help: the footage is gone.
- **Playback uses the low-resolution stream** where available. High resolution is H.265 at full sensor size — slow to open, undecodable in most browsers — so it is offered for downloads, not for watching. [Adaptive playback (beta)](#beta-options) makes it watchable, and adds a resolution picker to the player.
- **The player says how a clip reached you.** A badge reads *Direct play* when the footage came straight off the recorder with nothing in between, and names the conversion when [adaptive playback](#beta-options) had to step in.
- **High-resolution downloads won't open in QuickTime or Preview.** The file is valid and VLC and browsers play it, but the H.265 these recorders produce stalls Apple's decoder however it is packaged. Convert once if you need to: `ffmpeg -i clip.mp4 -c:v libx264 -crf 20 -c:a copy out.mp4`.
- **Downloads take about as long as the clip** — no fast-forward on the recorder's treadmill.
- **Detection labels, counts and clip trimming need Home Assistant's recorder.** Disabled or purged, rows fall back to whatever the NVR itself reported and clips to whole segments.

### Cloud sync

![reolink-stamina-cloud.png](img/cloud-sync.png)

One cloud sync per NVR, each with its own switch, quota and destination. It watches that recorder's detection sensors and, for each event, fetches a clip and uploads it to OneDrive.

Each sync becomes a device — shown under the NVR itself, the way the Reolink integration's own devices are — with a **switch** for whether new clips are *accepted*, which is what you automate from an alarm, and sensors for quota used and available, clips stored, queued uploads, uploads since restart, last upload and last error.

Clips are named date-first so any file browser lists them chronologically:

```
reolink/Reolink-NVR/240102_153045_recorder_front-door.mp4
```

When the quota is full, the **oldest clips are deleted** until the new one fits.

How it behaves:

- **The switch gates admission, not delivery.** Off stops new clips being accepted; anything queued still uploads. Disarming an alarm shouldn't discard footage of whatever made you disarm it.
- **One event is one clip.** Several sensors fire on one arrival, and people step in and out of frame. A clip runs from the first detection to a margin after the *last* one clears; stepping away and back rejoins the same clip. A sensor stuck on is cut off after ten minutes.
- **One fetch at a time per recorder** — Reolink recorders are sprinters, not marathon runners.
- **Cameras recording on events cost nothing extra:** their recording already *is* the clip, pre-record buffer included, so it goes up as it stands at wire speed.
- **Cameras recording 24/7 are cut down.** Only the clip's bytes are streamed and ffmpeg copies them into MP4 — nothing is re-encoded.
- **Nothing is written to your Home Assistant machine**; clips stream through memory to the cloud.
- **Clips land in Home Assistant's OneDrive app folder** — the access that integration holds, and what avoids a second app registration.

## Configuration

### Turning on the panel

**Settings → Devices & services → Add integration → Reolink Stamina.**

Setup asks nothing: it discovers your NVRs through the Reolink integration and adds itself to the sidebar. Once an NVR is set up there, Home Assistant also offers Stamina by itself — it watches the network for Reolink recorders exactly as the official integration does, and the panel turns up as a **Discovered** card.

Then *Reolink Stamina → **Configure*** for how the panel searches and presents recordings:

| Option | Default | What it is for |
| --- | --- | --- |
| Stream used for browsing | Low resolution | Low searches faster; playback and downloads can still use high |
| Event segment length | 5 min | How finely continuous recordings split into rows — the biggest change to how the list looks. 0 lists one row per recording file |
| Estimated pre-roll | 5 s | Where to draw the trigger marker for cameras that don't report their own pre-record time |
| Start playback before the detection | 30 s | Where the playhead opens inside the clip. Only the playhead — it does not change the clip |
| Clip: seconds before / after | 15 s / 15 s | Where a trimmed clip starts and ends on 24/7 footage |
| Hide scheduled recordings | on | Which filters start enabled. Off also brings back scheduled and unlabelled footage |
| Restrict to administrators | on | Recordings can be sensitive. Off lets non-admins open the panel |
| Adaptive playback (beta) | off | Convert a recording server-side when the browser cannot play it — see below |
| Show hubs and standalone cameras (beta) | off | List devices other than recorders — see below |

#### Beta options

Two things this has been asked for repeatedly, both off by default. With both off, nothing above them behaves differently and no code path they add is reached.

**Adaptive playback**

Playback normally goes straight from the recorder to the browser with nothing in between, and that stays the first thing tried. Some viewers for different reason cannot use it, and none of them says so; each one just shows a black window.

So the player works down a ladder and stops at the first rung that draws a frame:

1. **The recorder's own stream**, demuxed in the browser. No server work at all.
2. **Repackaged** — ffmpeg changes the container and nothing else. This is what an iPhone needs for H.264: the phone's own decoder still does the work. Cheap on any machine.
3. **Re-encoded** — only for a codec the device itself cannot decode. Uses the machine's GPU where there is one (VideoToolbox, QSV, VAAPI, NVENC, Rockchip, Pi), falls back to software. Capped at 1080p, because re-encoding 8 megapixels in real time is beyond any machine Home Assistant usually runs on.

**Hubs and standalone cameras**

Lists Home Hubs and cameras with their own SD card alongside your recorders, tagged as the untested quantity they are. They answer the same search API, so the panel can browse them — but no hardware here has one, which is the whole reason this is a beta and reports are useful.

A camera that is already a channel on one of your NVRs is left out even so: it would be the same footage listed twice, under two names. Cloud sync remains NVR-only atm.

### Turning on cloud sync

*Reolink Stamina → **Add cloud sync***. Add one for each NVR you want backed up.

| Setting | Default | What it is for |
| --- | --- | --- |
| Recorder | First NVR not yet synced | Clips are uploaded for every camera on this NVR. Recorders that already have a sync are not offered again |
| Cloud account | — | An existing OneDrive integration; nothing to authorise again |
| Storage quota | 15 GB | How much this recorder's clips may occupy before the oldest are evicted |
| Detections to upload | person, vehicle, animal | Motion is offered, but fires constantly on most cameras and would spend the quota on empty footage |
| Resolution | Low | A fraction of the size, and plays everywhere |
| Seconds before / after | 10 s / 10 s | How much footage to keep either side of the event |
| Folder name | `reolink/<NVR name>` | Where the clips go; may be several levels deep |

Which recorder a sync serves is fixed once created — it owns the clips it has already uploaded, and re-pointing it would strand them where nothing will ever evict them. Everything else is editable under **Configure**.

## Troubleshooting

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.reolink_stamina: debug
    reolink_aio: debug
```

| Symptom | Likely cause |
| --- | --- |
| No sidebar item | Integration not added, or you're a non-admin and *Restrict to administrators* is on |
| "No Reolink NVR found" | No NVR in the Reolink integration, or it's a standalone camera / Home Hub — switch on the beta for those |
| Black window, or "this browser cannot decode this recording" | The stream is H.265 and this is Chrome or Firefox. Switch on *Adaptive playback* |
| Black window in Safari at high resolution | Same cause, despite Safari claiming H.265 support: Apple's decoder stalls on what these recorders produce. With *Adaptive playback* on it re-encodes instead, after a few seconds of finding out |
| Nothing plays in the iPhone app | Same option: iOS cannot read the recorder's stream at all, and needs the repackaged one |
| One camera is black on the phone while the others play | Its stream needs more than repackaging. With the beta on it lands on the re-encode rung by itself and stays there; the log says what ffmpeg made of it under `custom_components.reolink_stamina` at debug |
| "Adaptive playback needs ffmpeg" | Install ffmpeg, or leave the option off |
| Playback is slow and the player says "Re-encoded" | The machine is converting in software. Expected on a Pi at high resolution; low resolution is far cheaper |
| "This clip cannot be played in this browser" | Every route was tried and none drew a frame. **Download this clip** beside the message writes it out as MP4 to watch locally, and the other resolution is often worth a try |
| A camera on an NVR is missing from the beta list | Deliberate: it is listed under its recorder instead, so the same footage does not appear twice |
| An NVR is greyed out | Its Reolink config entry isn't loaded — the card says why |
| Rows read "Recording" with no detection type | Neither Home Assistant's sensors nor the NVR classified them: check the recorder is enabled and hasn't purged that far back |
| Cloud sync has no entities | Its recorder is gone from the Reolink integration, or the chosen cloud account was removed — the log says which |
| Clips queue but never upload | Check *Last error*; a revoked cloud login surfaces as a reauth prompt on that integration |
| "no ffmpeg is installed" | A 24/7 camera's clip must be cut; install ffmpeg or sync event-recording cameras only |
| Panel looks stale after an update | Clear your browser cache, then hard-refresh |
| Everything is slow | It's the NVR. It's always the NVR |

## Contributing

Fork, open a pull request with a description of your changes and the use case you are addressing, follow the existing style, include tests.

## Licence

[MIT](LICENSE) · [Reolink integration][reolink]

[reolink]: https://www.home-assistant.io/integrations/reolink/
