# FAQ and troubleshooting

[← back to the README](../README.md)

## Questions

**Does it need my Reolink password?**
No. Every device, camera and recording comes through the official [Reolink integration][reolink], which already holds the credentials.

**Does it replace the Reolink integration?**
No — it is a companion, and does not work without it.

**How far back can it see?**
About 30 days, bounded by the Reolink search API and your HDD.

**Does it work without an NVR?**
Home Hubs and standalone cameras are [listed alongside recorders](timeline.md#hubs-and-standalone-cameras), but nothing here has been tested against that hardware. Cloud sync remains NVR-only.

**Does it write anything to my Home Assistant machine?**
Cloud sync streams clips through memory and writes nothing. Converted playback writes temporary segments and removes them afterwards. [Learning what is normal](relevance.md) keeps a small database of detection times; removing the integration deletes it.

**Will it hammer my recorder?**
That is most of the point of the name. Searches are cached and refreshed in the background, and the recorder is only ever asked one thing at a time.

**Why is everything so slow?**
It's the NVR. It's always the NVR.

## Debug logging

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.reolink_stamina: debug
    reolink_aio: debug
```

## Symptoms

### Setup and the panel

| Symptom | Likely cause |
| --- | --- |
| No sidebar item | Integration not added, or you are a non-admin and *Restrict to administrators* is on |
| "No Reolink NVR found" | Nothing set up in the Reolink integration yet |
| A standalone camera is missing | Deliberate, if it is also a channel on one of your NVRs — it is listed under its recorder instead |
| An NVR is greyed out | Its Reolink config entry is not loaded; the card says why |
| Rows read "Recording" with no detection type | Neither Home Assistant's sensors nor the NVR classified them — check the recorder is enabled and has not purged that far back |
| Panel looks stale after an update | A plain reload is enough. If you *edited* the files in place, reload the integration — see [CONTRIBUTING.md](../CONTRIBUTING.md) |

### Playback

| Symptom | Likely cause |
| --- | --- |
| Black window, or "this browser cannot decode this recording" | Every route was tried and none drew a frame. If the log says ffmpeg is missing, install it — the conversions need it |
| Nothing plays in the iPhone app | iOS cannot read the recorder's stream at all and needs the repackaged one, which needs ffmpeg |
| One camera is black on the phone while the others play | Its stream needs re-encoding rather than repackaging; it finds that rung by itself, and the log says what ffmpeg made of it at debug |
| Low resolution needs converting too, not just high | Some models encode H.265 on both streams. The route is chosen by what the stream contains, not by which resolution it is |
| Playback is slow and the badge says "Re-encoded" | The machine is converting in software. Expected on a Pi at high resolution |
| `CERTIFICATE_VERIFY_FAILED`, or a 502 with an empty body, while search works fine | The recorder is being reached over HTTPS with a certificate Home Assistant does not trust. Turn *Verify the recorder's HTTPS certificate* off |
| "The recorder would not serve this recording on any playback endpoint" | Every way of asking was refused. Some recorders — Home Hubs, and NVRs like the RLN36 — will not stream a recording at all and hand over the whole file instead, which is found automatically. Seeing this means even that was refused: check the clip still exists and the recorder is reachable |
| Playback works but dragging the playhead is slow or lands in the wrong place | The recorder is one that only serves whole files, so the seeking is the browser's rather than the recorder's. How well it works depends on the recorder answering range requests |

### Cloud sync

| Symptom | Likely cause |
| --- | --- |
| No entities | Its recorder is gone from the Reolink integration, or the cloud account was removed; the log says which |
| Clips queue but never upload | Check *Last error* — a revoked cloud login surfaces as a reauth prompt on that integration |
| "no ffmpeg is installed" | A 24/7 camera's clip has to be cut out of a longer segment |
| Nothing is uploaded despite *Also upload unusual events* | Expected for the first week or so, until a camera has enough behind it to call anything unusual |

### Learning what is normal

| Symptom | Likely cause |
| --- | --- |
| Nothing is marked yet | Expected — see [how long it takes](relevance.md#what-to-expect) |
| A camera says it is still collecting after weeks | It needs a week *and* a few hundred detections; the detail view on any row says which it is short of |
| One property's cameras are judged against another's rhythm | By default every camera is compared with every other. Set *What each camera is compared with* to *Each recorder on its own* under **Configure** |
| No history was imported | Home Assistant's recorder is disabled, has purged, or excludes the Reolink sensors. Collecting from now on is unaffected — the import is a head start, not how it works |

[reolink]: https://www.home-assistant.io/integrations/reolink/
