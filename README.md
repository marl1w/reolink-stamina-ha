# Reolink Stamina

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.1%2B-41BDF5.svg)](https://www.home-assistant.io)
[![Release](https://img.shields.io/github/v/release/marl1w/reolink-stamina-ha?display_name=tag&sort=semver)](https://github.com/marl1w/reolink-stamina-ha/releases)
[![Licence: MIT](https://img.shields.io/badge/Licence-MIT-blue.svg)](LICENSE)

*Your NVR has the memory. This has the stamina.*

Two things for a **Reolink NVR** in Home Assistant:

- a **sidebar panel** putting every camera's person, vehicle and animal events on one timeline, one click from the footage;
- a **cloud sync** that uploads a clip of each detection, so the evidence is off-site before anyone reaches the recorder.

A companion to the official [Reolink integration][reolink], not a replacement — every NVR, camera and recording comes from it, so there are **no extra credentials to enter**.

**Why "Stamina"?** Because everything else in this chain tires quickly. The recorder answers searches at its leisure, streams playback at walking pace, and drops your connection if you ask twice too fast. So this is built to outlast it: cached results appear instantly while it keeps asking in the background, clips queue instead of stampeding the NVR, a backlog survives a restart, and it will process the four hundredth cat of the evening without complaint.

## Requirements

- Home Assistant 2026.1 or newer, any install type
- The official **Reolink integration**, with at least one **NVR** with a working HDD
- For cloud sync: an existing **OneDrive** integration — its credentials are reused

Standalone cameras and Home Hubs are out of scope and filtered out.

> **Tested against:** Home Assistant 2026.7.4 · Reolink RLN8-410 (N7MB01) on firmware
> v3.6.5.562_26062933 · cameras B800, RLC-81MA, Duo 2 PoE, Duo 2v PoE.
> Other NVRs should work; other *models* of recorder are where surprises live, so reports are
> welcome.

## Installation

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=marl1w&repository=reolink-stamina-ha&category=integration)

1. HACS → **⋮** → **Custom repositories** → add `https://github.com/marl1w/reolink-stamina-ha`, category **Integration**.
2. Install **Reolink Stamina**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Reolink Stamina.**

Setup asks nothing; it discovers your NVRs through the Reolink integration and adds itself to the sidebar.

Once an NVR is set up in the Reolink integration, Home Assistant also offers Reolink Stamina by itself: it watches the network for Reolink recorders exactly as the official integration does, and the panel turns up as a **Discovered** card on the Devices & services page.

## The panel

Pick cameras, then a day or a range. Filter chips toggle each kind of detection — person, vehicle, animal and other alerts are on by default, motion and scheduled recordings are off, because on a 24/7 recorder they outnumber real events many times over. Rows say what fired and how often ("Person (2)"), the scrub bar marks each detection in the colour of what was detected, and clips open where the event is rather than at the top of a five-minute segment. **Download** saves the clip as MP4 in your choice of resolution.

*Settings → Devices & services → Reolink Stamina → **Configure*** for:

| Option | Default | Effect |
| --- | --- | --- |
| Stream used for browsing | Low res | Low resolution searches faster; playback and downloads can still use high |
| Event segment length | 5 min | How finely continuous recordings split into rows — the biggest change to how the list looks |
| Estimated pre-roll | 5 s | Trigger-marker position for cameras that don't report their own pre-record time |
| Start playback before the detection | 30 s | Where the playhead opens inside the clip |
| Clip: seconds before / after | 15 s / 15 s | Where a trimmed clip starts and ends |
| Hide scheduled recordings | on | Which filters start enabled |
| Restrict to administrators | on | Turn off to let non-admins open the panel |

## Cloud sync

Configured **per NVR**, so each recorder gets its own switch, quota and destination: *Reolink Stamina → **Add cloud sync***. Add one for each NVR you want backed up.

| Setting | Default | Meaning |
| --- | --- | --- |
| Recorder | The first NVR not yet synced | Clips are uploaded for every camera on this NVR. Recorders that already have a cloud sync are not offered again |
| Cloud account | — | An existing OneDrive integration; nothing to authorise again |
| Storage quota | 15 GB | How much this recorder's clips may occupy |
| Detections to upload | person, vehicle, animal | Motion is offered, but fires constantly on most cameras |
| Resolution | Low | A fraction of the size, and plays everywhere |
| Seconds before / after | 10 s / 10 s | Footage kept either side of the event |
| Folder name | `reolink/<NVR name>` | Where the clips go; may be several levels deep |

Which recorder a sync serves is fixed once created — it owns the clips it has uploaded, and re-pointing it would strand them. Everything else is editable under **Configure**.

Each recorder becomes a device — shown under the NVR itself, the way the Reolink integration's own devices are — with a **switch** for whether new clips are *accepted*, which is what you automate from an alarm, and sensors for quota used and available, clips stored, queued uploads, uploads since restart, last upload and last error.

Clips are named date-first so any file browser lists them chronologically:

```
reolink/Reolink-NVR/240102_153045_recorder_front-door.mp4
```

When the quota is full, the **oldest clips are deleted** until the new one fits.

### How it behaves

- **The switch gates admission, not delivery.** Off stops new clips being accepted; anything queued still uploads. Disarming an alarm shouldn't discard footage of whatever made you disarm it.
- **One event is one clip.** Several sensors fire on one arrival, and people step in and out of frame. A clip runs from the first detection to a margin after the *last* one clears; stepping away and back rejoins the same clip. A sensor stuck on is cut off after ten minutes.
- **One fetch at a time per recorder** — Reolink recorders are sprinters, not marathon runners.
- **Cameras recording on events cost nothing extra:** their recording already *is* the clip, pre-record buffer included, so it goes up as it stands at wire speed.
- **Cameras recording 24/7 are cut down.** Only the clip's bytes are streamed and ffmpeg copies them into MP4 — nothing is re-encoded. The only path needing ffmpeg, which ships with HA OS, Container and Supervised.
- **Nothing is written to your Home Assistant machine**; clips stream through memory to the cloud.
- **Clips land in Home Assistant's OneDrive app folder** — the access that integration holds, and what avoids a second app registration.

## Good to know

- **Reach is about 30 days**, bounded by the Reolink search API and your HDD. Beyond that even stamina cannot help: the footage is gone.
- **Every row has a recording.** A detection that produced none leaves nothing to find. The row badge says which *resolutions* exist.
- **Playback uses the low-resolution stream** where available. High resolution is H.265 at full sensor size — slow to open, undecodable in most browsers — so it is offered for downloads, not for watching.
- **High-resolution downloads won't open in QuickTime or Preview.** The file is valid and VLC and browsers play it, but the H.265 these recorders produce stalls Apple's decoder however it is packaged. Convert once if you need to: `ffmpeg -i clip.mp4 -c:v libx264 -crf 20 -c:a copy out.mp4`.
- **The trigger marker is often an estimate**, drawn dashed unless the camera reports its own pre-record time.
- **Panel downloads take about as long as the clip** — no fast-forward on the recorder's treadmill.
- **Detection counts and clip trimming need Home Assistant's recorder.** Disabled or purged, rows show no counts and clips fall back to whole segments.

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
| "No Reolink NVR found" | No NVR in the Reolink integration, or it's a standalone camera / Home Hub |
| An NVR is greyed out | Its Reolink config entry isn't loaded — the card says why |
| Rows have no person/vehicle labels | The NVR isn't reporting event types; needs Baichuan (port 9000) or parseable filenames |
| Rows show no detection counts | The recorder is disabled, starting, or has purged that far back |
| Cloud sync has no entities | Its recorder is gone from the Reolink integration, or the chosen cloud account was removed — the log says which |
| Clips queue but never upload | Check *Last error*; a revoked cloud login surfaces as a reauth prompt on that integration |
| "no ffmpeg is installed" | A 24/7 camera's clip must be cut; install ffmpeg or sync event-recording cameras only |
| Panel looks stale after an update | Clear your browser cache, then hard-refresh |
| Everything is slow | It's the NVR. It's always the NVR |

## Development

```bash
./scripts/check.sh --setup   # once: creates .venv, installs test dependencies
./scripts/check.sh           # lint, format, tests, frontend parse, clip writer
```

No NVR is needed for any test. Run the suite after every Home Assistant update: `tests/test_upstream_contract.py` pins what this expects of the Reolink integration and `reolink_aio`, including `entry.runtime_data`, which is not public API — so an upgrade breaking it becomes a clear failure naming what moved. `tests/frontend/test_clip.mjs` covers the browser-side MP4 writer and needs `node`, `ffmpeg` and `ffprobe`; it is skipped without them.

The integration domain is `reolink_stamina`, from before the rename, and stays: changing it would orphan existing config entries, entity ids, the panel URL and the cache.

## Contributing

Fork, open a pull request, follow the existing style, include tests.

## Licence

[MIT](LICENSE) · [Reolink integration][reolink]

[reolink]: https://www.home-assistant.io/integrations/reolink/
