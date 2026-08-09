"""Tests for setup, the sidebar panel and the options flow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.const import (
    CONF_NVR_ENTRY,
    CONF_PRE_ROLL,
    CONF_REMOTE_FOLDER,
    CONF_SPLIT_MINUTES,
    CONF_SYNC_KINDS,
    CONF_SYNC_UNUSUAL,
    CONF_SYNC_UNUSUAL_KINDS,
    DEFAULT_REMOTE_FOLDER,
    DEFAULT_SYNC_KINDS,
    DOMAIN,
    ISSUE_INCOMPATIBLE,
    PANEL_TITLE,
    PANEL_URL_PATH,
    SUBENTRY_TYPE_SYNC,
    SYNC_KIND_CHOICES,
)

from .conftest import FakeApi, FakeHost

_DHCP = DhcpServiceInfo(ip="192.168.1.50", hostname="reolink", macaddress="ec71dbaabbcc")


def _loaded_reolink(hass: HomeAssistant, name: str = "Backyard NVR") -> MockConfigEntry:
    """Add a loaded Reolink entry holding one NVR."""
    api = FakeApi()
    api.nvr_name = name
    entry = MockConfigEntry(domain="reolink", title=name)
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(host=FakeHost(api))
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def _setup(
    hass: HomeAssistant,
    options: dict | None = None,
    subentries: list[ConfigSubentryData] | None = None,
) -> MockConfigEntry:
    """Set up the integration."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Reolink Events",
        options=options or {},
        subentries_data=subentries or [],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_registers_the_sidebar_panel(hass: HomeAssistant) -> None:
    """The panel is the whole point of the integration."""
    await _setup(hass)

    panels = hass.data["frontend_panels"]
    assert PANEL_URL_PATH in panels
    panel = panels[PANEL_URL_PATH]
    assert panel.sidebar_title == PANEL_TITLE
    assert panel.require_admin is True


async def test_unload_removes_the_panel(hass: HomeAssistant) -> None:
    """Removing the integration must not leave a dead sidebar item."""
    entry = await _setup(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert PANEL_URL_PATH not in hass.data["frontend_panels"]
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_survives_a_reload(hass: HomeAssistant) -> None:
    """Static paths can only be registered once; reloading must not break."""
    entry = await _setup(hass)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert PANEL_URL_PATH in hass.data["frontend_panels"]


async def test_options_reach_the_panel(hass: HomeAssistant) -> None:
    """The panel is handed its configuration at registration time."""
    await _setup(hass, {CONF_SPLIT_MINUTES: 2, CONF_PRE_ROLL: 8})

    config = hass.data["frontend_panels"][PANEL_URL_PATH].config
    assert config["options"]["split_minutes"] == 2
    assert config["options"]["pre_roll"] == 8
    # The version lives in the *path*, not a query string, so that the panel's relative
    # imports are invalidated by a release too rather than being served from cache.
    module_url = config["_panel_custom"]["module_url"]
    assert module_url.startswith("/reolink_stamina_static/")
    assert module_url.endswith("/reolink-stamina-panel.js")
    assert "?" not in module_url
    version = module_url.split("/")[2]
    assert version and version != "reolink-stamina-panel.js"
    assert config["_panel_custom"]["name"] == "reolink-stamina-panel"
    assert config["_panel_custom"]["embed_iframe"] is False


async def test_changing_options_reloads_the_entry(hass: HomeAssistant) -> None:
    """The admin-only flag is fixed at registration, so a reload is required."""
    entry = await _setup(hass)

    hass.config_entries.async_update_entry(entry, options={CONF_PRE_ROLL: 12})
    await hass.async_block_till_done()

    config = hass.data["frontend_panels"][PANEL_URL_PATH].config
    assert config["options"]["pre_roll"] == 12


async def test_no_repair_issue_without_reolink(hass: HomeAssistant) -> None:
    """Not having set up an NVR yet is not a problem to report."""
    await _setup(hass)

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_INCOMPATIBLE) is None


async def test_no_repair_issue_with_a_readable_reolink(hass: HomeAssistant) -> None:
    """A healthy Reolink integration must not trip the compatibility check."""
    reolink = MockConfigEntry(domain="reolink", title="NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(FakeApi()))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    await _setup(hass)

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_INCOMPATIBLE) is None


