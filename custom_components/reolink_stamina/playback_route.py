"""Which endpoint a recorder serves playback from, and how to address it.

Reolink recorders do not agree on this, and the disagreement does not follow the device
family. Three endpoints exist:

* `cmd=Playback` on the CGI API, addressed by timestamp. This is what the recorders'
  own web players use, what Home Assistant's Reolink integration uses, and what this
  integration has always used.
* `/flv` with `stream=playback.bcs`, addressed by file name, proxying the recorder's
  RTMP service.
* `cmd=Download`, addressed by file name, which hands over the whole recorded file rather
  than streaming it. Not real-time playback at all, and it cannot seek — the last resort,
  for recorders that have neither of the other two.

Measured, on hardware:

* RLN8-410 on firmware v3.6.5.562 answers `cmd=Playback` with FLV on every channel, and
  closes the connection without sending a byte for every form of the `/flv` playback
  request — while serving `/flv` live view from the same endpoint perfectly well.
* RLN16-410 is reported to do the exact reverse: 404 for every `cmd=Playback` however it
  is phrased, and `/flv` playing and seeking correctly.
* RLN36 is reported to answer neither: 404 on `cmd=Playback`, and `/flv` accepted and then
  silent until the read times out. Its own web player cannot replay either, so there is no
  better-phrased request to find — the recorder simply does not serve recordings over
  either real-time endpoint, and only `cmd=Download` will give them up.
* Home Hubs behave like the RLN36 and are tried on `cmd=Download` first, because they are
  known in advance never to have the other two.

So no endpoint can be assumed, and `is_nvr` does not predict which one a device has:
three of the four above are NVRs. The rule here is therefore not a rule at all but a
measurement. The route that has always worked is tried first, the other is tried only
when it fails, and the answer is remembered per config entry so the cost is one refused
request per device per Home Assistant run.

Remembered in memory rather than on disk, deliberately. A firmware update can move a
recorder from one endpoint to the other, and a remembered route that outlives the firmware
it was measured against is a fault nobody would think to look for. Forgetting on restart
and on reload costs one request and cannot go stale.
"""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
import logging
import re
from typing import Any, Final, NamedTuple
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from aiohttp import ClientResponse, ClientTimeout
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import STREAM_MAIN, STREAM_SUB
from .redact import api_secrets, scrub_credentials
from .reolink_registry import DeviceUnavailableError, ReolinkIncompatibleError, async_get_host
from .tls import async_nvr_session

_LOGGER = logging.getLogger(__name__)

# The recorder's own stream selector: 1 is the sub stream, 0 the main one.
PLAYBACK_STREAM_TYPE: Final = {STREAM_SUB: 1, STREAM_MAIN: 0}

# The CGI route first, always: it is addressed by timestamp rather than by file name, it
# authenticates with a token rather than the account password, and it is the one this
# integration has years of behaviour with. `/flv` is the fallback, not the equal.
ROUTE_PLAYBACK: Final = "playback"
ROUTE_FLV: Final = "flv"
# Last, and only ever last: it transfers the whole file rather than streaming it, and the
# recorder will not seek it, so it is what a device gets when it has nothing better.
ROUTE_DOWNLOAD: Final = "download"
ROUTE_ORDER: Final = (ROUTE_PLAYBACK, ROUTE_FLV, ROUTE_DOWNLOAD)
# The two that arrive as a live-paced stream, which is all the FLV pass-through can pipe.
ROUTE_ORDER_REALTIME: Final = (ROUTE_PLAYBACK, ROUTE_FLV)

# Generous: the recorder sends at roughly real time, so a long clip takes a long time.
# The browser closing the connection is what normally ends it.
STREAM_TIMEOUT: Final = ClientTimeout(total=None, sock_connect=15, sock_read=60)

# How long a route gets to answer at all, as opposed to how long it may then take between
# chunks. Separate because they are different questions: an RLN36 accepts the `/flv`
# connection and says nothing, and waiting a full read timeout for that costs every route
# behind it. Once headers arrive the generous read timeout above governs the body.
PROBE_SECONDS: Final = 15

_ROUTES_KEY: Final = "reolink_stamina_playback_routes"


