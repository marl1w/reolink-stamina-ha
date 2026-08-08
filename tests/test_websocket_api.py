"""Tests for the websocket API.

The behaviour that matters here is the ordering contract: the snapshot must arrive
without waiting on the NVR, and the patch must follow when the search finishes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.reolink_stamina.const import CONF_BETA_RESTREAM, DOMAIN

from .conftest import FakeApi, FakeHost

TODAY = dt_util.now().date()


@pytest.fixture
async def setup_stamina(hass: HomeAssistant):
    """Set up Stamina alongside a fake Reolink NVR."""
    assert await async_setup_component(hass, "http", {})

    api = FakeApi(channels=[0])
    reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(api))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    entry = MockConfigEntry(domain=DOMAIN, title="Reolink Events")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    return SimpleNamespace(api=api, reolink=reolink, entry=entry)


async def test_the_devices_command_lists_the_recorder(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Discovery over the wire, including the options the panel needs."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/devices"})
    response = await client.receive_json()

    assert response["success"]
    assert len(response["result"]["devices"]) == 1
    assert response["result"]["devices"][0]["name"] == "Test NVR"
    assert response["result"]["options"]["browse_stream"] == "sub"
    assert response["result"]["search_window_days"] == 30


async def test_events_snapshot_arrives_before_the_nvr_answers(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """The whole responsiveness design in one test.

    The search is held open, and the snapshot must still arrive — marked as not yet
    loaded, so the panel can show a skeleton instead of a wrong 'nothing recorded'.
    """
    gate = asyncio.Event()

    async def held_search(*args, **kwargs):
        await gate.wait()
        return [], 0

    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        side_effect=held_search,
    ):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/events",
                "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
                "start_date": TODAY.isoformat(),
                "end_date": TODAY.isoformat(),
            }
        )
        assert (await client.receive_json())["success"]

        snapshot = await client.receive_json()
        assert snapshot["event"]["type"] == "snapshot"
        buckets = snapshot["event"]["buckets"]
        assert len(buckets) == 1
        assert buckets[0]["loaded"] is False
        assert buckets[0]["events"] == []

        gate.set()
        await hass.async_block_till_done()


async def test_events_patch_follows_the_search(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """When the NVR answers, exactly that camera-day is pushed."""
    start = dt.datetime(TODAY.year, TODAY.month, TODAY.day, 14, 0, 0)
    files = [
        {
            "start": start.isoformat(),
            "end": (start + dt.timedelta(seconds=30)).isoformat(),
            "start_id": "20260803140000",
            "end_id": "20260803140030",
            "name": "a.mp4",
            "size": 2048,
            "type": "sub",
            "triggers": ["person"],
            "duration": 30.0,
        }
    ]

    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(files, 0)):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/events",
                "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
                "start_date": TODAY.isoformat(),
                "end_date": TODAY.isoformat(),
            }
        )
        assert (await client.receive_json())["success"]
        assert (await client.receive_json())["event"]["type"] == "snapshot"

        patch_message = await client.receive_json()

    assert patch_message["event"]["type"] == "patch"
    bucket = patch_message["event"]["bucket"]
    assert bucket["loaded"] is True
    # The patch that carries the results is also the one that ends the refresh: this is
    # what the toolbar's "Updating…" pill reads, and nothing else follows to clear it.
    assert bucket["updating"] is False
    assert len(bucket["events"]) == 1
    event = bucket["events"][0]
    assert event["triggers"] == ["person"]
    assert event["camera"] == "Camera 0"
    assert event["playable"] is True


async def test_detections_reports_both_the_playhead_and_the_clip_bounds(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Three separate settings, and the player needs all of them.

    `lead` only places the playhead; `clip_lead`/`clip_tail` are what the clip is cut to.
    Conflating them is how "start playback 30s early" would silently become the clip.
    """
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/detections",
            "entry_id": setup_stamina.reolink.entry_id,
            "channel": 0,
            "start": f"{TODAY.isoformat()}T09:00:00+02:00",
            "end": f"{TODAY.isoformat()}T09:05:00+02:00",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["lead"] == 30
    assert response["result"]["clip_lead"] == 15
    assert response["result"]["clip_tail"] == 15


