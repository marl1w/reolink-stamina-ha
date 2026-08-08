# FAQ and troubleshooting

[← back to the README](../README.md)

## Questions

**Does it need my Reolink password?**
No. Every device, camera and recording comes through the official [Reolink integration][reolink], which already holds the credentials. Stamina stores none of its own.

**Does it replace the Reolink integration?**
No — it is a companion to it, and does not work without it.

**How far back can it see?**
About 30 days, bounded by the Reolink search API and your HDD. Beyond that the footage is gone and nothing can help.

**Does it work without an NVR?**
Only with the [hubs and standalone cameras beta](timeline.md#hubs-and-standalone-cameras-beta) switched on, and nothing here has been tested against that hardware. Cloud sync remains NVR-only.

**Does it record anything to my Home Assistant machine?**
Cloud sync streams clips through memory to the cloud and writes nothing. Adaptive playback writes temporary segments while a clip is playing and removes them afterwards. [Learning what is normal](relevance.md) keeps a small database of detection times, and only once you switch it on.

**Will it hammer my recorder?**
That is most of the point of the name. Searches are cached and refreshed in the background, clips queue rather than stampede, and only one fetch runs at a time per recorder.

**Why is everything so slow?**
It's the NVR. It's always the NVR.

## Turning on debug logging

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
| No sidebar item | Integration not added, or you're a non-admin and *Restrict to administrators* is on |
| "No Reolink NVR found" | No NVR in the Reolink integration, or it's a standalone camera / Home Hub — switch on the beta for those |
| A camera on an NVR is missing from the beta list | Deliberate: it is listed under its recorder instead, so the same footage does not appear twice |
| An NVR is greyed out | Its Reolink config entry isn't loaded — the card says why |
| Rows read "Recording" with no detection type | Neither Home Assistant's sensors nor the NVR classified them: check the recorder is enabled and hasn't purged that far back |
| Panel looks stale after an update | Every module is served under a URL named after the contents of the frontend directory, so an update renames all of them and a plain reload is enough. If you *edited* the files in place, reload the integration — see [CONTRIBUTING.md](../CONTRIBUTING.md) — because the URL is only renamed when the integration loads, and until then the browser is right to keep the copy it has |

### Playback

| Symptom | Likely cause |
| --- | --- |
| Black window, or "this browser cannot decode this recording" | The stream is H.265 and this is Chrome or Firefox. Switch on *Adaptive playback* |
| Low resolution needs converting too, not just high | Some models and firmware encode H.265 on both streams. Nothing is wrong: the ladder chooses by what the stream contains, not by which resolution it is, so it will convert whichever one needs it |
| Black window in Safari at high resolution | Same cause, despite Safari claiming H.265 support: Apple's decoder stalls on what these recorders produce. With *Adaptive playback* on it re-encodes instead, after a few seconds of finding out |
| Nothing plays in the iPhone app | Same option: iOS cannot read the recorder's stream at all, and needs the repackaged one |
| One camera is black on the phone while the others play | Its stream needs more than repackaging. With the beta on it lands on the re-encode rung by itself and stays there; the log says what ffmpeg made of it at debug |
| "Adaptive playback needs ffmpeg" | Install ffmpeg, or leave the option off |
| Playback is slow and the player says "Re-encoded" | The machine is converting in software. Expected on a Pi at high resolution; low resolution is far cheaper |
| "This clip cannot be played in this browser" | Every route was tried and none drew a frame. **Download this clip** beside the message writes it out as MP4 to watch locally, and the other resolution is often worth a try |
| `CERTIFICATE_VERIFY_FAILED`, or a 502 with an empty body, while search works fine | The recorder is being reached over HTTPS with a certificate Home Assistant does not trust. Turn *Verify the recorder's HTTPS certificate* off — it is off by default from 1.2.12 on. Alternatively, point the Reolink integration at the recorder's HTTP port instead, which many local-network setups already do |

### Cloud sync

| Symptom | Likely cause |
| --- | --- |
| Cloud sync has no entities | Its recorder is gone from the Reolink integration, or the chosen cloud account was removed — the log says which |
| Clips queue but never upload | Check *Last error*; a revoked cloud login surfaces as a reauth prompt on that integration |
| "no ffmpeg is installed" | A 24/7 camera's clip must be cut; install ffmpeg or sync event-recording cameras only |

### Learning what is normal

| Symptom | Likely cause |
| --- | --- |
| Nothing has changed in the panel | Expected. It only collects so far — see [what it does](relevance.md) |
| A camera says it is still collecting after weeks | It needs both a week *and* a few hundred detections. A quiet camera can have months of days and still too little to compare against — the detail view on any row says which of the two it is short of |
| No history was imported | Home Assistant's recorder is disabled, has purged, or excludes the Reolink sensors. Collection from now on is unaffected — the import is a one-off head start, not how it works |

[reolink]: https://www.home-assistant.io/integrations/reolink/
