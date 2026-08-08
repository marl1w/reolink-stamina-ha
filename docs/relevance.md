# Learning what is normal

[← back to the README](../README.md)

![Relevance](../img/relevance.png)

![Relevance in the timeline](../img/relevance.png)

> **Beta.** It works end to end — it collects, it learns, and it marks. What it has not had yet is months of real households, which is what the numbers behind it need in order to be right. Reports are the point.

A 24/7 recorder produces hundreds of detections a day, and almost all of them are the same detections it produced yesterday. The timeline puts them in one list; what it cannot do is tell you which three are worth opening.

This answers that without recognising anything. It keeps a record of what each camera normally sees, at what hour, for how long — and an event is interesting when that combination is rare. A cat that crosses the drive at one in the morning every night has told you what normal looks like there. A person doing the same thing has not, and the difference falls out of counting rather than out of understanding anything.

That becomes a small mark on the row, a sentence saying why, and a filter that turns four hundred events into twelve.

## In the panel

**An *Unusual* mark** appears on a row when something inside it was rare for that camera. Only outliers are marked and nothing is ever labelled "common" — a mark on nineteen rows in twenty is noise people stop seeing, and it would devalue the twentieth.

**Tap the mark, or the ⓘ on any other row,** and you get the evidence: the sentence, a gauge showing how far past the cut it landed, and each signal drawn as a bar either side of a centre line — right of centre for rarer than chance, left for more common. Which signal made an event stand out is the longest bar, so it reads without arithmetic, and an ordinary event looks like four short stubs near the middle.

Beside each bar is the figure and the count it came from: *seen 122×* is a fact about that camera, not an estimate.

That detail view **works from the first day**, before anything can be marked. It is the honest answer to "is this doing anything?" — you can watch what it is collecting long before it has enough to have an opinion.

**An *Unusual* chip** in the filter row narrows the list to just those rows. While a camera is still learning it reads *Learning…* and is not clickable, which is true and better than a filter that can only return nothing.

## What it keeps

**Nothing is recorded until you switch this on.** What it keeps is a record of when your cameras see people, which is close to a record of when your house is empty — not something to start on your behalf. Switching it on is the consent.

- **It stays on your machine.** Its own small SQLite file in your Home Assistant configuration folder. No cloud, no account, nothing leaves the machine.
- **It is small.** A few megabytes a year for a busy recorder.
- **It is in your backups**, like the rest of that folder — deliberately, so a restored Home Assistant does not start again from nothing. The file is checkpointed before each backup so the copy is a usable database rather than one caught mid-write.
- **Turning it off** stops the recording and keeps what was collected. **Removing the integration** deletes the file.
- **No faces, no number plates, no images.** It stores timestamps and which sensor fired. There is no picture in it, and none is planned.

## What it needs

**Nothing installed.** No ffmpeg, no models, no GPU, no cloud service — this is arithmetic over timestamps and it is meant to stay that way.

It also **does not depend on your recorder's retention**. Detections are read from Home Assistant's state machine as they happen, so a ten-day purge no longer takes the history with it. Home Assistant's own recorder is read exactly once, when you turn this on, to import whatever it still holds — however many days you have it set to keep, not an assumed ten. That import runs in the background and does not hold up a restart.

## Turning it on

*Reolink Stamina → **Configure** → **Learn what is normal***.

That is all there is to configure. There is nothing to tune and nothing to train — it starts collecting, and the detail view on any row shows you what it has.

## How long before it says anything

A camera needs roughly **a fortnight of history and a few hundred detections** before it can be compared against itself. Both, not either: a camera can have months behind it and still see too little to compare anything against, and that is reported as its own state rather than as silence.

Your import shortens the wait by however much Home Assistant had already recorded — often ten days, more if you have set a longer retention.

## What it will not do

Worth saying plainly, because the obvious expectations are the wrong ones.

- **It will not recognise anyone.** No faces, no "that's the postman". It counts; it does not identify.
- **Common is not safe.** It ranks by how rare something is, and makes no claim about risk. A burglar at three in the afternoon in a busy driveway is statistically unremarkable.
- **It learns whatever it sees, including the bad.** A camera that has been firing on spiders every night for months has made spiders normal, and they will never be marked. That is correct behaviour, but it means its quality depends on your recorder's own detection quality.
- **Something genuinely new will be marked for a while.** A new family car stands out daily for a fortnight and then stops. Self-healing, and mildly irritating in the meantime.

## Where it is going

1. **The record.** ✅ The journal, and the one-off import of what Home Assistant already held.
2. **Scoring and the mark.** ✅ The rate model, the mark on the row, the sentence saying why, the detail view and the filter chip.
3. **Your own signals.** Point it at entities you already have — is anyone home, is the alarm armed — so "a person on the drive while nobody is in" can be counted as its own thing. Nothing is assumed and nothing is pre-selected; whichever entities suit your house are yours to choose.
4. **What the picture says.** Optional and behind its own switch: the coarse shape and position of what moved, so an unfamiliar vehicle at the gate can be told from a familiar one. This is the only part that will need ffmpeg.
