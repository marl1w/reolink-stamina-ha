"""Tests for keeping credentials out of logs, reports and the panel."""

from __future__ import annotations

from custom_components.reolink_stamina.redact import api_secrets, scrub_credentials

# What ffmpeg says when the `/flv` route will not open: the whole URL, credentials and all.
FFMPEG_SAID = (
    "Error opening input file http://nvr:80/flv?port=1935&app=bcs&stream=playback.bcs"
    "&channel=9&type=1&start=20260807200001&seek=1718&user=admin&password=s3cr&t"
)


def test_a_password_containing_an_ampersand_is_removed_whole() -> None:
    """The failure a pattern that stops at `&` has: the tail stays in plain view.

    These endpoints reject a percent-encoded password, so reolink_aio sends it unencoded
    and whatever is in it arrives verbatim.
    """
    scrubbed = scrub_credentials(FFMPEG_SAID)

    assert "s3cr" not in scrubbed
    assert not scrubbed.endswith("&t")
    assert "password=***" in scrubbed


def test_the_username_goes_too_and_the_diagnosis_stays() -> None:
    """Half a credential is still half a credential; the rest explains the failure."""
    scrubbed = scrub_credentials(FFMPEG_SAID)

    assert "admin" not in scrubbed
    assert "user=***" in scrubbed
    # Scrubbing `user` stops at `&`, so what says *which recording failed* survives.
    assert "start=20260807200001" in scrubbed
    assert "seek=1718" in scrubbed
    assert "channel=9" in scrubbed


def test_a_token_is_a_credential_while_it_lives() -> None:
    """Shorter-lived than a password, but a live one is a live one."""
    scrubbed = scrub_credentials("...&output=x.mp4&token=c131a8bbf74fc0d")
    assert "c131a8bbf74fc0d" not in scrubbed
    assert "output=x.mp4" in scrubbed


def test_literal_secrets_cover_what_no_pattern_can() -> None:
    """A credential quoted without its parameter name is still a credential."""
    said = "Server returned 401 for user admin with p4ssw0rd-with-spaces in it"

    scrubbed = scrub_credentials(said, secrets=("p4ssw0rd-with-spaces",))

    assert "p4ssw0rd-with-spaces" not in scrubbed
    assert "Server returned 401" in scrubbed


def test_a_short_secret_does_not_shred_the_message() -> None:
    """A one-character password would otherwise redact half of every sentence."""
    scrubbed = scrub_credentials("could not open the recording", secrets=("o",))
    assert scrubbed == "could not open the recording"


def test_a_short_secret_is_still_caught_by_name() -> None:
    """Because the pattern matches the parameter, not the value."""
    assert "password=***" in scrub_credentials("...&password=o", secrets=("o",))


def test_nothing_to_scrub_is_left_alone() -> None:
    """The common case: ffmpeg complaining about a codec, not about a URL."""
    said = "Could not find a decoder for stream 0 (hevc)"
    assert scrub_credentials(said) == said


class _FakeApi:
    _password = "s3cr&t"
    _enc_password = "ZW5j"
    _token = "TOK123"


def test_api_secrets_reads_what_the_device_authenticates_with() -> None:
    """All three, because ffmpeg may quote back whichever one the route used."""
    assert api_secrets(_FakeApi()) == ("s3cr&t", "ZW5j", "TOK123")


def test_api_secrets_survives_a_library_that_renamed_them() -> None:
    """A rename should cost the literal pass, not the whole conversion."""
    assert api_secrets(object()) == ()
