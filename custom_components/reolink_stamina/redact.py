"""Keep credentials out of text that is about to be shown, logged or reported.

One playback route authenticates with the recorder's own username and password in the
query string — the endpoint rejects a percent-encoded password, so they travel in clear —
and ffmpeg and aiohttp both quote the whole URL back when they complain. That text reaches
the log, the diagnostics download and the panel, so it is scrubbed on the way out.

Kept in its own module, with nothing but the standard library behind it, so that every
caller can reach it: the cloud fetcher, the restreamer and the playback proxy would
otherwise have to import each other for a regular expression.
"""

from __future__ import annotations

from collections.abc import Iterable
import re

# `password` and `token` deliberately run to whitespace or a quote rather than stopping at
# `&`. Both reolink_aio's FLV URL and the playback URL built here put the secret last, so
# there is nothing after it worth keeping -- and stopping at `&` would leave the tail of a
# password that contains one in plain view, which is the failure that matters. `user` stops
# at `&` instead: a username is not the secret, and swallowing the rest of the URL would
# throw away the parameters that explain the failure.
_SECRET = re.compile(r"\b(password|token)=[^\s'\"]+")
_USER = re.compile(r"\buser=[^&\s'\"]+")

REDACTED = "***"


def scrub_credentials(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Blank credentials out of text bound for a log, a report or the panel.

    `secrets` are literal values to remove — the recorder's password and token, where the
    caller has the device to hand. They go first and are exact, so they cover the cases a
    pattern cannot: a credential quoted without its parameter name, split across a line
    wrap by ffmpeg, or carrying characters the pattern has to guess the extent of.

    The patterns then catch whatever the caller could not name, which is the common case:
    `_Stream` holds ffmpeg's output long after the URL that produced it is out of scope.
    """
    for secret in secrets:
        # A one-character password would otherwise redact half the message. The pattern
        # below still covers it, since it is matched by parameter name rather than value.
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    text = _SECRET.sub(rf"\1={REDACTED}", text)
    return _USER.sub(f"user={REDACTED}", text)


def api_secrets(api: object) -> tuple[str, ...]:
    """Return the credential values a device authenticates with, for `secrets`.

    reolink_aio exposes `hide_password`, which redacts these same values, but only as a
    whole-string transform on an object we do not always still hold. Read defensively:
    these are private attributes, and a library that renames them should cost the literal
    pass rather than the whole conversion.
    """
    found = []
    for attr in ("_password", "_enc_password", "_token"):
        try:
            value = getattr(api, attr, None)
        except Exception:
            continue
        if isinstance(value, str) and value:
            found.append(value)
    return tuple(found)