class PlaybackRouteError(HomeAssistantError):
    """Raised when no playback endpoint on the recorder would serve a recording."""


@dataclass(frozen=True, slots=True)
class Recording:
    """Everything a recorder has to be told to replay one clip.

    Carried as one value because it travels together through four layers — the panel's
    URL, the view that parses it, the route chosen for it and the URL finally sent to the
    recorder — and because the two views used to parse the same seven path segments with
    the same seven lines each.
    """

    entry_id: str
    channel: int
    stream: str
    filename: str
    # The recording's own start, not the window's: the endpoint locates the file by it.
    start_id: str
    playback_id: str
    seek: int

    @classmethod
    def from_path(
        cls,
        entry_id: str,
        channel: str,
        stream: str,
        filename: str,
        start_id: str,
        playback_id: str,
        seek: str,
    ) -> Recording:
        """Parse the path segments of a playback URL.

        Raises ValueError or UnicodeDecodeError on anything malformed, which both views
        turn into a 400.
        """
        return cls(
            entry_id=entry_id,
            channel=int(channel),
            stream=stream,
            filename=urlsafe_b64decode(filename.encode()).decode(),
            start_id=start_id,
            playback_id=playback_id,
            seek=max(0, int(seek)),
        )

    def path(self, prefix: str) -> str:
        """Return this integration's own URL for the recording, under `prefix`.

        The file name is base64'd and everything is a path segment rather than a query
        parameter, so the whole URL can be signed by the panel with no ambiguity about
        what the signature covered.
        """
        encoded = urlsafe_b64encode(self.filename.encode()).decode()
        return (
            f"{prefix}/{self.entry_id}/{self.channel}/{self.stream}"
            f"/{encoded}/{self.start_id}/{self.playback_id}/{max(0, int(self.seek))}"
        )

    @property
    def label(self) -> str:
        """A short name for logs and stream labels."""
        return f"{self.entry_id}/{self.channel}/{self.stream}@{self.seek}s"


# ------------------------------------------------------------------- remembered routes


@callback
def async_remembered_route(hass: HomeAssistant, entry_id: str) -> str | None:
    """Return the endpoint this recorder was last seen to serve playback from."""
    return hass.data.get(_ROUTES_KEY, {}).get(entry_id)


@callback
def async_remember_route(hass: HomeAssistant, entry_id: str, route: str) -> None:
    """Record which endpoint answered, so the other is not tried again."""
    routes = hass.data.setdefault(_ROUTES_KEY, {})
    if routes.get(entry_id) != route:
        _LOGGER.debug("Recorder %s serves playback from the %s route", entry_id, route)
    routes[entry_id] = route


@callback
def async_forget_routes(hass: HomeAssistant) -> None:
    """Forget every measured route, so the next request measures again."""
    hass.data.pop(_ROUTES_KEY, None)


@callback
def async_all_routes(hass: HomeAssistant) -> dict[str, str]:
    """Return every remembered route, for diagnostics."""
    return dict(hass.data.get(_ROUTES_KEY, {}))


@callback
def _async_route_order(
    hass: HomeAssistant, entry_id: str, *, prefer: str | None = None
) -> tuple[str, ...]:
    """Return the routes to try, best first.

    A remembered route is tried first but the others are still kept behind it: a recorder
    that stops answering the endpoint it answered an hour ago should fall back rather
    than fail, and re-measuring costs nothing once the first attempt succeeds.

    `prefer` is for what is known before any measurement — a Home Hub has neither
    real-time endpoint, and making it prove that on every restart is two refused requests
    nobody learns anything from.
    """
    first = async_remembered_route(hass, entry_id) or prefer
    if first is None:
        return ROUTE_ORDER
    return (first, *(route for route in ROUTE_ORDER if route != first))


# ------------------------------------------------------------------------ URL building


def _with_seek(template: str, seek: int) -> str:
    """Return the library's FLV URL with its seek set to where playback should begin.

    reolink_aio builds the whole URL correctly — scheme, ports, stream type, credentials —
    except that it pins `seek=0`. Substituted as text rather than parsed and rebuilt: the
    library sends the password deliberately unencoded because these endpoints reject a
    percent-encoded one, and a round trip through parse_qs/urlencode would encode it.

    Any value is matched, not just the pinned zero, so a library that starts computing the
    seek is still overridden rather than quietly duplicated. Appending only happens when
    there is no `seek` at all, so the URL can never carry two of them.
    """
    url, replaced = re.subn(r"(?<=[?&])seek=\d+(?=&|$)", f"seek={seek}", template, count=1)
    if replaced:
        return url
    return f"{url}{'&' if '?' in url else '?'}seek={seek}"


