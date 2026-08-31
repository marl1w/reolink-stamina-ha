"""Config and options flow for the Reolink Stamina."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.loader import async_get_integration
import voluptuous as vol

from .cloud.destinations import DESTINATION_DOMAINS
from .const import (
    CONF_BROWSE_STREAM,
    CONF_CLIP_LEAD,
    CONF_CLIP_TAIL,
    CONF_DESTINATION_ENTRY,
    CONF_EVENT_LEAD,
    CONF_HIDE_TIMER,
    CONF_NVR_ENTRY,
    CONF_PRE_ROLL,
    CONF_QUOTA_GB,
    CONF_RELEVANCE_SCOPE,
    CONF_RELEVANCE_SENSITIVITY,
    CONF_RELEVANCE_SIGNALS,
    CONF_REMOTE_FOLDER,
    CONF_REQUIRE_ADMIN,
    CONF_SPLIT_MINUTES,
    CONF_SYNC_KINDS,
    CONF_SYNC_LEAD,
    CONF_SYNC_STREAM,
    CONF_SYNC_TAIL,
    CONF_SYNC_UNUSUAL,
    CONF_SYNC_UNUSUAL_KINDS,
    CONF_VERIFY_TLS,
    DEFAULT_BROWSE_STREAM,
    DEFAULT_CLIP_LEAD,
    DEFAULT_CLIP_TAIL,
    DEFAULT_EVENT_LEAD,
    DEFAULT_HIDE_TIMER,
    DEFAULT_PRE_ROLL,
    DEFAULT_QUOTA_GB,
    DEFAULT_RELEVANCE_SCOPE,
    DEFAULT_RELEVANCE_SENSITIVITY,
    DEFAULT_REMOTE_FOLDER,
    DEFAULT_REQUIRE_ADMIN,
    DEFAULT_SPLIT_MINUTES,
    DEFAULT_SYNC_KINDS,
    DEFAULT_SYNC_LEAD,
    DEFAULT_SYNC_TAIL,
    DEFAULT_SYNC_UNUSUAL,
    DEFAULT_SYNC_UNUSUAL_KINDS,
    DEFAULT_VERIFY_TLS,
    DOMAIN,
    PANEL_TITLE,
    RELEVANCE_SCOPES,
    RELEVANCE_SENSITIVITY_FLOORS,
    RELEVANCE_SIGNAL_DOMAINS,
    RELEVANCE_SIGNAL_ENUM_DOMAINS,
    RELEVANCE_SIGNAL_WORLD_CLASSES,
    STREAM_MAIN,
    STREAM_SUB,
    SUBENTRY_TYPE_SYNC,
    SYNC_KIND_CHOICES,
)
from .reolink_registry import async_discover_devices, async_has_configured_nvr
from .signal_picker import async_unhelpful_signals


class ReolinkStaminaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Reolink Stamina.

    Nothing to ask: every device is discovered through the Reolink integration, so setup
    is a single confirmation.
    """

    # The config *entry* schema version, not the release. Bumping it makes Home Assistant
    # demand an `async_migrate_entry` for every entry created at a lower number, and abort
    # setup when there is none. The release version lives in manifest.json.
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm adding the panel."""
        if user_input is not None:
            return self.async_create_entry(title=PANEL_TITLE, data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Suggest the panel when a Reolink recorder turns up on the network.

        The same DHCP matchers the official Reolink integration uses, so the panel is
        offered at the same moment the NVR itself is. There is no way to be told that a
        Reolink config entry was added — this integration is not loaded until it is set
        up — so the network is what we listen to.

        The packet says "a Reolink device", not "an NVR", so the suggestion is withheld
        until the Reolink integration itself holds a working recorder: cameras and hubs
        are out of scope, and an NVR that is not set up there yet has nothing this panel
        could read. Nothing is lost by waiting — the recorder is on the network, so a
        later sighting offers the panel again.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if not async_has_configured_nvr(self.hass):
            return self.async_abort(reason="no_nvr")

        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ReolinkStaminaOptionsFlow:
        """Return the options flow."""
        return ReolinkStaminaOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Cloud sync is configured per recorder, as a subentry of the panel.

        The panel itself is one thing; syncing is several, one per NVR, each with its own
        switch, quota and destination. Subentries give each of those its own device without
        pretending the panel can be installed twice.
        """
        return {SUBENTRY_TYPE_SYNC: CloudSyncSubentryFlow}


