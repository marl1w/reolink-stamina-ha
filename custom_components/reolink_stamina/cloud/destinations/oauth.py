"""The half of OneDrive and Google Drive that is the same.

Both ride on an integration Home Assistant already has: they borrow that config entry's
OAuth session rather than asking for their own, which is why connecting a cloud here needs
no new credentials, no application registration and no second consent screen. If the
provider already works for backups, it works for clips.

What is left over is one authenticated request with a retry policy, which is identical for
both — a large service throttles under load and produces gateway errors that mean nothing
about the request.
"""

from __future__ import annotations

from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base import (
    MAX_BACKOFF_SECONDS,
    REQUEST_ATTEMPTS,
    Destination,
    DestinationError,
    TransientError,
    async_attempt,
)

# Failures worth trying again rather than reporting.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Read `Retry-After`, which is what a throttled caller is meant to obey."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), MAX_BACKOFF_SECONDS)
    except ValueError:
        # The header may be an HTTP date; the backoff schedule is a fine substitute.
        return None


class OAuthDestination(Destination):
    """A destination reached over HTTP with another integration's OAuth token."""

    # What the service is called in errors and logs. Subclasses set it.
    service: str = "Cloud"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to a loaded config entry belonging to the provider's own integration."""
        self._hass = hass
        self._entry = entry
        self._session: config_entry_oauth2_flow.OAuth2Session | None = None

    @property
    def label(self) -> str:
        """Return the account this uploads to."""
        return f"{self.service} ({self._entry.title})"

    async def _async_token(self) -> str:
        """Return a valid access token, refreshing it the way its owner would.

        The refresh is done through Home Assistant's own session helper, so the token this
        integration uses and the one the provider's integration uses stay the same — and a
        revoked login surfaces as that integration's reauth flow rather than as a mystery
        here.
        """
        if self._entry.state is not ConfigEntryState.LOADED:
            raise DestinationError(f"{self._entry.title} is not loaded")

        if self._session is None:
            try:
                implementation = (
                    await config_entry_oauth2_flow.async_get_config_entry_implementation(
                        self._hass, self._entry
                    )
                )
            except (ValueError, KeyError) as err:
                # `KeyError` is what an entry with no `auth_implementation` at all raises,
                # which is not the same shape of failure as a credential that has gone bad
                # but wants the same answer: this destination cannot be written to, and the
                # rest of the integration carries on without it.
                raise DestinationError(
                    f"{self._entry.title} has no usable credentials: {err}"
                ) from err
            self._session = config_entry_oauth2_flow.OAuth2Session(
                self._hass, self._entry, implementation
            )

        await self._session.async_ensure_token_valid()
        token = self._session.token.get("access_token")
        if not token:
            raise DestinationError(f"{self._entry.title} returned no access token")
        return str(token)

    async def _async_once(
        self,
        method: str,
        url: str,
        *,
        allow_missing: bool,
        data: Any,
        headers: dict[str, str] | None,
        params: dict[str, str] | None,
    ) -> Any:
        """Make one call, classifying its failure as permanent or worth retrying."""
        token = await self._async_token()
        session = async_get_clientsession(self._hass)
        sent = {"Authorization": f"Bearer {token}", **(headers or {})}
        try:
            async with session.request(
                method, url, headers=sent, data=data, params=params
            ) as response:
                if response.status == 404:
                    if allow_missing:
                        return None
                    raise DestinationError(f"{self.service} answered HTTP 404 for {method} {url}")
                if response.status in _RETRY_STATUS:
                    raise TransientError(f"HTTP {response.status}", _retry_after(response))
                if response.status >= 400:
                    detail = (await response.text())[:300]
                    raise DestinationError(
                        f"{self.service} answered HTTP {response.status}: {detail}"
                    )
                if response.content_type == "application/json":
                    return await response.json()
                return await response.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            # The request never got an answer, so it may well get one next time.
            raise TransientError(f"{type(err).__name__}: {err}") from err

    async def _async_request(
        self,
        method: str,
        url: str,
        *,
        allow_missing: bool = False,
        data: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        attempts: int = REQUEST_ATTEMPTS,
    ) -> Any:
        """Make one call, retrying the failures that are worth retrying.

        `allow_missing` turns 404 into None, which is what deleting something already gone
        and listing a folder that does not exist yet both want. It is **off by default**: a
        404 answering a write means the clip was not stored, and treating that as success is
        how a syncer comes to believe it holds footage that is not there.

        `attempts` is for the calls that are not safe to repeat blindly — one that creates
        something named rather than addressed can leave a twin behind.
        """
        return await async_attempt(
            f"{self.service} {method}",
            lambda: self._async_once(
                method,
                url,
                allow_missing=allow_missing,
                data=data,
                headers=headers,
                params=params,
            ),
            attempts=attempts,
        )
