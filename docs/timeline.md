# The timeline

[← back to the README](../README.md)

![The Reolink Stamina panel](../img/panel.png)

Every camera's detections in one list, whatever recorder they hang off.

Pick cameras, then a day or a range. Rows say what fired and how often, clips open where the event is rather than at the top of a five-minute segment, and **Download** saves one as MP4 in your choice of resolution. Filter chips toggle each kind of detection: person, vehicle and animal start on, motion and scheduled recordings start off, because on a 24/7 recorder they outnumber real events many times over.

## Setting it up

Nothing to do — the panel finds your devices through the Reolink integration and appears in the sidebar.

To adjust it: *Reolink Stamina → **Configure***, over four pages — what is switched on, the player, what else the counting should watch, and what counts as unusual.

| Option | Default | What it is for |
| --- | --- | --- |
| Stream used for browsing | Low resolution | Low searches faster; playback and downloads can still use high |
| Event segment length | 5 min | How finely continuous recordings split into rows. 0 lists one row per recording file |
| Estimated pre-roll | 5 s | Where to draw the trigger marker for cameras that don't report their own |
| Start playback before the detection | 30 s | Where the playhead opens inside the clip. It does not change the clip |
| Clip: seconds before / after | 15 s / 15 s | Where a trimmed clip starts and ends on 24/7 footage |
| Hide scheduled recordings | on | Which filters start enabled |
| Restrict to administrators | on | Recordings can be sensitive. Off lets non-admins open the panel |
| Verify the recorder's HTTPS certificate | off | Reolink's factory certificate cannot pass verification, and the Reolink integration's own calls do not check it either. On only if you installed one this Home Assistant trusts |

## What to expect

- **About 30 days of reach**, bounded by the Reolink search API and your HDD. Beyond that the footage is gone.
- **Playback that adapts on its own.** The recording normally goes straight from the recorder to your browser. When that draws a black window — H.265 on Chrome or Firefox, or anything on an iPhone — Home Assistant repackages it, and re-encodes only if the browser genuinely cannot decode it. A badge on the player says which route the picture took, and downloads are always the original.
- **Low resolution by default**, because high is H.265 at full sensor size: slow to open and undecodable in most browsers. The player offers a resolution picker anyway, since converting is what makes high watchable.
- **A player as wide as you want it.** Drag the seam between the list and the picture; double-click puts it back. Pinch, drag or double-tap to zoom in on a face or a number plate. On a narrow screen the player opens over the list instead.
- **Some recorders will not stream a recording at all.** Home Hubs, and NVRs like the RLN36 whose own web player cannot replay either, hand over the whole recorded file instead. That is found by asking rather than assumed, and re-measured after every restart or reload, so a firmware update that changes the answer is picked up. Playback still starts where you clicked; the seeking is the browser's rather than the recorder's.
- **Downloads take about as long as the clip** — there is no fast-forward on the recorder's side.
- **High-resolution downloads will not open in QuickTime or Preview.** The file is valid — VLC and browsers play it — but Apple's decoder stalls on the H.265 these recorders produce. `ffmpeg -i clip.mp4 -c:v libx264 -crf 20 -c:a copy out.mp4` converts it once.

### Hubs and standalone cameras

Home Hubs and cameras with their own SD card are listed alongside your recorders and tagged as the untested quantity they are — they answer the same search API, but no hardware here has one, so reports are useful. A camera that is already a channel on one of your NVRs is left out, or the same footage would appear twice. Cloud sync remains NVR-only.

## Requirements

- **Home Assistant's recorder**, for detection labels, counts and clip trimming. Disabled or purged, rows fall back to whatever the NVR itself reported and clips to whole segments.
- **ffmpeg**, only for the converted playback routes. Without it, a recording your browser cannot play can still be downloaded and watched locally.