async def test_clip_url_asks_the_recorder_to_cut_the_clip(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """The path points at Reolink's own proxy with both times and NvrDownload."""
    from base64 import urlsafe_b64decode

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/clip_url",
            "entry_id": setup_stamina.reolink.entry_id,
            "channel": 0,
            "stream": "sub",
            "start": "2026-08-04T08:45:12",
            "end": "2026-08-04T08:45:42",
        }
    )
    response = await client.receive_json()

    assert response["success"]
    path = response["result"]["path"]
    assert response["result"]["mime"] == "video/mp4"
    entry_id = setup_stamina.reolink.entry_id
    assert path.startswith(f"/api/reolink/video/{entry_id}/0/sub/NvrDownload/")
    assert (
        urlsafe_b64decode(path.rsplit("/", 1)[-1].encode()).decode()
        == "20260804084512_20260804084542"
    )


async def test_clip_url_rejects_a_window_the_recorder_would_choke_on(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """The device assembles the whole fragment before sending it; keep requests sane."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/clip_url",
            "entry_id": setup_stamina.reolink.entry_id,
            "channel": 0,
            "stream": "sub",
            "start": "2026-08-04T08:00:00",
            "end": "2026-08-04T09:00:00",
        }
    )
    response = await client.receive_json()

    assert not response["success"]
    assert "at most" in response["error"]["message"]


async def test_events_report_how_the_camera_records(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """A handful of short clips in a day is event recording, so nothing gets trimmed."""
    start = dt.datetime(TODAY.year, TODAY.month, TODAY.day, 14, 0, 0)
    files = [
        {
            "start": start.isoformat(),
            "end": (start + dt.timedelta(seconds=30)).isoformat(),
            "start_id": "20260803140000",
            "end_id": "20260803140030",
            "name": "a.mp4",
            "size": 2048,
            "type": "sub",
            "triggers": ["person"],
            "duration": 30.0,
        }
    ]

    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=(files, 0)):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/events",
                "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
                "start_date": TODAY.isoformat(),
                "end_date": TODAY.isoformat(),
            }
        )
        assert (await client.receive_json())["success"]
        assert (await client.receive_json())["event"]["type"] == "snapshot"
        patch_message = await client.receive_json()

    assert patch_message["event"]["bucket"]["events"][0]["continuous"] is False


async def test_events_are_continuous_when_recordings_were_discarded(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """The kept clips look like event recording; the discarded count says otherwise.

    Getting this backwards would let the player trim an event camera's clip and cut into
    the pre-record buffer the panel exists to preserve.
    """
    start = dt.datetime(TODAY.year, TODAY.month, TODAY.day, 14, 0, 0)
    files = [
        {
            "start": start.isoformat(),
            "end": (start + dt.timedelta(seconds=30)).isoformat(),
            "start_id": "20260803140000",
            "end_id": "20260803140030",
            "name": "a.mp4",
            "size": 2048,
            "type": "sub",
            "triggers": ["person"],
            "duration": 30.0,
        }
    ]

    with patch(
        "custom_components.reolink_stamina.cache.async_search_day", return_value=(files, 180)
    ):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/events",
                "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
                "start_date": TODAY.isoformat(),
                "end_date": TODAY.isoformat(),
            }
        )
        assert (await client.receive_json())["success"]
        assert (await client.receive_json())["event"]["type"] == "snapshot"
        patch_message = await client.receive_json()

    assert patch_message["event"]["bucket"]["events"][0]["continuous"] is True


async def test_events_rejects_an_unknown_nvr(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """A stale saved selection must produce an empty snapshot, not an error."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/events",
            "targets": [{"entry_id": "nope", "channel": 0}],
            "start_date": TODAY.isoformat(),
            "end_date": TODAY.isoformat(),
        }
    )
    assert (await client.receive_json())["success"]
    snapshot = await client.receive_json()
    assert snapshot["event"]["buckets"] == []


async def test_events_rejects_a_bad_date(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Malformed input is refused cleanly."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/events",
            "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
            "start_date": "not-a-date",
            "end_date": TODAY.isoformat(),
        }
    )
    response = await client.receive_json()
    assert not response["success"]
    assert response["error"]["code"] == "invalid_format"


