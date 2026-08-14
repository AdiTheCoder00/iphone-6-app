"""Provider-agnostic smart home facade.

Two backends exist, and the tools and REST endpoints above should not care
which one is active:

  "kasa"           talks to TP-Link Kasa/Tapo directly on the LAN. No extra
                   service to run, but TP-Link only.
  "home_assistant" talks to a Home Assistant instance, which in turn speaks to
                   nearly every brand. Needs HA running somewhere — which on
                   Windows means Docker or a VM.

Both expose the same list_devices()/set_state() shape, so switching is a
config change rather than a code change.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)


class SmartHomeError(RuntimeError):
    """The active provider could not complete the request."""


def provider() -> str:
    """Which backend is configured and usable, or "none"."""
    choice = (settings.smart_home_provider or "none").strip().lower()
    if choice == "kasa":
        return "kasa"
    if choice == "home_assistant" and settings.ha_enabled and settings.ha_token:
        return "home_assistant"
    # Legacy: HA configured without the newer provider setting.
    if choice in ("", "none") and settings.ha_enabled and settings.ha_token:
        return "home_assistant"
    return "none"


async def list_devices() -> list[dict]:
    active = provider()
    if active == "kasa":
        from app.services import tplink

        try:
            return await tplink.list_devices()
        except tplink.TPLinkError as e:
            raise SmartHomeError(str(e)) from e
    if active == "home_assistant":
        from app.services import home_assistant

        try:
            return await home_assistant.list_devices()
        except home_assistant.HomeAssistantError as e:
            raise SmartHomeError(str(e)) from e
    raise SmartHomeError("no smart home provider is configured")


async def set_state(entity_id: str, turn_on: bool) -> None:
    active = provider()
    if active == "kasa":
        from app.services import tplink

        try:
            await tplink.set_state(entity_id, turn_on)
            return
        except tplink.TPLinkError as e:
            raise SmartHomeError(str(e)) from e
    if active == "home_assistant":
        from app.services import home_assistant

        try:
            await home_assistant.set_state(entity_id, turn_on)
            return
        except home_assistant.HomeAssistantError as e:
            raise SmartHomeError(str(e)) from e
    raise SmartHomeError("no smart home provider is configured")


async def is_available() -> bool:
    """Never raises — used by /health."""
    active = provider()
    if active == "kasa":
        from app.services import tplink

        return await tplink.is_available()
    if active == "home_assistant":
        from app.services import home_assistant

        return await home_assistant.is_available()
    return False