async def test_repair_issue_when_reolink_cannot_be_read(hass: HomeAssistant) -> None:
    """An unreadable Reolink integration surfaces as a repair issue, not a stack trace."""
    reolink = MockConfigEntry(domain="reolink", title="NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(unexpected=True)
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    await _setup(hass)

    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, ISSUE_INCOMPATIBLE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR


async def test_home_assistant_finds_the_brand_icon(hass: HomeAssistant) -> None:
    """The icon is served from `brand/`, and only if Home Assistant sees that directory.

    A contract test rather than a file check: `has_branding` is what decides whether
    `/api/brands/integration/reolink_stamina/icon.png` reads our own icon or falls through
    to the CDN and its placeholder. Renaming the directory would fail here rather than
    silently costing the icon.
    """
    integration = await async_get_integration(hass, DOMAIN)
    brand = Path(integration.file_path) / "brand"

    assert integration.has_branding
    assert (brand / "icon.png").is_file()
    assert (brand / "icon@2x.png").is_file()


async def test_config_flow_creates_an_entry(hass: HomeAssistant) -> None:
    """Setup asks nothing: everything comes from the Reolink integration."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["title"] == PANEL_TITLE


async def test_dhcp_discovery_offers_the_panel_when_an_nvr_exists(hass: HomeAssistant) -> None:
    """A Reolink recorder on the network is what makes the panel worth suggesting."""
    reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(FakeApi()))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "dhcp"}, data=_DHCP
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["title"] == PANEL_TITLE


async def test_dhcp_discovery_stays_quiet_without_an_nvr(hass: HomeAssistant) -> None:
    """Reolink cameras are not recorders: suggesting the panel would be noise."""
    reolink = MockConfigEntry(domain="reolink", title="Doorbell")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(FakeApi(is_nvr=False)))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "dhcp"}, data=_DHCP
    )
    assert result["type"] == "abort"
    assert result["reason"] == "no_nvr"


async def test_dhcp_discovery_waits_for_the_reolink_integration(hass: HomeAssistant) -> None:
    """A recorder on the network is not enough: it must be set up in Reolink first."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "dhcp"}, data=_DHCP
    )
    assert result["type"] == "abort"
    assert result["reason"] == "no_nvr"


async def test_dhcp_discovery_is_silent_once_set_up(hass: HomeAssistant) -> None:
    """Every Reolink device on the network must not re-offer an installed panel."""
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "dhcp"}, data=_DHCP
    )
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"


