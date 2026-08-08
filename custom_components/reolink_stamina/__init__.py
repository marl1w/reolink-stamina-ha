"""The Reolink Stamina integration.

Registers a sidebar panel for reviewing AI events recorded by Reolink devices. All camera
access goes through the official Reolink integration; this integration stores no
credentials of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration

from .cache import VodCache
from .cloud.destination import OneDriveDestination
from .cloud.devices import async_entry_device_name, async_nvr_identifier, async_nvr_name
from .cloud.engine import NvrSyncer
from .const import (
    BYTES_PER_GB,
    CONF_BETA_ALL_DEVICES,
    CONF_BETA_RELEVANCE,
    CONF_BETA_RESTREAM,
    CONF_BROWSE_STREAM,
    CONF_CLIP_LEAD,
    CONF_CLIP_TAIL,
    CONF_DESTINATION_ENTRY,
    CONF_EVENT_LEAD,
    CONF_HIDE_TIMER,
    CONF_INCLUDE_UNLABELLED,
    CONF_NVR_ENTRY,
    CONF_PRE_ROLL,
    CONF_QUOTA_GB,
    CONF_REMOTE_FOLDER,
    CONF_REQUIRE_ADMIN,
    CONF_SPLIT_MINUTES,
    CONF_SYNC_KINDS,
    CONF_SYNC_LEAD,
    CONF_SYNC_STREAM,
    CONF_SYNC_TAIL,
    CONF_VERIFY_TLS,
    DEFAULT_BETA_ALL_DEVICES,
    DEFAULT_BETA_RELEVANCE,
    DEFAULT_BETA_RESTREAM,
    DEFAULT_BROWSE_STREAM,
    DEFAULT_CLIP_LEAD,
    DEFAULT_CLIP_TAIL,
    DEFAULT_EVENT_LEAD,
    DEFAULT_HIDE_TIMER,
    DEFAULT_INCLUDE_UNLABELLED,
    DEFAULT_PRE_ROLL,
    DEFAULT_QUOTA_GB,
    DEFAULT_REMOTE_FOLDER,
    DEFAULT_REQUIRE_ADMIN,
    DEFAULT_SPLIT_MINUTES,
    DEFAULT_SYNC_KINDS,
    DEFAULT_SYNC_LEAD,
    DEFAULT_SYNC_TAIL,
    DEFAULT_VERIFY_TLS,
    DOMAIN,
    ISSUE_INCOMPATIBLE,
    PANEL_COMPONENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    REOLINK_DOMAIN,
    STATIC_URL,
    SUBENTRY_TYPE_SYNC,
)
from .flv_proxy import ReolinkStaminaFlvView
from .playback_route import async_forget_routes
from .relevance import (
    RelevanceRuntime,
    async_delete as async_delete_journal,
    async_start as async_start_relevance,
    async_stop as async_stop_relevance,
)
from .reolink_registry import async_is_compatible
from .restream import (
    ReolinkStaminaHlsView,
    ReolinkStaminaRestreamView,
    async_shutdown as async_shutdown_restreams,
    async_sweep_sessions,
)
from .websocket_api import async_register as async_register_websocket_api

_LOGGER = logging.getLogger(__name__)

_STATIC_REGISTERED = f"{DOMAIN}_static_registered"
_VIEW_REGISTERED = f"{DOMAIN}_view_registered"
_WS_REGISTERED = f"{DOMAIN}_ws_registered"

# Only forwarded when at least one recorder is syncing, so a panel-only install creates no
# entities at all.
SYNC_PLATFORMS = [Platform.SENSOR, Platform.SWITCH]


@dataclass(slots=True)
class StaminaOptions:
    """Resolved user options."""

    browse_stream: str = DEFAULT_BROWSE_STREAM
    split_minutes: int = DEFAULT_SPLIT_MINUTES
    hide_timer: bool = DEFAULT_HIDE_TIMER
    pre_roll: int = DEFAULT_PRE_ROLL
    require_admin: bool = DEFAULT_REQUIRE_ADMIN
    include_unlabelled: bool = DEFAULT_INCLUDE_UNLABELLED
    event_lead: int = DEFAULT_EVENT_LEAD
    clip_lead: int = DEFAULT_CLIP_LEAD
    clip_tail: int = DEFAULT_CLIP_TAIL
    # Off, and a recorder with its factory certificate needs it to stay that way. See tls.py.
    verify_tls: bool = DEFAULT_VERIFY_TLS
    # Betas. Off is the behaviour this integration has always had.
    beta_restream: bool = DEFAULT_BETA_RESTREAM
    beta_all_devices: bool = DEFAULT_BETA_ALL_DEVICES
    # Off, and while it is off no journal exists and nothing about the household is written
    # down. Switching it on is the consent, which is why there is no quieter way to start it.
    beta_relevance: bool = DEFAULT_BETA_RELEVANCE

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> StaminaOptions:
        """Read options off a config entry, falling back to defaults."""
        options = entry.options
        return cls(
            browse_stream=options.get(CONF_BROWSE_STREAM, DEFAULT_BROWSE_STREAM),
            split_minutes=int(options.get(CONF_SPLIT_MINUTES, DEFAULT_SPLIT_MINUTES)),
            hide_timer=bool(options.get(CONF_HIDE_TIMER, DEFAULT_HIDE_TIMER)),
            pre_roll=int(options.get(CONF_PRE_ROLL, DEFAULT_PRE_ROLL)),
            require_admin=bool(options.get(CONF_REQUIRE_ADMIN, DEFAULT_REQUIRE_ADMIN)),
            include_unlabelled=bool(
                options.get(CONF_INCLUDE_UNLABELLED, DEFAULT_INCLUDE_UNLABELLED)
            ),
            event_lead=int(options.get(CONF_EVENT_LEAD, DEFAULT_EVENT_LEAD)),
            clip_lead=int(options.get(CONF_CLIP_LEAD, DEFAULT_CLIP_LEAD)),
            clip_tail=int(options.get(CONF_CLIP_TAIL, DEFAULT_CLIP_TAIL)),
            verify_tls=bool(options.get(CONF_VERIFY_TLS, DEFAULT_VERIFY_TLS)),
            beta_restream=bool(options.get(CONF_BETA_RESTREAM, DEFAULT_BETA_RESTREAM)),
            beta_all_devices=bool(options.get(CONF_BETA_ALL_DEVICES, DEFAULT_BETA_ALL_DEVICES)),
            beta_relevance=bool(options.get(CONF_BETA_RELEVANCE, DEFAULT_BETA_RELEVANCE)),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the panel."""
        return {
            "browse_stream": self.browse_stream,
            "split_minutes": self.split_minutes,
            "hide_timer": self.hide_timer,
            "pre_roll": self.pre_roll,
            "require_admin": self.require_admin,
            "include_unlabelled": self.include_unlabelled,
            "event_lead": self.event_lead,
            "clip_lead": self.clip_lead,
            "clip_tail": self.clip_tail,
            "verify_tls": self.verify_tls,
            "beta_restream": self.beta_restream,
            "beta_all_devices": self.beta_all_devices,
            "beta_relevance": self.beta_relevance,
        }


