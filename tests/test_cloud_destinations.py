"""Tests for the destinations that are not OneDrive, and for choosing between them.

Each provider is a different shape of the same three questions: did the clip land, is a
missing file an error, and does a folder that does not exist yet read as empty. The answers
have to be the same whichever one a user picked, because the syncer above them cannot tell
which it has — a provider that returns quietly on a failed write makes it believe it holds
footage that is not there.

OneDrive has its own file; this covers the rest.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
import pytest

from custom_components.reolink_stamina.cloud.destinations import (
    DestinationError,
    GoogleDriveDestination,
    OneDriveDestination,
    SftpDestination,
    SynologyDestination,
    WebDavDestination,
    async_create_destination,
)
from custom_components.reolink_stamina.cloud.destinations.synology import PAGE

BASE = "custom_components.reolink_stamina.cloud.destinations.base"

FOLDER = "reolink/Main House"
PATH = f"{FOLDER}/260804_215051_main-nvr_Front Gate.mp4"
CLIP = b"x" * 2048


def loaded_entry(domain: str, **runtime: Any) -> MagicMock:
    """Return a loaded config entry, optionally carrying runtime data."""
    entry = MagicMock(
        domain=domain,
        title=domain,
        entry_id=f"{domain}-entry",
        state=ConfigEntryState.LOADED,
    )
    entry.runtime_data = runtime.get("runtime_data")
    return entry


# --------------------------------------------------------------------- choosing one


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("onedrive", OneDriveDestination),
        ("google_drive", GoogleDriveDestination),
        ("synology_dsm", SynologyDestination),
        ("webdav", WebDavDestination),
        ("sftp_storage", SftpDestination),
    ],
)
async def test_the_provider_is_read_from_the_entry_it_points_at(domain, expected) -> None:
    """Which class serves a syncer is decided by its destination entry's own domain.

    This is what spares every syncer configured before there was a choice: nothing was
    stored to say "OneDrive", and nothing needs to be — their entry has always said so.
    """
    assert isinstance(async_create_destination(MagicMock(), loaded_entry(domain)), expected)


async def test_an_integration_that_is_not_a_destination_is_refused() -> None:
    """Better a named failure at startup than a syncer that fails once per clip."""
    with pytest.raises(DestinationError, match="not a destination"):
        async_create_destination(MagicMock(), loaded_entry("spotify"))


# --------------------------------------------------------------------- WebDAV


def webdav_destination(client: MagicMock) -> WebDavDestination:
    """Return a WebDAV destination over a scripted client."""
    entry = loaded_entry("webdav", runtime_data=client)
    return WebDavDestination(MagicMock(), entry)


def webdav_client(*, size: int | None = None, exists: bool = True) -> MagicMock:
    """Return a client that accepts everything and reports one stored size."""
    client = MagicMock()
    client.check = AsyncMock(return_value=exists)
    client.mkdir = AsyncMock(return_value=True)
    client.upload_iter = AsyncMock()
    client.clean = AsyncMock()
    client.info = AsyncMock(return_value={"size": str(size if size is not None else len(CLIP))})
    client.list_with_infos = AsyncMock(return_value=[])
    return client


async def test_webdav_uploads_the_whole_clip_and_says_how_long_it_is() -> None:
    """A server given no length has to guess at chunking, and some guess wrong."""
    client = webdav_client()
    await webdav_destination(client).async_store(PATH, CLIP)

    _, kwargs = client.upload_iter.call_args
    assert kwargs["content_length"] == len(CLIP)
    args, _ = client.upload_iter.call_args
    assert args[0].read() == CLIP
    assert args[1] == PATH


async def test_webdav_creates_the_folders_it_needs() -> None:
    """WebDAV will not create a parent on upload; an unmade folder is a refused PUT."""
    client = webdav_client(exists=False)
    await webdav_destination(client).async_store(PATH, CLIP)

    assert [call.args[0] for call in client.mkdir.await_args_list] == [
        "reolink",
        "reolink/Main House",
    ]


async def test_a_truncated_webdav_upload_is_a_failure() -> None:
    """A short file the server was happy with is the failure that cannot be noticed."""
    client = webdav_client(size=len(CLIP) // 2)

    with (
        patch(f"{BASE}.asyncio.sleep", new=AsyncMock()),
        pytest.raises(DestinationError, match="not the"),
    ):
        await webdav_destination(client).async_store(PATH, CLIP)


async def test_deleting_a_webdav_clip_already_gone_succeeds() -> None:
    """Eviction must not fail because someone tidied the folder by hand."""
    client = webdav_client(exists=False)
    await webdav_destination(client).async_delete(PATH)

    client.clean.assert_not_awaited()


async def test_listing_a_webdav_folder_that_does_not_exist_yet_is_empty() -> None:
    """A syncer that has never uploaded anything has no folder; that is not an error."""
    client = webdav_client(exists=False)
    assert await webdav_destination(client).async_list(FOLDER) == {}


async def test_webdav_listing_returns_paths_and_sizes_and_skips_folders() -> None:
    """The index is reconciled against this, so the keys have to match stored paths.

    The name comes from each entry's own path rather than its `displayname`, which a server
    is free to leave out — and one that does would otherwise empty this listing, so the
    index would forget every clip and the quota would never evict anything again.
    """
    client = webdav_client()
    client.list_with_infos = AsyncMock(
        return_value=[
            {
                "name": "",
                "path": f"/dav/{FOLDER}/260804_215051_main-nvr_Front Gate.mp4",
                "size": "2048",
                "isdir": "False",
            },
            {"name": "older", "path": f"/dav/{FOLDER}/older/", "size": "", "isdir": "True"},
        ]
    )

    assert await webdav_destination(client).async_list(FOLDER) == {
        f"{FOLDER}/260804_215051_main-nvr_Front Gate.mp4": 2048
    }


async def test_a_webdav_server_that_reports_no_size_is_not_called_a_truncated_upload() -> None:
    """`getcontentlength` is optional, and arrives as an empty string rather than absent.

    Reading that as a length made every upload to such a server a failure, which cost the
    recorder another real-time playback for a clip that was already safely stored.
    """
    client = webdav_client()
    client.info = AsyncMock(return_value={"size": ""})

    await webdav_destination(client).async_store(PATH, CLIP)


# --------------------------------------------------------------------- Synology


def synology_destination(station: MagicMock) -> SynologyDestination:
    """Return a Synology destination over a scripted File Station."""
    entry = loaded_entry(
        "synology_dsm", runtime_data=SimpleNamespace(api=SimpleNamespace(file_station=station))
    )
    return SynologyDestination(MagicMock(), entry)


def dsm_missing() -> Exception:
    """Return the error DSM raises for a file that is not there."""
    return RuntimeError({"code": 900, "details": [{"code": 408}]})


async def test_synology_writes_into_the_share_named_by_the_folder() -> None:
    """File Station addresses from a share down, so the path needs its leading slash."""
    station = MagicMock(upload_file=AsyncMock(return_value=True))
    await synology_destination(station).async_store(PATH, CLIP)

    _, kwargs = station.upload_file.call_args
    assert kwargs["path"] == f"/{FOLDER}"
    assert kwargs["filename"] == "260804_215051_main-nvr_Front Gate.mp4"
    assert kwargs["source"] == CLIP
    assert kwargs["create_parents"] is True


async def test_an_upload_synology_does_not_confirm_is_a_failure() -> None:
    """`success: false` is DSM saying it did not store the clip."""
    station = MagicMock(upload_file=AsyncMock(return_value=False))

    with (
        patch(f"{BASE}.asyncio.sleep", new=AsyncMock()),
        pytest.raises(DestinationError, match="did not confirm"),
    ):
        await synology_destination(station).async_store(PATH, CLIP)


async def test_deleting_a_synology_clip_already_gone_succeeds() -> None:
    """DSM reports a missing file as an error; eviction must read it as done."""
    station = MagicMock(delete_file=AsyncMock(side_effect=dsm_missing()))
    await synology_destination(station).async_delete(PATH)


async def test_a_synology_without_file_station_says_so() -> None:
    """File Station is a package DSM ships but does not always have installed."""
    entry = loaded_entry(
        "synology_dsm", runtime_data=SimpleNamespace(api=SimpleNamespace(file_station=None))
    )

    destination = SynologyDestination(MagicMock(), entry)

    with pytest.raises(DestinationError, match="File Station"):
        assert destination._file_station


async def test_synology_listing_reads_every_page() -> None:
    """A folder longer than one page would otherwise hide clips from the quota.

    The clips it forgets are never evicted, so a quota that looked full stays full and the
    oldest footage is kept for ever at the expense of the newest.
    """

    def file(name: str, size: int) -> SimpleNamespace:
        return SimpleNamespace(is_dir=False, name=name, additional=SimpleNamespace(size=size))

    full = [file(f"clip{index}.mp4", 10) for index in range(PAGE)]
    station = MagicMock(get_files=AsyncMock(side_effect=[full, [file("last.mp4", 20)]]))

    found = await synology_destination(station).async_list(FOLDER)

    assert len(found) == PAGE + 1
    assert found[f"{FOLDER}/last.mp4"] == 20
    assert [call.kwargs["offset"] for call in station.get_files.await_args_list] == [0, PAGE]


async def test_listing_a_synology_folder_that_does_not_exist_yet_is_empty() -> None:
    """A syncer that has never uploaded anything has no folder; that is not an error.

    Listing answers 408 outright where deleting answers the batch failure 900 with the 408
    one level down, so both shapes have to be read as "not there" — otherwise every first
    startup spends four retries on a folder that is simply not made yet.
    """
    station = MagicMock(get_files=AsyncMock(side_effect=RuntimeError({"code": 408})))
    assert await synology_destination(station).async_list(FOLDER) == {}


# --------------------------------------------------------------------- SFTP


def sftp_destination(sftp: MagicMock) -> SftpDestination:
    """Return an SFTP destination over an already-open scripted client."""
    entry = loaded_entry(
        "sftp_storage",
        runtime_data=SimpleNamespace(host="nas", port=22, backup_location="/backups"),
    )
    destination = SftpDestination(MagicMock(), entry)
    destination._sftp = sftp
    return destination


def sftp_client(*, size: int | None = None) -> MagicMock:
    """Return a client that accepts everything and reports one stored size."""
    handle = MagicMock(write=AsyncMock(), close=AsyncMock())
    sftp = MagicMock()
    sftp.makedirs = AsyncMock()
    sftp.open = AsyncMock(return_value=handle)
    sftp.stat = AsyncMock(
        return_value=SimpleNamespace(size=size if size is not None else len(CLIP))
    )
    sftp.exists = AsyncMock(return_value=True)
    sftp.unlink = AsyncMock()
    sftp.isdir = AsyncMock(return_value=True)
    sftp.readdir = AsyncMock(return_value=[])
    return sftp


async def test_sftp_writes_under_the_location_the_integration_validated() -> None:
    """That directory is the one Home Assistant has already proved it can write to."""
    sftp = sftp_client()
    await sftp_destination(sftp).async_store(PATH, CLIP)

    sftp.open.assert_awaited_once_with(f"/backups/{PATH}", "wb")
    sftp.makedirs.assert_awaited_once_with(f"/backups/{FOLDER}", exist_ok=True)


async def test_a_truncated_sftp_write_is_a_failure() -> None:
    """A connection that died mid-transfer leaves a short file the server accepts."""
    sftp = sftp_client(size=len(CLIP) // 2)

    with (
        patch(f"{BASE}.asyncio.sleep", new=AsyncMock()),
        pytest.raises(DestinationError, match="not the"),
    ):
        await sftp_destination(sftp).async_store(PATH, CLIP)


async def test_deleting_an_sftp_clip_already_gone_succeeds() -> None:
    """Eviction must not fail because someone tidied the folder by hand."""
    sftp = sftp_client()
    sftp.exists = AsyncMock(return_value=False)
    await sftp_destination(sftp).async_delete(PATH)

    sftp.unlink.assert_not_awaited()


async def test_sftp_listing_returns_paths_and_sizes_and_skips_folders() -> None:
    """The index is reconciled against this, so the keys have to match stored paths."""
    sftp = sftp_client()
    sftp.readdir = AsyncMock(
        return_value=[
            SimpleNamespace(
                filename="260804_215051_main-nvr_Front Gate.mp4",
                attrs=SimpleNamespace(size=2048, permissions=0o100644),
            ),
            SimpleNamespace(filename="older", attrs=SimpleNamespace(size=0, permissions=0o040755)),
        ]
    )

    assert await sftp_destination(sftp).async_list(FOLDER) == {
        f"{FOLDER}/260804_215051_main-nvr_Front Gate.mp4": 2048
    }


async def test_closing_an_sftp_destination_lets_go_of_the_connection() -> None:
    """One connection is held for the life of the syncer; a reload must not leak it."""
    sftp = sftp_client()
    sftp.exit = MagicMock()
    sftp.wait_closed = AsyncMock()
    destination = sftp_destination(sftp)

    await destination.async_close()

    sftp.exit.assert_called_once()
    assert destination._sftp is None


# --------------------------------------------------------------------- Google Drive


def drive_destination(answers: list[Any]) -> tuple[GoogleDriveDestination, list[dict]]:
    """Return a Drive destination answering from a script, and the calls it made."""
    destination = GoogleDriveDestination(MagicMock(), loaded_entry("google_drive"))
    destination._async_token = AsyncMock(return_value="token")
    calls: list[dict] = []

    async def answer(method, url, *, allow_missing=False, data=None, headers=None, params=None):
        calls.append({"method": method, "url": url, "data": data, "params": params})
        return answers[min(len(calls) - 1, len(answers) - 1)]

    destination._async_request = answer  # type: ignore[method-assign]
    return destination, calls


async def test_drive_replaces_a_clip_of_the_same_name_rather_than_duplicating_it() -> None:
    """Drive allows two files with one name; a retried store would leave an orphan.

    The orphan is invisible to the index, so nothing ever evicts it and the quota it
    occupies is never given back.
    """
    destination, calls = drive_destination(
        [
            {"files": [{"id": "folder-1"}]},  # reolink
            {"files": [{"id": "folder-2"}]},  # Main House
            {"files": [{"id": "existing"}]},  # the clip is already there
            {"id": "existing", "size": str(len(CLIP))},
        ]
    )

    await destination.async_store(PATH, CLIP)

    assert [call["method"] for call in calls] == ["GET", "GET", "GET", "PATCH"]
    assert calls[-1]["url"].endswith("/files/existing")
    assert calls[-1]["data"] == CLIP


async def test_a_truncated_drive_upload_is_a_failure() -> None:
    """A stored length that disagrees with what was sent means a damaged clip."""
    destination, _ = drive_destination(
        [
            {"files": [{"id": "folder-1"}]},
            {"files": [{"id": "folder-2"}]},
            {"files": [{"id": "existing"}]},
            {"id": "existing", "size": "12"},
        ]
    )

    with pytest.raises(DestinationError, match="not the"):
        await destination.async_store(PATH, CLIP)


async def test_a_drive_create_whose_answer_was_lost_does_not_make_a_twin() -> None:
    """Drive keys on ids, so a retried create is a second file of the same name.

    Nothing indexes the twin, so the quota never reclaims it — and eviction, working from
    a name, may delete the real clip and leave the empty one behind.
    """
    destination = GoogleDriveDestination(MagicMock(), loaded_entry("google_drive"))
    destination._async_token = AsyncMock(return_value="token")
    posts = 0

    async def answer(
        method, url, *, allow_missing=False, data=None, headers=None, params=None, attempts=4
    ):
        nonlocal posts
        if method == "POST":
            posts += 1
            assert attempts == 1, "a create must not be blindly repeated"
            raise DestinationError("the answer never came back")
        # The create did happen; the second look finds it.
        return {"files": [{"id": "made-anyway"}] if posts else []}

    destination._async_request = answer  # type: ignore[method-assign]

    assert await destination._async_folder_id("reolink") == "made-anyway"
    assert posts == 1


async def test_listing_a_drive_folder_that_does_not_exist_yet_is_empty() -> None:
    """Reading must not create the folder it went looking for."""
    destination, calls = drive_destination([{"files": []}])

    assert await destination.async_list(FOLDER) == {}
    assert all(call["method"] == "GET" for call in calls), "listing must not write"


async def test_a_camera_with_an_apostrophe_does_not_break_the_drive_query() -> None:
    """`Bill's Gate` is a legal camera name and the end of an unescaped Drive query."""
    destination, calls = drive_destination([{"files": [{"id": "folder-1"}]}, {"files": []}])

    await destination.async_list("reolink/Bill's Gate")

    assert "Bill\\'s Gate" in calls[1]["params"]["q"]
