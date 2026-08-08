"""Tests for how this integration treats the recorder's certificate.

The bug this exists for was one-sided: every call the Reolink integration makes reaches the
recorder over `ssl.CERT_NONE`, and the two this integration makes for itself went over Home
Assistant's verifying session instead. A recorder with its factory certificate answered the
search and then refused to serve what the search had found.

So what is asserted here is that the setting reaches *every* way of addressing the recorder
— both aiohttp requests and both ffmpeg inputs — and that it reaches them in both
directions: off is the default, on is honoured for an installation that has a certificate
worth checking.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.reolink_stamina.const import CONF_VERIFY_TLS, DOMAIN
from custom_components.reolink_stamina.restream import FORMAT_MP4, MODE_COPY, build_args
from custom_components.reolink_stamina.tls import (
    async_nvr_session,
    async_verify_tls,
    ffmpeg_tls_args,
)

from .conftest import FakeApi, FakeHost

_HTTPS = "https://nvr/cgi-bin/api.cgi?cmd=Playback&token=T"
_HTTP = "http://nvr/cgi-bin/api.cgi?cmd=Playback&token=T"


@pytest.fixture
def setup_stamina(hass: HomeAssistant):
    """Set the panel up with the certificate option in a given position."""

    async def _setup(verify: bool) -> MockConfigEntry:
        reolink = MockConfigEntry(domain="reolink", title="Backyard NVR")
        reolink.add_to_hass(hass)
        reolink.runtime_data = SimpleNamespace(host=FakeHost(FakeApi()))
        reolink.mock_state(hass, ConfigEntryState.LOADED)

        entry = MockConfigEntry(domain=DOMAIN, options={CONF_VERIFY_TLS: verify})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


# ------------------------------------------------------------------------- the setting


async def test_a_recorders_certificate_is_not_verified_by_default(
    hass: HomeAssistant, setup_stamina
) -> None:
    """The factory certificate cannot pass, and reolink_aio does not ask it to."""
    await setup_stamina(False)
    assert async_verify_tls(hass) is False


async def test_an_installation_with_a_real_certificate_can_ask_for_it(
    hass: HomeAssistant, setup_stamina
) -> None:
    """Turning the option on is the whole point of it being an option."""
    await setup_stamina(True)
    assert async_verify_tls(hass) is True


async def test_an_unloaded_integration_answers_with_the_default(hass: HomeAssistant) -> None:
    """The ffmpeg paths outlive a reload, and must not raise while one is in progress."""
    assert async_verify_tls(hass) is False


# ------------------------------------------------------------------------ aiohttp side


async def test_the_recorder_session_is_the_one_that_does_not_verify(
    hass: HomeAssistant, setup_stamina
) -> None:
    """Requests to the recorder must not go over Home Assistant's verifying session."""
    await setup_stamina(False)
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    assert async_nvr_session(hass) is not async_get_clientsession(hass)
    assert async_nvr_session(hass) is async_get_clientsession(hass, verify_ssl=False)


async def test_verifying_puts_the_recorder_back_on_the_shared_session(
    hass: HomeAssistant, setup_stamina
) -> None:
    """With the option on there is nothing special about these requests any more."""
    await setup_stamina(True)
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    assert async_nvr_session(hass) is async_get_clientsession(hass)


# ------------------------------------------------------------------------- ffmpeg side


def test_ffmpeg_is_told_nothing_when_nothing_needs_saying() -> None:
    """The ffmpeg default is already not to verify, so nothing is added for a build to reject."""
    assert ffmpeg_tls_args(_HTTPS, verify_tls=False) == []


def test_ffmpeg_is_told_to_verify_when_the_option_says_so() -> None:
    """The option would be a lie if it only reached the aiohttp half."""
    assert ffmpeg_tls_args(_HTTPS, verify_tls=True) == ["-tls_verify", "1"]


def test_plain_http_has_no_certificate_to_have_an_opinion_about() -> None:
    """A recorder on port 80 gets the same command either way."""
    assert ffmpeg_tls_args(_HTTP, verify_tls=True) == []


def test_the_tls_flag_reaches_ffmpeg_before_the_input_it_describes() -> None:
    """An input option after `-i` applies to the output, which is nothing at all here."""
    args = build_args("ffmpeg", _HTTPS, mode=MODE_COPY, output_format=FORMAT_MP4, verify_tls=True)

    assert args.index("-tls_verify") < args.index("-i")
    assert args[args.index("-tls_verify") + 1] == "1"


def test_the_conversion_command_is_unchanged_while_the_option_is_off() -> None:
    """Nothing about the default path moves, so nothing about it can regress."""
    assert build_args("ffmpeg", _HTTPS, mode=MODE_COPY, output_format=FORMAT_MP4) == build_args(
        "ffmpeg", _HTTPS, mode=MODE_COPY, output_format=FORMAT_MP4, verify_tls=False
    )
    assert "-tls_verify" not in build_args(
        "ffmpeg", _HTTPS, mode=MODE_COPY, output_format=FORMAT_MP4
    )