@dataclass(slots=True)
class StaminaData:
    """Runtime data for this integration."""

    cache: VodCache
    options: StaminaOptions
    # One syncer per configured recorder, keyed by its subentry id. Empty unless cloud sync
    # has been set up, so an installation that only wants the panel pays nothing for it.
    syncers: dict[str, NvrSyncer] = field(default_factory=dict)
    # None unless the Relevance beta is on, in which case no journal exists at all.
    relevance: RelevanceRuntime | None = None


def _frontend_fingerprint(path: Path) -> str:
    """Return a short digest of the frontend directory's contents.

    Used as the static path segment, so that changing any module changes every module's
    URL. Size and modification time are enough to notice an edit without reading every
    file on every start.
    """
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if not file.is_file():
            continue
        stat = file.stat()
        digest.update(str(file.relative_to(path)).encode())
        digest.update(f"{stat.st_size}:{int(stat.st_mtime)}".encode())
    return digest.hexdigest()[:12]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the panel from a config entry."""
    options = StaminaOptions.from_entry(entry)

    cache = VodCache(hass)
    await cache.async_load()

    data = StaminaData(cache=cache, options=options)
    hass.data[DOMAIN] = data
    await _async_start_syncers(hass, entry)

    if options.beta_relevance:
        data.relevance = await async_start_relevance(
            hass, entry, include_all_devices=options.beta_all_devices
        )

    # Unconditional rather than gated on the beta being on: what was left behind was left
    # behind by whatever the option said at the time, and turning the beta off does not
    # give the space back.
    await async_sweep_sessions(hass)

    if not hass.data.get(_WS_REGISTERED):
        async_register_websocket_api(hass)
        hass.data[_WS_REGISTERED] = True

    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version or "0")

    # The whole directory is served under a URL scoped by the contents of the frontend
    # directory, not just the entry module and not the integration version.
    #
    # The panel is a graph of ES modules that import each other by relative path, so a
    # query string on the entry point busts nothing else: the browser keeps serving cached
    # copies of views/player.js and friends for as long as the cache headers allow. Naming
    # the directory after what is in it means editing any module invalidates every module,
    # with no version to remember to bump — a version that stays still while the code moves
    # is exactly how stale modules got served for days.
    frontend_path = Path(__file__).parent / "frontend"
    fingerprint = await hass.async_add_executor_job(_frontend_fingerprint, frontend_path)
    static_url = f"{STATIC_URL}/{fingerprint}"
    if not hass.data.get(_VIEW_REGISTERED):
        # Streams a recording straight from the recorder to the browser. No subprocess,
        # nothing cached, nothing to leak.
        hass.http.register_view(ReolinkStaminaFlvView())
        # The adaptive beta's two routes. Registered whether or not the beta is on — a view
        # cannot be added and removed as an option changes — and both refuse outright while
        # it is off, so the URLs exist but do nothing.
        hass.http.register_view(ReolinkStaminaRestreamView())
        hass.http.register_view(ReolinkStaminaHlsView())
        hass.data[_VIEW_REGISTERED] = True

    if hass.data.get(_STATIC_REGISTERED) != static_url:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(static_url, str(frontend_path), True)]
        )
        hass.data[_STATIC_REGISTERED] = static_url

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        module_url=f"{static_url}/reolink-stamina-panel.js",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=options.require_admin,
        embed_iframe=False,
        config={"options": options.as_dict(), "version": version},
    )

    _async_check_compatibility(hass)

    if hass.data[DOMAIN].syncers:
        await hass.config_entries.async_forward_entry_setups(entry, SYNC_PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_start_syncers(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring up one syncer per configured recorder.

    A subentry that cannot be honoured — its recorder is gone from the Reolink integration,
    or its destination is not loaded — is logged and skipped rather than failing the whole
    integration: the panel and the other recorders should keep working.
    """
    data: StaminaData = hass.data[DOMAIN]
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_SYNC:
            continue
        config = dict(subentry.data)
        nvr_entry_id = config.get(CONF_NVR_ENTRY, "")
        nvr_entry = hass.config_entries.async_get_entry(nvr_entry_id)
        if nvr_entry is None or nvr_entry.domain != REOLINK_DOMAIN:
            _LOGGER.warning(
                "Cloud sync for %s names a Reolink NVR that no longer exists; skipping",
                subentry.title,
            )
            continue
        target = hass.config_entries.async_get_entry(config.get(CONF_DESTINATION_ENTRY, ""))
        if target is None:
            _LOGGER.warning(
                "Cloud sync for %s points at a destination that no longer exists; skipping",
                subentry.title,
            )
            continue

        syncer = NvrSyncer(
            hass,
            entry,
            subentry,
            # From the device registry, not the live API: a recorder that is offline right
            # now still has a name, and the syncer waits for it rather than starting namelessly.
            nvr_name=async_nvr_name(hass, nvr_entry_id) or nvr_entry.title,
            entry_id=nvr_entry_id,
            destination=OneDriveDestination(hass, target),
            kinds=set(config.get(CONF_SYNC_KINDS) or DEFAULT_SYNC_KINDS),
            quota=int(float(config.get(CONF_QUOTA_GB, DEFAULT_QUOTA_GB)) * BYTES_PER_GB),
            folder=config.get(CONF_REMOTE_FOLDER, DEFAULT_REMOTE_FOLDER),
            stream=config.get(CONF_SYNC_STREAM, DEFAULT_BROWSE_STREAM),
            lead=float(config.get(CONF_SYNC_LEAD, DEFAULT_SYNC_LEAD)),
            tail=float(config.get(CONF_SYNC_TAIL, DEFAULT_SYNC_TAIL)),
            nvr_device=async_nvr_identifier(hass, nvr_entry_id),
            destination_device=async_entry_device_name(hass, target.entry_id),
        )
        await syncer.async_start()
        data.syncers[subentry.subentry_id] = syncer


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the panel."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)

    # Whatever the adaptive beta was converting stops here: an ffmpeg left pulling from a
    # recorder is exactly the failure this integration was built to avoid.
    await async_shutdown_restreams(hass)

    data: StaminaData | None = hass.data.get(DOMAIN)
    if data is not None and data.relevance is not None:
        # Before anything else that could fail: whatever is buffered is unrecoverable once
        # the process is gone, and a reload is the most common reason to be here.
        await async_stop_relevance(data.relevance)
        data.relevance = None

    if data is not None and data.syncers:
        await hass.config_entries.async_unload_platforms(entry, SYNC_PLATFORMS)
        for syncer in data.syncers.values():
            await syncer.async_stop()

    # Measured against the firmware that was running, so a reload re-measures rather than
    # carrying an answer that a device update may have invalidated.
    async_forget_routes(hass)

    data = hass.data.pop(DOMAIN, None)
    if data is not None:
        data.cache.async_shutdown()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete what this integration wrote when it is removed.

    The cached search results are a convenience; the journal is a record of when a household
    is in and out. Neither should survive somebody removing the integration, and the second
    one is the reason this is not merely tidiness.
    """
    cache = VodCache(hass)
    await cache.async_clear()
    await async_delete_journal(hass)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change.

    A reload is needed rather than a live update because the sidebar panel's
    admin-only flag is fixed at registration time.
    """
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_check_compatibility(hass: HomeAssistant) -> None:
    """Raise a repair issue if the Reolink integration cannot be read.

    Reaching the live Reolink API means reading `entry.runtime_data`, which is not public
    API, so a clear message beats a stack trace when it moves.
    """
    if async_is_compatible(hass):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_INCOMPATIBLE)
        return

    _LOGGER.warning(
        "The installed Reolink integration does not expose its API the way this panel "
        "expects; event browsing will not work"
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_INCOMPATIBLE,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_INCOMPATIBLE,
    )
