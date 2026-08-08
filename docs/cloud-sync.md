# Cloud sync

[← back to the README](../README.md)

![Cloud sync devices and sensors](../img/cloud-sync.png)

An off-site copy of what mattered, so the evidence outlives the recorder it was written on.

One cloud sync per NVR, each with its own switch, quota and destination. It watches that recorder's detection sensors and, for each event, fetches a clip and uploads it to OneDrive.

Each sync becomes a device — shown under the NVR itself, the way the Reolink integration's own devices are — with a **switch** for whether new clips are *accepted*, which is what you automate from an alarm, and sensors for quota used and available, clips stored, queued uploads, uploads since restart, last upload and last error.

Clips are named date-first so any file browser lists them chronologically:

```
reolink/Reolink-NVR/240102_153045_recorder_front-door.mp4
```

When the quota is full, the **oldest clips are deleted** until the new one fits.

## How it behaves

- **The switch gates admission, not delivery.** Off stops new clips being accepted; anything queued still uploads. Disarming an alarm shouldn't discard footage of whatever made you disarm it.
- **One event is one clip.** Several sensors fire on one arrival, and people step in and out of frame. A clip runs from the first detection to a margin after the *last* one clears; stepping away and back rejoins the same clip. A sensor stuck on is cut off after ten minutes.
- **One fetch at a time per recorder** — Reolink recorders are sprinters, not marathon runners.
- **Cameras recording on events cost nothing extra:** their recording already *is* the clip, pre-record buffer included, so it goes up as it stands at wire speed.
- **Cameras recording 24/7 are cut down.** Only the clip's bytes are streamed and ffmpeg copies them into MP4 — nothing is re-encoded.
- **Nothing is written to your Home Assistant machine**; clips stream through memory to the cloud.
- **Clips land in Home Assistant's OneDrive app folder** — the access that integration holds, and what avoids a second app registration.

## Setting one up

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

## Requirements

- An existing **OneDrive** integration in Home Assistant. Its credentials are reused, so there is nothing to authorise again.
- **ffmpeg**, but only for cameras recording 24/7 — their clips have to be cut out of a longer segment. A recorder whose cameras record on events needs nothing installed.
