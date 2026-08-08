# Reolink Stamina

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz) [![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.1%2B-41BDF5.svg)](https://www.home-assistant.io) [![Release](https://img.shields.io/github/v/release/marl1w/reolink-stamina-ha?display_name=tag&sort=semver)](https://github.com/marl1w/reolink-stamina-ha/releases) [![Licence: MIT](https://img.shields.io/badge/Licence-MIT-blue.svg)](LICENSE)

*Your NVR has the memory. This has the stamina.*

Reolink hardware records diligently and then makes the footage tedious to get at: one camera at a time, one app or one folder at a time, and nothing that survives the recorder being stolen. **Reolink Stamina closes that gap** — it is about the experience of *using* the Reolink system you already own, from inside Home Assistant.

It is a companion to the official [Reolink integration][reolink], not a replacement. Every device, camera and recording comes from it, so there are **no extra credentials to enter**.

**Why "Stamina"?** Because everything else in this chain tires quickly, and a good experience has to outlast it. The recorder answers searches at its leisure, streams playback at walking pace, and drops your connection if you ask twice too fast. So: cached results appear instantly while it keeps asking in the background, clips queue instead of stampeding the recorder, a backlog survives a restart, and it will process the four hundredth cat of the evening without complaint.

## Three things it does

| | |
| --- | --- |
| **[One timeline across every device →](docs/timeline.md)** | Every camera's detections in a single list, whatever recorder they hang off, with the clip one click away and the playhead already at the event. |
| **[An off-site copy of what mattered →](docs/cloud-sync.md)** | A clip of each detection uploaded to your own cloud storage, event by event, so the evidence outlives the recorder it was written on. |
| **[Learning what is normal →](docs/relevance.md)** | A record of what each camera usually sees and when, so the handful of events worth opening can be told from the hundreds that are not. Beta. |

## What you need

- Home Assistant 2026.1 or newer, any install type
- The official **Reolink integration**, with at least one **NVR** with a working HDD
- For cloud sync only: an existing **OneDrive** integration — its credentials are reused

Standalone cameras and Home Hubs are filtered out, unless you switch on [the beta that lists them](docs/timeline.md#hubs-and-standalone-cameras-beta).

## Install

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=marl1w&repository=reolink-stamina-ha&category=integration)

1. HACS → **⋮** → **Custom repositories** → add `https://github.com/marl1w/reolink-stamina-ha`, category **Integration**.
2. Install **Reolink Stamina**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Reolink Stamina.**

Setup asks nothing: it discovers your NVRs through the Reolink integration and adds itself to the sidebar. Once an NVR is set up there, Home Assistant also offers Stamina by itself — it watches the network for Reolink recorders exactly as the official integration does, and the panel turns up as a **Discovered** card.

> **Tested against:** Home Assistant 2026.8.4 · Reolink RLN8-410 (N7MB01) on firmware v3.6.5.562_26062933 · cameras B800, RLC-81MA, Duo 2 PoE, Duo 2v PoE. Other NVRs should work; other *models* of recorder are where surprises live, so reports are welcome.

## Documentation

- **[The timeline](docs/timeline.md)** — browsing, playback, downloads, and the panel's options
- **[Cloud sync](docs/cloud-sync.md)** — off-site clips, quotas and how the switch behaves
- **[Learning what is normal](docs/relevance.md)** — what it collects, what it will do with it, and what it keeps on your machine
- **[FAQ and troubleshooting](docs/faq.md)** — symptoms, causes, and how to turn on debug logging

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for how to run the checks and the one non-obvious thing about editing the panel. Maintained by [marl1w](MAINTAINERS.md).

```bash
make          # what there is to run
make setup    # once
make check    # lint, format, tests, frontend
make preview  # the panel in a browser, no Home Assistant needed
```

## Licence

[MIT](LICENSE) · [Reolink integration][reolink]

[reolink]: https://www.home-assistant.io/integrations/reolink/