async def _async_flv_url(api: Any, recording: Recording) -> str:
    """Build the `/flv` playback URL, which addresses a recording by file name."""
    from reolink_aio.enums import VodRequestType

    _mime, template = await api.get_vod_source(
        recording.channel, recording.filename, recording.stream, VodRequestType.FLV
    )
    return _with_seek(template, max(0, int(recording.seek)))


async def _async_download_url(api: Any, recording: Recording) -> str:
    """Build the whole-file MP4 URL used by Home Hubs."""
    from reolink_aio.enums import VodRequestType

    _mime, url = await api.get_vod_source(
        recording.channel, recording.filename, recording.stream, VodRequestType.DOWNLOAD
    )
    return url


async def _async_playback_url(api: Any, recording: Recording) -> str:
    """Build the `cmd=Playback` URL, as the recorder's own web player builds it.

    Every parameter matters. `start` is the recording's StartTime while `playbackTime` is
    the same instant in UTC, and both are required; `type` selects the resolution
    numerically; `channel` and `seek` are mandatory even at offset zero. Omitting any one
    of them makes the recorder answer 404 or drop the connection.

    reolink_aio's own PLAYBACK URL omits four of them and derives `start` by
    pattern-matching the file name, which never matches the synthetic names a recorder
    returns — so only its base address and freshly minted token are borrowed, and
    authentication and renewal stay the library's problem.
    """
    from reolink_aio.enums import VodRequestType

    _mime, template = await api.get_vod_source(
        recording.channel, recording.filename, recording.stream, VodRequestType.PLAYBACK
    )
    parts = urlsplit(template)
    token = parse_qs(parts.query).get("token", [""])[0]

    query: dict[str, Any] = {
        "cmd": "Playback",
        "channel": recording.channel,
        "type": PLAYBACK_STREAM_TYPE.get(recording.stream, 1),
        "start": recording.start_id,
        "seek": max(0, int(recording.seek)),
        "source": recording.filename,
        "playbackTime": recording.playback_id,
    }
    if token:
        query["token"] = token

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


_BUILDERS: Final = {
    ROUTE_PLAYBACK: _async_playback_url,
    ROUTE_FLV: _async_flv_url,
    ROUTE_DOWNLOAD: _async_download_url,
}


async def async_route_url(hass: HomeAssistant, recording: Recording, route: str) -> str:
    """Build the URL for one named route. Exposed for tests and diagnostics."""
    api = async_get_host(hass, recording.entry_id).api
    return await _BUILDERS[route](api, recording)


@callback
def async_playback_secrets(hass: HomeAssistant, entry_id: str) -> tuple[str, ...]:
    """Return the credential values a recorder's playback URLs may carry.

    Handed to whatever outlives the URL — an ffmpeg whose output is read minutes later —
    so its complaints can be scrubbed exactly rather than by pattern alone. Best effort:
    a device that cannot be read right now costs the literal pass, not the conversion.
    """
    try:
        return api_secrets(async_get_host(hass, entry_id).api)
    except (DeviceUnavailableError, ReolinkIncompatibleError):
        return ()


# --------------------------------------------------------------------------- opening


class OpenedStream(NamedTuple):
    """An open upstream, and which route and URL produced it."""

    response: ClientResponse
    route: str
    url: str