class ReolinkStaminaOptionsFlow(OptionsFlow):
    """Adjust how the panel searches and presents recordings."""

    def __init__(self) -> None:
        """Hold the first step's answers while the second one is asked."""
        self._pending: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask what is switched on, in one short page.

        Twelve fields on one form meant scrolling past six numbers to reach the switch you
        came for. Three pages instead: what is on, how the player behaves, and what the
        counting should watch — so the page you want is the one you land on.
        """
        if user_input is not None:
            self._pending = {**self.config_entry.options, **user_input}
            return await self.async_step_player()

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            # Home Assistant labels the button "Submit" unless it is told the flow continues.
            # Three pages all saying Submit read as three chances to finish, and somebody who
            # pressed the first one had no reason to expect two more forms.
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HIDE_TIMER,
                        default=options.get(CONF_HIDE_TIMER, DEFAULT_HIDE_TIMER),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_REQUIRE_ADMIN,
                        default=options.get(CONF_REQUIRE_ADMIN, DEFAULT_REQUIRE_ADMIN),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_VERIFY_TLS,
                        default=options.get(CONF_VERIFY_TLS, DEFAULT_VERIFY_TLS),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_player(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask how recordings are searched, cut and played."""
        if user_input is not None:
            self._pending = {**self._pending, **user_input}
            return await self.async_step_signals()

        options = self._pending

        def seconds(high: int, step: int) -> selector.NumberSelector:
            """Return a seconds box, since five of these differ only by their bounds."""
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=high,
                    step=step,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )

        return self.async_show_form(
            step_id="player",
            # Two pages still to come: what else to count, and how much to mark.
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BROWSE_STREAM,
                        default=options.get(CONF_BROWSE_STREAM, DEFAULT_BROWSE_STREAM),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=STREAM_SUB, label="Low resolution (faster)"
                                ),
                                selector.SelectOptionDict(
                                    value=STREAM_MAIN, label="High resolution"
                                ),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_BROWSE_STREAM,
                        )
                    ),
                    vol.Required(
                        CONF_SPLIT_MINUTES,
                        default=options.get(CONF_SPLIT_MINUTES, DEFAULT_SPLIT_MINUTES),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=60,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_EVENT_LEAD,
                        default=options.get(CONF_EVENT_LEAD, DEFAULT_EVENT_LEAD),
                    ): seconds(300, 5),
                    vol.Required(
                        CONF_CLIP_LEAD,
                        default=options.get(CONF_CLIP_LEAD, DEFAULT_CLIP_LEAD),
                    ): seconds(300, 5),
                    vol.Required(
                        CONF_CLIP_TAIL,
                        default=options.get(CONF_CLIP_TAIL, DEFAULT_CLIP_TAIL),
                    ): seconds(300, 5),
                    vol.Required(
                        CONF_PRE_ROLL,
                        default=options.get(CONF_PRE_ROLL, DEFAULT_PRE_ROLL),
                    ): seconds(60, 1),
                }
            ),
        )

    async def async_step_signals(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what else to count alongside each recorder's detections.

        One picker per recorder, because one Home Assistant often serves more than one
        property: whether anybody is home at the first says nothing about the second.

        Skippable, and it opens empty. Relevance works on time and duration alone — those are
        most of its value — so gating the feature behind a page of configuration would mean
        most people never got past it. And nothing is pre-selected: the model never interprets
        a signal, it counts the state as it finds it, so "is anyone home" serves exactly as
        well as a named person and which one suits a household is not this integration's
        business to assume.
        """
        devices = async_discover_devices(self.hass, include_all_devices=True)
        # Keyed by name rather than by entry id, because the key is what Home Assistant shows
        # as the field's label and an entry id reads as a barcode.
        by_name = {device.name: device.entry_id for device in devices}
        current = dict(self._pending.get(CONF_RELEVANCE_SIGNALS) or {})

        if user_input is not None:
            self._pending[CONF_RELEVANCE_SIGNALS] = {
                entry_id: list(user_input.get(name) or [])
                for name, entry_id in by_name.items()
                if user_input.get(name)
            }
            return await self.async_step_marking()

        # Nothing to pick signals for, but there is still a line to draw and a boundary to
        # draw it within — both of which outlive whichever recorders happen to be loaded
        # right now, so the last page is shown either way.
        if not by_name:
            return await self.async_step_marking()

        unhelpful = async_unhelpful_signals(self.hass)
        # Two filters rather than one list of domains, because `sensor` is admitted only for
        # the enum entities in it. Home Assistant ORs them.
        allowed = [
            selector.EntityFilterSelectorConfig(domain=list(RELEVANCE_SIGNAL_DOMAINS)),
            selector.EntityFilterSelectorConfig(
                domain=list(RELEVANCE_SIGNAL_ENUM_DOMAINS), device_class="enum"
            ),
            # Numbers, but only the ones measuring the world rather than the wiring. "Any
            # sensor with a unit" offered 383 entities on a real installation and all but a
            # handful were voltage and energy counters; this offers 85, and they are the
            # weather station and the room sensors.
            selector.EntityFilterSelectorConfig(
                domain=list(RELEVANCE_SIGNAL_ENUM_DOMAINS),
                device_class=list(RELEVANCE_SIGNAL_WORLD_CLASSES),
            ),
        ]
        return self.async_show_form(
            step_id="signals",
            last_step=False,
            data_schema=vol.Schema(
                {
                    **{
                        vol.Optional(
                            name, default=current.get(entry_id, [])
                        ): selector.EntitySelector(
                            selector.EntitySelectorConfig(
                                filter=allowed,
                                multiple=True,
                                exclude_entities=unhelpful,
                            )
                        )
                        for name, entry_id in by_name.items()
                    }
                }
            ),
        )

    async def async_step_marking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Decide how much gets marked, and what each camera is compared with.

        The two questions on this page are the same question asked from either end. One moves
        the line an event has to clear; the other decides whose history the line is drawn
        from. Neither recounts anything — both are applied when scoring, over a journal that
        was written down without either of them — so changing one is instant and costs no
        history.
        """
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self._pending,
                    CONF_RELEVANCE_SENSITIVITY: user_input.get(
                        CONF_RELEVANCE_SENSITIVITY, DEFAULT_RELEVANCE_SENSITIVITY
                    ),
                    CONF_RELEVANCE_SCOPE: user_input.get(
                        CONF_RELEVANCE_SCOPE, DEFAULT_RELEVANCE_SCOPE
                    ),
                }
            )

        return self.async_show_form(
            step_id="marking",
            last_step=True,
            data_schema=vol.Schema(
                {
                    # Words rather than the quantile behind them. "0.95" is meaningful to
                    # whoever wrote the scorer and to nobody else, and the question somebody
                    # actually has is whether they are seeing too many of these or too few.
                    vol.Required(
                        CONF_RELEVANCE_SENSITIVITY,
                        default=self._pending.get(
                            CONF_RELEVANCE_SENSITIVITY, DEFAULT_RELEVANCE_SENSITIVITY
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(RELEVANCE_SENSITIVITY_FLOORS),
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="relevance_sensitivity",
                        )
                    ),
                    vol.Required(
                        CONF_RELEVANCE_SCOPE,
                        default=self._pending.get(CONF_RELEVANCE_SCOPE, DEFAULT_RELEVANCE_SCOPE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(RELEVANCE_SCOPES),
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="relevance_scope",
                        )
                    ),
                }
            ),
        )


