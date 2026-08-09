# Cloud sync

[← back to the README](../README.md)

![Cloud sync devices and sensors](../img/cloud-sync.png)

An off-site copy of what mattered, so the footage outlives the recorder it was written on.

One cloud sync per NVR. It watches that recorder's cameras and uploads a clip of each detection to your own OneDrive. Each one becomes a device with a switch — the thing you automate from an alarm — and sensors for the quota, the queue and what has been uploaded.

## Setting one up

*Reolink Stamina → **Add cloud sync***, once per recorder.

| Setting | Default | What it is for |
| --- | --- | --- |
| Recorder | First NVR not yet synced | Every camera on it is covered. Fixed once created: a sync owns the clips it has already uploaded |
| Cloud account | — | An existing OneDrive integration; nothing to authorise again |
| Storage quota | 15 GB | When it is full, the oldest clips are deleted to make room |
| Detections to upload | person, vehicle, animal | Motion fires constantly on most cameras and would spend the quota on empty footage |
| Also upload unusual events | off | A second rule — see below |
| Kinds that count as unusual | all of them | What that rule may upload. Ignored while it is off |
| Resolution | Low | A fraction of the size, and plays everywhere |
| Seconds before / after | 10 s / 10 s | How much footage to keep either side of the event |
| Folder name | `reolink/<NVR name>` | Where the clips go |

Everything but the recorder can be changed later under **Configure**.

## What to expect

Clips are named date-first, so any file browser lists them chronologically:

```
reolink/Reolink-NVR/240102_153045_recorder_front-door.mp4
```

- **One event is one clip**, however many sensors fired. It runs from the first detection to a margin after the last one clears, and stepping out of frame and back rejoins the same clip. A sensor stuck on is cut off after ten minutes.
- **A clip appears within a minute or so.** The recorder needs a moment to finish writing it; several cameras firing at once are handled in parallel, as far as your machine's memory allows.
- **The switch stops new events being taken on**, not the one already under way — disarming an alarm should not discard the footage that made you disarm it.
- **Nothing is written to your Home Assistant machine.** Clips stream through memory into Home Assistant's own OneDrive app folder.

## Uploading what is unusual

The list above answers "what is worth keeping". It cannot answer "keep the odd one", because the odd one is usually a kind you chose *not* to sync — the motion at three in the morning on a camera whose motion fires four hundred times a day.

So there is a second rule, off by default: an event is uploaded when [the model marks it as unusual](relevance.md) for its camera, whatever its kind. Those clips are named `..._u.mp4`.

Two things to know before switching it on:

- **It does nothing for the first week or so.** Nothing can be called unusual until a camera has roughly a week and a few hundred detections behind it.
- **It outranks the switch.** Keeping the odd one out is the point of it, so an unusual event is uploaded even while that recorder's switch is off. Ordinary footage still stops, and a recorder that never switched this on behaves exactly as before.

The quota does not treat these clips specially — an unusual one is evicted by age like any other, so give a recorder whose anomalies you want to keep some room.

## Requirements

- An existing **OneDrive** integration. Its credentials are reused, so there is nothing to authorise again.
- **ffmpeg**, but only for cameras recording 24/7, whose clips have to be cut out of a longer segment. A recorder whose cameras record on events needs nothing installed.
