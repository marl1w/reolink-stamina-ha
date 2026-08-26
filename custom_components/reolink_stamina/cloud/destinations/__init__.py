"""Where clips go, and how they get there.

One class per provider, all of them satisfying the same three-method interface in `base`, so
the syncer never learns which one it has. Adding another is a file here and a line in the
table below.

Every provider rides on an integration Home Assistant already has: the account, address or
key is entered once, in the place that owns it, and this borrows the session. That is why
connecting a destination needs no second login — and why the list of what a user may choose
is simply the list of those integrations they have set up.

Which class serves a subentry is read from its destination config entry's own domain rather
than stored alongside it. There is then no second copy to disagree with the first, and no
migration for the syncers that predate every provider but OneDrive: their entry has always
said `onedrive`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .base import Destination, DestinationError, UploadTooLargeError
from .google_drive import GoogleDriveDestination
from .onedrive import OneDriveDestination
from .sftp import SftpDestination
from .synology import SynologyDestination
from .webdav import WebDavDestination

__all__ = [
    "DESTINATION_DOMAINS",
    "Destination",
    "DestinationError",
    "GoogleDriveDestination",
    "OneDriveDestination",
    "SftpDestination",
    "SynologyDestination",
    "UploadTooLargeError",
    "WebDavDestination",
    "async_create_destination",
]

_PROVIDERS: Final[dict[str, Callable[[HomeAssistant, ConfigEntry], Destination]]] = {
    "onedrive": OneDriveDestination,
    "google_drive": GoogleDriveDestination,
    "synology_dsm": SynologyDestination,
    "webdav": WebDavDestination,
    "sftp_storage": SftpDestination,
}

# The integrations a destination may be chosen from, in the order the form offers them:
# the two that are somebody's NAS first, then the two clouds, then the general case.
DESTINATION_DOMAINS: Final[tuple[str, ...]] = (
    "synology_dsm",
    "webdav",
    "sftp_storage",
    "onedrive",
    "google_drive",
)


def async_create_destination(hass: HomeAssistant, entry: ConfigEntry) -> Destination:
    """Return the destination for one config entry, whichever integration owns it."""
    provider = _PROVIDERS.get(entry.domain)
    if provider is None:
        raise DestinationError(f"{entry.domain} is not a destination Stamina can write to")
    return provider(hass, entry)
