"""How this integration talks TLS to a recorder.

Reolink recorders ship a self-signed certificate and serve HTTPS out of the box. Some
installations replace it — a recorder given a name and a certificate from an internal CA
verifies perfectly well, and an installation that has done that work should get the benefit
of it. Many do not, and that is the shape the device arrives in. `reolink_aio` assumes the
latter and pins `ssl.CERT_NONE` on the session it makes its own calls over, so every
request the official Reolink integration sends — login, search, snapshots — reaches the
recorder without its certificate being checked, whatever certificate it is carrying.

The two requests this integration makes for itself did not. They went over Home
Assistant's shared client session, which verifies, so a recorder that answered `cmd=Search`
happily would then refuse to serve the recording that search had just found:

    Cannot connect to host 192.168.x.x:443 ssl:True [SSLCertVerificationError:
    (1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed
    certificate)]

Verification is therefore off by default here, which matches the bar the library has
already set rather than lowering it: the credentials a playback URL carries travel over
the connection reolink_aio has already made unverified. A default that verified would
refuse to play anything on a recorder still holding its factory certificate, which is not
a security posture but a playback bug.

It is an option and not a constant because that default is the wrong answer for an
installation that has put a certificate on its recorder and wants it checked. Turning it
on applies to every path this integration reaches the recorder by — both aiohttp requests
and both ffmpeg inputs — so the answer is one setting rather than a per-feature surprise.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_VERIFY_TLS, DOMAIN


@callback
def async_verify_tls(hass: HomeAssistant) -> bool:
    """Whether the recorder's certificate should be checked.

    Read live rather than passed down from setup, because the callers are views and a
    background syncer that outlive any one option read. Falls back to the default while the
    integration is not loaded, which is what the two ffmpeg paths see during a reload.
    """
    data = hass.data.get(DOMAIN)
    if data is None:
        return DEFAULT_VERIFY_TLS
    return bool(data.options.verify_tls)


@callback
def async_nvr_session(hass: HomeAssistant) -> ClientSession:
    """Return the shared session to talk to a recorder over.

    Home Assistant keeps one session per verification setting, so this is the same shared,
    self-closing session as everywhere else — not a new one per request.
    """
    return async_get_clientsession(hass, verify_ssl=async_verify_tls(hass))


def ffmpeg_tls_args(source_url: str, *, verify_tls: bool) -> list[str]:
    """Return the input options that make ffmpeg agree with the setting above.

    Nothing at all in the default case: ffmpeg's own `tls_verify` is already 0, and the
    option is not worth the risk of a build that does not recognise it. Only a user who has
    asked for verification pays that, and finds out immediately if their ffmpeg is too old.

    Plain HTTP gets nothing either way — there is no certificate to have an opinion about.
    """
    if not verify_tls or urlsplit(source_url).scheme != "https":
        return []
    return ["-tls_verify", "1"]