async def test_calendar_reports_days_with_recordings(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Used to pre-mark the date picker."""
    with patch(
        "custom_components.reolink_stamina.cache.async_search_calendar",
        return_value=[1, 4, 9],
    ):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/calendar",
                "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
                "year": TODAY.year,
                "month": TODAY.month,
            }
        )
        assert (await client.receive_json())["success"]
        assert (await client.receive_json())["event"]["type"] == "snapshot"
        patch_message = await client.receive_json()

    assert patch_message["event"]["camera"]["days"] == [1, 4, 9]


async def test_non_admin_is_refused_by_default(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token,
    setup_stamina,
) -> None:
    """Recordings are sensitive, so the default is admin-only."""
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": f"{DOMAIN}/devices"})
    response = await client.receive_json()

    assert not response["success"]
    assert response["error"]["code"] == "unauthorized"


async def test_non_admin_is_allowed_when_the_option_is_off(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token,
) -> None:
    """The API must honour the same option as the panel, not its own rule."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Reolink Events", options={"require_admin": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": f"{DOMAIN}/devices"})
    response = await client.receive_json()

    assert response["success"]


async def test_calls_after_unload_fail_cleanly(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """An open panel can outlive an unload; it must not raise a KeyError."""
    client = await hass_ws_client(hass)

    assert await hass.config_entries.async_unload(setup_stamina.entry.entry_id)
    await hass.async_block_till_done()

    await client.send_json_auto_id({"type": f"{DOMAIN}/devices"})
    response = await client.receive_json()

    assert not response["success"]
    assert response["error"]["code"] == "not_found"


async def test_events_searches_one_resolution_only(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Browsing must cost one search per camera-day, not one per resolution.

    Searching the other resolution as well doubled the load on the recorder purely to
    render a quality badge on each row.
    """
    with patch(
        "custom_components.reolink_stamina.cache.async_search_day", return_value=([], 0)
    ) as search:
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/events",
                "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
                "start_date": TODAY.isoformat(),
                "end_date": TODAY.isoformat(),
            }
        )
        assert (await client.receive_json())["success"]
        assert (await client.receive_json())["event"]["type"] == "snapshot"
        await hass.async_block_till_done()

    assert search.call_count == 1
    # And only the browsing resolution was asked for.
    assert search.call_args[0][3] == "sub"


async def test_events_snapshot_has_no_availability_pending_state(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """The 'checking quality' state is gone along with the second search."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/events",
            "targets": [{"entry_id": setup_stamina.reolink.entry_id, "channel": 0}],
            "start_date": TODAY.isoformat(),
            "end_date": TODAY.isoformat(),
        }
    )
    assert (await client.receive_json())["success"]
    bucket = (await client.receive_json())["event"]["buckets"][0]

    assert "availability_pending" not in bucket
    assert "streams_checked" not in bucket


async def test_stream_url_resolves_an_unsearched_resolution(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Playing a resolution the panel never searched resolves it from the time window."""
    start = dt.datetime(TODAY.year, TODAY.month, TODAY.day, 14, 0, 0)
    main_files = [
        {
            "start": start.isoformat(),
            "end": (start + dt.timedelta(seconds=30)).isoformat(),
            "start_id": "20260803140000",
            "end_id": "20260803140030",
            "playback_id": "20260803120000",
            "name": "the-main-file",
            "size": 2048,
            "type": "main",
            "triggers": ["person"],
            "duration": 30.0,
        }
    ]

    with patch(
        "custom_components.reolink_stamina.cache.async_search_day",
        return_value=(main_files, 0),
    ):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/stream_url",
                "entry_id": setup_stamina.reolink.entry_id,
                "channel": 0,
                "stream": "main",
                # No filename: the panel does not know it.
                "start": start.isoformat(),
                "end": (start + dt.timedelta(seconds=30)).isoformat(),
            }
        )
        response = await client.receive_json()

    assert response["success"]
    path = response["result"]["path"]
    # Points at this integration's pass-through view, not at anything transcoded.
    assert path.startswith("/api/reolink_stamina/flv/")
    assert path.endswith("/20260803140000/20260803120000/0")
    assert response["result"]["seekable"] is True


async def test_stream_url_carries_the_seek_offset(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Seeking is server-side: the offset ends up in the path the browser opens."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/stream_url",
            "entry_id": setup_stamina.reolink.entry_id,
            "channel": 0,
            "stream": "sub",
            "filename": "a-file",
            "start_id": "20260803090001",
            "playback_id": "20260803070001",
            "seek": 240,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["path"].endswith("/20260803090001/20260803070001/240")
    assert response["result"]["seek"] == 240


async def test_stream_url_reports_a_missing_resolution(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """A clip that exists in only one resolution must fail clearly, not silently."""
    with patch("custom_components.reolink_stamina.cache.async_search_day", return_value=([], 0)):
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/stream_url",
                "entry_id": setup_stamina.reolink.entry_id,
                "channel": 0,
                "stream": "main",
                "start": "2026-08-03T14:00:00+02:00",
                "end": "2026-08-03T14:00:30+02:00",
            }
        )
        response = await client.receive_json()

    assert not response["success"]
    assert response["error"]["code"] == "not_found"


async def test_stream_url_adds_the_window_offset_to_the_seek(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """The recorder counts from the start of the recording, the panel from the row.

    A 30 minute recording holds several rows. Asking to play the 08:50 row 30 seconds in
    means 1230 seconds into the recording, and getting this wrong replayed the file from
    its beginning regardless of which row was clicked.
    """
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/stream_url",
            "entry_id": setup_stamina.reolink.entry_id,
            "channel": 4,
            "stream": "sub",
            "filename": "1-4-0-01260704063000-00000",
            # The recording's own start, and this row's place inside it.
            "start_id": "20260804083000",
            "playback_id": "20260804063000",
            "offset": 1200,
            "seek": 30,
        }
    )
    response = await client.receive_json()

    assert response["success"]
    # 1200 into the recording, plus 30 into the row.
    assert response["result"]["path"].endswith("/1230")
    # Echoed back row-relative, which is what the player displays.
    assert response["result"]["seek"] == 30


# --------------------------------------------------- the adaptive playback beta


@pytest.fixture
async def setup_beta(hass: HomeAssistant):
    """Set up Stamina with adaptive playback switched on."""
    assert await async_setup_component(hass, "http", {})

    api = FakeApi(channels=[0])
    reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
    reolink.add_to_hass(hass)
    reolink.runtime_data = SimpleNamespace(host=FakeHost(api))
    reolink.mock_state(hass, ConfigEntryState.LOADED)

    entry = MockConfigEntry(
        domain=DOMAIN, title="Reolink Events", options={CONF_BETA_RESTREAM: True}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    return SimpleNamespace(api=api, reolink=reolink, entry=entry)


def _stream_url(target, **extra):
    """Build the command the player sends to open a recording."""
    return {
        "type": f"{DOMAIN}/stream_url",
        "entry_id": target.reolink.entry_id,
        "channel": 0,
        "stream": "sub",
        "filename": "a-file",
        "start_id": "20260803090001",
        "playback_id": "20260803070001",
        **extra,
    }


async def test_a_conversion_is_refused_while_the_beta_is_off(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_stamina
) -> None:
    """Nothing can be made to convert without the option being on, panel or not."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(_stream_url(setup_stamina, route="remux"))
    response = await client.receive_json()

    assert not response["success"]
    assert response["error"]["code"] == "not_supported"


async def test_passthrough_is_still_the_default_with_the_beta_on(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_beta
) -> None:
    """The option makes conversion possible; it does not make it happen."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(_stream_url(setup_beta))
    response = await client.receive_json()

    assert response["success"]
    assert "/flv/" in response["result"]["path"]
    assert response["result"]["mime"] == "video/x-flv"


async def test_a_remux_asks_ffmpeg_only_to_repackage(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_beta
) -> None:
    """The cheap rung has to be addressable, or it can never be the one that is used."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(_stream_url(setup_beta, route="remux", seek=240))
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["path"].startswith("/api/reolink_stamina/restream/copy/")
    assert response["result"]["path"].endswith("/240")
    assert response["result"]["mime"] == "video/mp4"


async def test_a_transcode_says_so_in_its_path(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_beta
) -> None:
    """The view must not have to guess how much work it was asked to do."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(_stream_url(setup_beta, route="transcode"))
    response = await client.receive_json()

    assert response["success"]
    assert response["result"]["path"].startswith("/api/reolink_stamina/restream/encode/")


async def test_an_hls_session_is_started_and_addressed_by_its_token(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_beta
) -> None:
    """What an iPhone is handed has to be a playlist it can fetch unaided.

    iOS gives playback to the system player, which sends none of Home Assistant's
    authentication and resolves each segment against the playlist's own URL — so the
    session is started here and the URL says it must not be signed.
    """
    with patch(
        "custom_components.reolink_stamina.api.playback.async_start_hls",
        return_value="tok123",
    ) as start:
        client = await hass_ws_client(hass)
        await client.send_json_auto_id(
            _stream_url(setup_beta, route="remux", format="hls", seek=90)
        )
        response = await client.receive_json()

    assert response["success"]
    assert response["result"]["path"] == "/api/reolink_stamina/hls/tok123/index.m3u8"
    assert response["result"]["sign"] is False
    assert response["result"]["mime"] == "application/vnd.apple.mpegurl"
    # Repackaging, and starting where the player asked.
    assert start.call_args.args[-1] == "copy"
    assert start.call_args.args[-2] == 90


async def test_an_unknown_route_is_rejected_by_the_schema(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, setup_beta
) -> None:
    """Only the three real routes exist; anything else is a mistake worth naming."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(_stream_url(setup_beta, route="magic"))
    response = await client.receive_json()

    assert not response["success"]