async def async_open_playback_stream(
    hass: HomeAssistant,
    recording: Recording,
    *,
    routes: tuple[str, ...] = ROUTE_ORDER,
) -> OpenedStream:
    """Open the recording on whichever endpoint the recorder serves it from.

    The caller owns the response and must close it.

    Success is an HTTP 200 and nothing more. The first bytes are deliberately not read
    here: a recorder that is slow to start sending is still the right route, and waiting
    for video to prove it would turn a busy device into a fallback to an endpoint it does
    not have. Every failure seen in the wild is unambiguous well before any video — a 404,
    the connection closed with no response at all, or nothing said until the probe expires.

    `routes` narrows what may be answered with, for a caller that can only use some of
    them: the FLV pass-through can pipe a live stream and not a whole file.
    """
    api = async_get_host(hass, recording.entry_id).api
    secrets = api_secrets(api)
    # Not Home Assistant's default session: a recorder's certificate is its own, and
    # verifying it is off unless the user has said otherwise. See tls.py.
    session = async_nvr_session(hass)
    problems: list[str] = []
    prefer = ROUTE_DOWNLOAD if api.is_hub else None

    for route in _async_route_order(hass, recording.entry_id, prefer=prefer):
        if route not in routes:
            continue
        try:
            url = await _BUILDERS[route](api, recording)
        except Exception as err:
            # A route the library cannot even build a URL for is not a route this device
            # has. Recorded and stepped over rather than raised, so the other is still
            # tried.
            problems.append(f"{route}: {scrub_credentials(str(err), secrets=secrets)}")
            _LOGGER.debug("Could not build the %s URL for %s", route, recording.label)
            continue

        try:
            # The probe bounds the wait for an answer; STREAM_TIMEOUT then bounds the
            # gaps between chunks of the answer, which is a much longer thing to allow.
            async with asyncio.timeout(PROBE_SECONDS):
                upstream = await session.get(url, timeout=STREAM_TIMEOUT)
        except TimeoutError:
            problems.append(f"{route}: no answer within {PROBE_SECONDS}s")
            continue
        except Exception as err:
            problems.append(f"{route}: {scrub_credentials(str(err), secrets=secrets)}")
            continue

        if upstream.status == 200:
            async_remember_route(hass, recording.entry_id, route)
            return OpenedStream(upstream, route, url)

        upstream.release()
        problems.append(f"{route}: HTTP {upstream.status}")

    raise PlaybackRouteError(
        "The recorder would not serve this recording on any playback endpoint "
        f"({'; '.join(problems)})"
    )


@callback
def async_playback_input_seek(hass: HomeAssistant, recording: Recording) -> int:
    """Return an ffmpeg input seek when the device cannot seek its own playback stream.

    Whole files arrive from their beginning however far in playback should start, so the
    seek that the other two routes hand to the recorder has to be done locally instead.
    Read from what was remembered, so it must be called after the source URL, which is
    what measures.
    """
    if async_remembered_route(hass, recording.entry_id) == ROUTE_DOWNLOAD:
        return recording.seek
    return recording.seek if async_get_host(hass, recording.entry_id).api.is_hub else 0


async def async_playback_route(hass: HomeAssistant, recording: Recording) -> str:
    """Return the route this recorder serves the recording from, measuring if need be.

    Home Hubs are known in advance, so they are answered without a request. Everything
    else is measured once and remembered, which is the whole point of the module.
    """
    api = async_get_host(hass, recording.entry_id).api
    if api.is_hub:
        return ROUTE_DOWNLOAD

    remembered = async_remembered_route(hass, recording.entry_id)
    if remembered is not None:
        return remembered

    opened = await async_open_playback_stream(hass, recording)
    # Measured, not consumed: nothing here wants these bytes, and holding the connection
    # would leave the recorder sending to nobody.
    opened.response.close()
    return opened.route


async def async_playback_is_file(hass: HomeAssistant, recording: Recording) -> bool:
    """Whether this recording arrives as a whole file rather than a live-paced stream.

    What the panel needs to know before it is handed a path: a file is given to the video
    element directly and seeked in the browser, a stream is piped through the FLV view and
    seeked by reopening it.
    """
    return await async_playback_route(hass, recording) == ROUTE_DOWNLOAD


async def async_playback_source(hass: HomeAssistant, recording: Recording) -> str:
    """Return a URL for the recording, on a route the recorder is known to answer.

    For ffmpeg, which is handed a URL rather than an open socket. A recorder with neither
    real-time endpoint — a Home Hub, an RLN36 — ends up on its whole-file Download route,
    and `async_playback_input_seek` is then what starts playback in the right place.
    """
    route = await async_playback_route(hass, recording)
    return await async_route_url(hass, recording, route)
