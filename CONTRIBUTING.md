# Contributing

Issues and pull requests are welcome. Open a pull request with a description of your changes and the use case you are addressing, follow the existing style, and include tests.

Maintained by [marl1w](MAINTAINERS.md).

## Before you open a pull request

```bash
make setup   # once: creates .venv and installs test dependencies
make check   # lint, format, tests, and the frontend checks
```

`make` on its own lists everything there is to run. Every target that needs the virtualenv uses it, so there is nothing to activate.

Home Assistant 2026.3+ needs Python 3.14.2 or newer. If your default `python3` is older:

```bash
make setup PYTHON="$(pyenv root)/versions/3.14.6/bin/python3"
```

The contract tests in `tests/test_upstream_contract.py` are the important ones. They assert that the real Reolink integration and `reolink_aio` still look the way this panel expects, which is what turns an upstream change into a clear failure here instead of a broken panel in somebody's sidebar. Run them after every Home Assistant update, not only when you have changed something.

## House style

- **No new dependencies without a very good reason.** The integration installs with nothing but the standard library, and ffmpeg is optional even where it is used. That is a promise worth keeping.
- **Comments say why, not what.** The code says what it does; a comment earns its place by recording the measurement, the failure or the trade-off that made it look like this.
- **Everything user-facing gets a string in `strings.json` and `translations/en.json`** — the two are compared in review, and a key in one and not the other renders as a raw identifier.

## Editing the panel

One non-obvious thing. Everything under `custom_components/reolink_stamina/frontend/` is served under a URL named after that directory's contents, with cache headers that tell the browser it never changes — which is true of any one URL, and what keeps the panel off the network on every load.

That name is computed **when the integration loads**. So an edit becomes visible on:

**Settings → Devices & services → Reolink Stamina → ⋮ → Reload**

Not on a browser reload. No restart needed, and no clearing the cache: reloading renames every module and the browser fetches all of them again. Skipping the reload is what leaves Safari, quite correctly, showing you yesterday's panel.

## Working on "learning what is normal"

The journal stores raw state changes and every constant is applied when they are *read*, so nothing is baked in and everything can be re-tried against history already collected. `scripts/replay.py` is what does the re-trying:

```bash
make replay JOURNAL=journal.db ARGS=--tz\ Europe/Rome    # the recorder's own clock
make replay JOURNAL=journal.db ARGS=--sweep\ merge       # try several merge windows
make replay JOURNAL=journal.db ARGS=--sweep\ quantile    # try several thresholds
make replay JOURNAL=journal.db ARGS=--sweep\ scope       # what pooling recorders costs
```

**Pass `--tz`.** A copied database carries timestamps and no location, and Home Assistant defaults to UTC until something tells it otherwise — so without it every hour in the report sits an offset away from the hour the panel shows, and asking whether a person at two in the morning would be marked quietly asks about four. It defaults to this machine's zone, which is right when you are replaying your own journal and wrong when you are replaying somebody else's.

The solar term is absent for the same reason: no location, so where the sun was cannot be recovered and is reported as unknown rather than guessed. Everything else — the local clock, the configured signals stamped on each transition, the merge window — goes through the same `events.derive` the panel uses, so what comes out is what the panel would say.

It needs no Home Assistant running, only the file. If you change a constant, say in the pull request what the replay output looked like before and after — these numbers were first guesses, and real journals are the only thing that can improve them.

## Seeing the panel without Home Assistant

`scripts/preview.py` serves the real panel modules on this machine against an invented
household, so a change can be looked at without restarting anything:

```bash
make preview            # http://127.0.0.1:8123
make preview SEED=7     # a different household
make preview DAYS=20    # less history, so nothing is scored yet
make preview PORT=9000
```

Two things worth knowing. The detections are invented, but the marks are not: they come from
the same merging, rate tables and scoring the integration ships, so what you see there is what
that data would produce in Home Assistant. And the picture is a test pattern rather than
footage — ffmpeg makes one on the first run, and the passthrough rung of the playback ladder
refuses on purpose so the converted rung is what gets exercised.

Because the modules are served straight from `custom_components/reolink_stamina/frontend/`
with caching off, an edit needs a browser reload and nothing else — unlike inside Home
Assistant, where the URL only changes when the integration reloads.

## Releasing

Bump `version` in `custom_components/reolink_stamina/manifest.json` for anything a user
would notice. Minor for a new feature, patch for a fix.

It is not only bookkeeping. Two things read that number at runtime:

- **The panel introduction** appears once per version. Ship a feature without bumping and
  nobody is told about it, because the browser has already seen this version.
- **The playback ladder forgets what it learned** from an earlier release, so a build that
  fixed a conversion is given a fresh chance instead of inheriting an old verdict.

Nothing checks this for you — a version that stays still while the code moves looks exactly
like a version that had nothing to say.
