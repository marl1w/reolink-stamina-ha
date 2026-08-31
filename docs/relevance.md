# Learning what is normal

[← back to the README](../README.md)

![Relevance in the timeline](../img/relevance.png)

> **Beta.** It collects, it learns and it marks, from the moment the panel is set up. What it has not had yet is months of real households, which is what the numbers behind it need in order to be right. Reports are the point.

A 24/7 recorder produces hundreds of detections a day, and almost all of them are the same detections it produced yesterday. This tells you which three are worth opening — not by recognising anything, but by counting.

It keeps a record of what each camera normally sees — what kind of thing, at what hour, on what sort of day, for how long. An event is interesting when that combination is rare, and *what kind of thing* is counted in its own right: a person on a camera that only ever sees the wind stands out before the hour is even considered. The cat that crosses the drive at one in the morning every night has told you what normal looks like there; a person doing the same thing has not.

## What it keeps

Read this first, because it is a record of when your cameras see people — which is close to a record of when your house is empty. It starts collecting when the panel is set up.

- **It stays on your machine.** Its own small SQLite file in your configuration folder. No cloud, no account, nothing leaves the machine.
- **No faces, no number plates, no images.** Timestamps and which sensor fired, and that is all there is.
- **It is small** — a few megabytes a year for a busy recorder — and it is in your backups, so a restored Home Assistant does not start again from nothing.
- **Removing the integration deletes the file.** That is the off switch, and it is the whole of it.

## Setting it up

Nothing to switch on. The last two pages of *Reolink Stamina → **Configure*** hold everything you can change, and all of it is optional.

**What else to count**, as one list per recorder: anything true of the whole property, like whether anybody is in, whether the alarm is set, whether the gate is locked. *"A person on the drive while nobody is home"* then becomes its own thing rather than just a person on the drive. Nothing is pre-selected — the model never interprets a signal, it counts the state as it finds it, so "is anyone home" works as well as a named person. Each camera's own floodlight, siren and day/night state are found automatically and need no picking.

**What counts as unusual** is the last page, and it holds two settings.

*How much to mark* — *Only the strangest*, *Balanced*, or *More, including borderline ones*. Measured across nine cameras, the three came out at roughly 1.6, 4 and 9 marks a day for the whole property.

*What each camera is compared with* — *All cameras together* (the default), *Each recorder on its own*, or *Each camera on its own*. Every camera is always judged against its own past first; this decides what a camera with too little history of its own may borrow from, and which cameras count as having fired before one another.

One pool suits most houses, and it asks least of a new camera — it leans on every other one until it has a history of its own. Keep recorders apart if yours cover *different properties*: a quiet camera at a holiday house should not inherit the rhythm of a street-facing gate two hundred miles away, and *"the drive fired ninety seconds before the hall"* is a statement about one building. *Each camera on its own* shares nothing at all: the most honest setting and the slowest to start, since a new camera has nothing to lean on until it has a history of its own.

Neither setting recounts anything. Both are applied when an event is scored, over a journal that was written down without them, so changing either just moves the line.

## What to expect

**Nothing for the first week or so.** A camera needs roughly a week of history *and* a few hundred detections before it can be compared against itself — both, because a quiet camera can have months of days behind it and still too little to compare against. The panel says which of the two it is short of rather than staying silent. Home Assistant's own recorder is read once at setup for a head start.

Once it is running:

- **An *Unusual* mark** appears on a row when something in it was rare for that camera. Only outliers are marked; nothing is ever labelled "common".
- **Tap the mark, or the ⓘ on any row, for the evidence** — the sentence saying why, and each signal drawn as a bar either side of centre, right for rarer than chance. The longest bar is what made the event stand out. This works from the first day, before anything can be marked, which is how you tell it is doing something.
- **An *Unusual* chip** in the filter row narrows four hundred rows to twelve. While a camera is still learning it reads *Learning…*.
- **A chart button** on each camera, and one in the toolbar for all of them, opens what that camera has actually counted: when it sees each kind of thing, how long they last, what fired before them. It is also the only way to spot a camera that has learned something wrong.

And, plainly, what it will **not** do:

- **It will not recognise anyone.** It counts; it does not identify.
- **Common is not safe.** It ranks by rarity and makes no claim about risk. A burglar at three in the afternoon in a busy driveway is statistically unremarkable.
- **It learns whatever it sees, including the bad.** A camera that has fired on spiders every night for months has made spiders normal.
- **Something genuinely new is marked for a while.** A new family car stands out daily for a fortnight, then stops.
- **A person walking past three cameras is three events.** Grouping them into one arrival is not built.

## Requirements

**Nothing installed** — no ffmpeg, no models, no GPU, no cloud service. It is arithmetic over timestamps and is meant to stay that way.

It does not depend on your recorder's retention either: detections are read from Home Assistant as they happen, so a ten-day purge no longer takes the history with it.