class CloudSyncSubentryFlow(ConfigSubentryFlow):
    """Set up cloud sync for one recorder.

    One subentry per NVR, so a sync device is the counterpart of the recorder device the
    Reolink integration creates: what it covers is stated rather than inferred, and cannot
    change underneath the user because something was moved to another area.
    """

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Ask which recorder, where to put its clips, and how much room they may take."""
        available = self._unsynced_nvrs()
        if not available:
            return self.async_abort(
                reason="no_nvr" if not async_discover_devices(self.hass) else "all_nvrs_configured"
            )
        destinations = await self._async_destinations()
        if not destinations:
            return self.async_abort(reason="no_destination")

        if user_input is not None:
            entry_id = user_input[CONF_NVR_ENTRY]
            return self.async_create_entry(
                title=f"Cloud sync {self._nvr_name(entry_id)}",
                data=user_input,
                unique_id=entry_id,
            )

        first = available[0]["value"]
        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(
                {
                    CONF_NVR_ENTRY: first,
                    CONF_REMOTE_FOLDER: f"{DEFAULT_REMOTE_FOLDER}/{self._nvr_name(first)}",
                },
                destinations,
                choices=available,
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change an existing syncer's settings.

        Which recorder it serves is not among them: a syncer owns the clips it has already
        uploaded, and re-pointing it at another NVR would leave them stranded in a folder
        nothing will ever evict from. Delete it and add the other recorder instead.
        """
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(self._get_entry(), subentry, data_updates=user_input)

        # The destination this syncer already uses stays on the list even when its
        # integration is not loaded — a OneDrive waiting on reauth must not be the reason
        # its quota or folder cannot be changed.
        destinations = await self._async_destinations(
            keep=subentry.data.get(CONF_DESTINATION_ENTRY)
        )
        if not destinations:
            return self.async_abort(reason="no_destination")
        return self.async_show_form(
            step_id="reconfigure", data_schema=self._schema(subentry.data, destinations)
        )

    async def _async_destinations(
        self, *, keep: str | None = None
    ) -> list[selector.SelectOptionDict]:
        """Return every account and server clips could be sent to, across all providers.

        The list is built here rather than left to a config entry selector because that one
        filters to a single integration, which would mean asking for the provider on a page
        of its own before the account could be offered. What the user is actually choosing
        is "where do clips go", and that is one question — so they get one list, each entry
        named for the integration it belongs to.

        `keep` names an entry to list regardless of whether its integration is loaded.
        """
        options: list[selector.SelectOptionDict] = []
        for domain in DESTINATION_DOMAINS:
            entries = self.hass.config_entries.async_loaded_entries(domain)
            if keep is not None and not any(entry.entry_id == keep for entry in entries):
                kept = self.hass.config_entries.async_get_entry(keep)
                if kept is not None and kept.domain == domain:
                    entries = [*entries, kept]
            if not entries:
                continue
            # The integration's own name, so "SFTP Storage" and "Synology DSM" read as
            # they do everywhere else in Home Assistant rather than as domains.
            integration = await async_get_integration(self.hass, domain)
            for entry in entries:
                options.append(
                    selector.SelectOptionDict(
                        value=entry.entry_id, label=f"{integration.name} — {entry.title}"
                    )
                )
        return options

    @callback
    def _unsynced_nvrs(self) -> list[selector.SelectOptionDict]:
        """Return the recorders that do not have a syncer yet, ready for the form.

        Recorders already covered are left out rather than shown and rejected: with one
        subentry per NVR there is nothing useful a second one could mean.
        """
        used = {
            subentry.data.get(CONF_NVR_ENTRY)
            for subentry in self._get_entry().subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_SYNC
        }
        return [
            selector.SelectOptionDict(value=nvr.entry_id, label=nvr.name)
            for nvr in async_discover_devices(self.hass)
            if nvr.entry_id not in used
        ]

    @callback
    def _nvr_name(self, entry_id: str) -> str:
        """Return the recorder's name, as the panel knows it."""
        return next(
            (nvr.name for nvr in async_discover_devices(self.hass) if nvr.entry_id == entry_id),
            "NVR",
        )

    def _schema(
        self,
        current: dict[str, Any] | None,
        destinations: list[selector.SelectOptionDict],
        *,
        choices: list[selector.SelectOptionDict] | None = None,
    ) -> vol.Schema:
        """Build the form, keeping whatever is already set.

        The recorder is only offered while adding — see `async_step_reconfigure`.
        """
        current = current or {}
        fields: dict[Any, Any] = {}
        if choices is not None:
            fields[vol.Required(CONF_NVR_ENTRY, default=current.get(CONF_NVR_ENTRY))] = (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            )
        return vol.Schema(
            {
                **fields,
                vol.Required(
                    CONF_DESTINATION_ENTRY,
                    default=current.get(CONF_DESTINATION_ENTRY, destinations[0]["value"]),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=destinations, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(
                    CONF_QUOTA_GB, default=current.get(CONF_QUOTA_GB, DEFAULT_QUOTA_GB)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=2000,
                        step=1,
                        unit_of_measurement="GB",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SYNC_KINDS, default=list(current.get(CONF_SYNC_KINDS, DEFAULT_SYNC_KINDS))
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SYNC_KIND_CHOICES),
                        multiple=True,
                        translation_key=CONF_SYNC_KINDS,
                    )
                ),
                # The second admission rule. Kept next to the first, because it is the same
                # question — what is worth uploading — asked the other way round.
                vol.Required(
                    CONF_SYNC_UNUSUAL,
                    default=current.get(CONF_SYNC_UNUSUAL, DEFAULT_SYNC_UNUSUAL),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_SYNC_UNUSUAL_KINDS,
                    default=list(current.get(CONF_SYNC_UNUSUAL_KINDS, DEFAULT_SYNC_UNUSUAL_KINDS)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SYNC_KIND_CHOICES),
                        multiple=True,
                        # The same labels as the list above: a kind must not be called two
                        # different things on one form.
                        translation_key=CONF_SYNC_KINDS,
                    )
                ),
                vol.Required(
                    CONF_SYNC_STREAM, default=current.get(CONF_SYNC_STREAM, STREAM_SUB)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=STREAM_SUB, label="Low resolution (recommended)"
                            ),
                            selector.SelectOptionDict(value=STREAM_MAIN, label="High resolution"),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_BROWSE_STREAM,
                    )
                ),
                vol.Required(
                    CONF_SYNC_LEAD, default=current.get(CONF_SYNC_LEAD, DEFAULT_SYNC_LEAD)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=60,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SYNC_TAIL, default=current.get(CONF_SYNC_TAIL, DEFAULT_SYNC_TAIL)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=60,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_REMOTE_FOLDER,
                    default=current.get(CONF_REMOTE_FOLDER, DEFAULT_REMOTE_FOLDER),
                ): selector.TextSelector(),
            }
        )