async def test_only_one_instance_allowed(hass: HomeAssistant) -> None:
    """A second panel would just fight the first one."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"


def _schema_default(result, field: str):
    """Return the default a form offers for one field."""
    for key in result["data_schema"].schema:
        if key.schema == field:
            return key.default()
    raise AssertionError(f"{field} not in the form")


async def test_cloud_sync_offers_the_nvr_and_a_folder_named_after_it(hass: HomeAssistant) -> None:
    """One recorder is the common case, so both fields it decides are filled in already."""
    entry = await _setup(hass)
    reolink = _loaded_reolink(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SYNC), context={"source": "user"}
    )

    assert result["type"] == "form"
    assert _schema_default(result, CONF_NVR_ENTRY) == reolink.entry_id
    assert _schema_default(result, CONF_REMOTE_FOLDER) == f"{DEFAULT_REMOTE_FOLDER}/Backyard NVR"


async def test_cloud_sync_offers_the_unusual_rule_off_and_covering_everything(
    hass: HomeAssistant,
) -> None:
    """The second admission rule: off, and — once switched on — covering every kind.

    Every kind rather than the synced ones, because the point of the rule is the kind you did
    *not* think worth uploading turning out to be worth seeing.
    """
    entry = await _setup(hass)
    _loaded_reolink(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SYNC), context={"source": "user"}
    )

    assert _schema_default(result, CONF_SYNC_UNUSUAL) is False
    assert _schema_default(result, CONF_SYNC_UNUSUAL_KINDS) == list(SYNC_KIND_CHOICES)
    # And the rule it sits beside is untouched: person, vehicle, animal.
    assert _schema_default(result, CONF_SYNC_KINDS) == list(DEFAULT_SYNC_KINDS)


async def test_reconfiguring_a_syncer_keeps_the_unusual_rule_it_was_given(
    hass: HomeAssistant,
) -> None:
    """Both new fields have to survive a trip through the reconfigure form.

    They are added to the shared schema, so a field offered when adding and forgotten when
    reconfiguring would silently reset itself the first time anything else was changed.
    """
    reolink = _loaded_reolink(hass)
    entry = await _setup(
        hass,
        subentries=[
            ConfigSubentryData(
                data={
                    CONF_NVR_ENTRY: reolink.entry_id,
                    CONF_SYNC_UNUSUAL: True,
                    CONF_SYNC_UNUSUAL_KINDS: ["motion", "vehicle"],
                },
                subentry_type=SUBENTRY_TYPE_SYNC,
                title="Cloud sync Backyard NVR",
                unique_id=reolink.entry_id,
            )
        ],
    )
    subentry = next(iter(entry.subentries.values()))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SYNC),
        context={"source": "reconfigure", "subentry_id": subentry.subentry_id},
    )

    assert result["type"] == "form"
    assert _schema_default(result, CONF_SYNC_UNUSUAL) is True
    assert _schema_default(result, CONF_SYNC_UNUSUAL_KINDS) == ["motion", "vehicle"]


async def test_a_recorder_that_already_syncs_is_not_offered_twice(hass: HomeAssistant) -> None:
    """One syncer per recorder: a second would fight the first over the same clips."""
    reolink = _loaded_reolink(hass)
    entry = await _setup(
        hass,
        subentries=[
            ConfigSubentryData(
                data={CONF_NVR_ENTRY: reolink.entry_id},
                subentry_type=SUBENTRY_TYPE_SYNC,
                title="Cloud sync Backyard NVR",
                unique_id=reolink.entry_id,
            )
        ],
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_SYNC), context={"source": "user"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "all_nvrs_configured"


async def test_options_flow_saves_values(hass: HomeAssistant) -> None:
    """The options the panel's behaviour depends on, gathered over the pages they live on.

    Three pages rather than one form of twelve fields: what is switched on, how the player
    behaves, and what the counting should watch alongside a detection. Walking them here is
    the only place the chaining is exercised.
    """
    entry = await _setup(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "init"

    # Page one: what is on.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"hide_timer": False, "require_admin": False, "verify_tls": False},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "player"

    # Page two: the player. No recorder is set up here, so there is nothing to pick signals
    # for and the third page has nothing to ask.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "browse_stream": "main",
            "split_minutes": 3,
            "pre_roll": 6,
            "event_lead": 20,
            "clip_lead": 10,
            "clip_tail": 25,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.options["browse_stream"] == "main"
    assert entry.options["split_minutes"] == 3
    assert entry.options["require_admin"] is False
    # The clip bounds are separate settings from where playback starts.
    assert entry.options["event_lead"] == 20
    assert entry.options["clip_lead"] == 10
    assert entry.options["clip_tail"] == 25


async def test_the_signals_page_is_offered_for_each_recorder(hass: HomeAssistant) -> None:
    """The third page asks what else to count, once there is a recorder to count for."""
    _loaded_reolink(hass)
    entry = await _setup(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"hide_timer": True, "require_admin": True, "verify_tls": False},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "browse_stream": "sub",
            "split_minutes": 5,
            "pre_roll": 5,
            "event_lead": 30,
            "clip_lead": 15,
            "clip_tail": 15,
        },
    )

    assert result["type"] == "form"
    assert result["step_id"] == "signals"

    # Skippable, and it opens empty: the counting is worth having on time alone.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.options["relevance_signals"] == {}


async def test_panel_modules_are_served_under_a_content_scoped_path(
    hass: HomeAssistant,
) -> None:
    """Every module must live under a path that changes when the code does.

    The panel is a graph of ES modules importing each other by relative path, so a query
    string on the entry point busts nothing else. With long cache headers a browser then
    keeps running old copies of the imported modules, which is what made several shipped
    fixes appear to have no effect. The integration version cannot be the key, because it
    deliberately stays still between releases.
    """
    entry = await _setup(hass)

    module_url = hass.data["frontend_panels"][PANEL_URL_PATH].config["_panel_custom"]["module_url"]
    prefix = module_url.rsplit("/", 1)[0]

    assert prefix.startswith("/reolink_stamina_static/")
    assert prefix != "/reolink_stamina_static"
    # Not the version: that is frozen while the code moves.
    integration = await async_get_integration(hass, DOMAIN)
    assert not prefix.endswith(str(integration.version))
    assert entry.state is ConfigEntryState.LOADED


def test_the_fingerprint_follows_the_contents(tmp_path) -> None:
    """Editing any file must change the path, or stale modules get served again."""
    from custom_components.reolink_stamina import _frontend_fingerprint

    (tmp_path / "views").mkdir()
    module = tmp_path / "views" / "player.js"
    module.write_text("one")
    (tmp_path / "api.js").write_text("api")

    before = _frontend_fingerprint(tmp_path)
    assert _frontend_fingerprint(tmp_path) == before  # stable when nothing changes

    module.write_text("a longer body, so the size differs")
    assert _frontend_fingerprint(tmp_path) != before


def test_a_gigabyte_means_the_same_thing_everywhere() -> None:
    """A 15 GB quota must hold 15 GB and report 15 GB free when it is empty.

    Three places have to agree on what a gigabyte is: the label on the form, the multiplier
    that turns the entered number into bytes, and the unit the quota sensors report those bytes
    in. It was GB on the form, 1024**3 in setup and decimal GIGABYTES on the sensors — so a
    quota entered as 15 GB held 16.11 GB and displayed 16.11 as its free space.
    """
    from homeassistant.const import UnitOfInformation

    from custom_components.reolink_stamina.const import BYTES_PER_GB, DEFAULT_QUOTA_GB
    from custom_components.reolink_stamina.sensor import SENSORS

    quota_sensors = [sensor for sensor in SENSORS if sensor.key in ("quota_used", "quota_free")]
    assert len(quota_sensors) == 2
    for sensor in quota_sensors:
        assert sensor.native_unit_of_measurement == UnitOfInformation.BYTES
        # Home Assistant converts to this by 10**9, which is what fixes the multiplier below.
        assert sensor.suggested_unit_of_measurement == UnitOfInformation.GIGABYTES

    assert BYTES_PER_GB == 1000**3
    assert DEFAULT_QUOTA_GB * BYTES_PER_GB == 15_000_000_000
