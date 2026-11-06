"""Awaitable Store writes that report failure to their caller.

Home Assistant 2026.11's Store.async_save logs and suppresses WriteError and
SerializationError, and defers writes while stopping. That behavior is useful
for ordinary cached state, but unsafe before acknowledging an encrypted key
or an offline command queue as persisted. The one private hook below captures
the original write failure so the public async_save can propagate it. Review
this adapter when Home Assistant changes its Store implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

_T = TypeVar("_T", bound=Mapping[str, Any] | Sequence[Any])


class StrictStore(Store[_T]):
    """Store-compatible adapter whose awaited writes do not silently fail."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._strict_save_lock = asyncio.Lock()
        self._strict_write_error: Exception | None = None

    async def async_save(self, data: _T) -> None:
        """Wait for a write, or raise instead of claiming it was persisted."""
        async with self._strict_save_lock:
            if self.hass.state is CoreState.stopping:
                raise HomeAssistantError("Cannot persist TelePool data while Home Assistant is stopping")
            if self._read_only:
                raise HomeAssistantError("Cannot persist TelePool data in a read-only store")
            self._strict_write_error = None
            await super().async_save(data)
            if self._strict_write_error is not None:
                error = self._strict_write_error
                self._strict_write_error = None
                raise error

    async def _async_write_data(self, data: dict) -> None:
        """Capture errors before HA's write handler suppresses them (2026.11)."""
        try:
            await super()._async_write_data(data)
        except Exception as error:
            self._strict_write_error = error
            raise
