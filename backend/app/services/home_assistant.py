"""Smart home control, via Home Assistant's REST API.

This backend never talks to a device brand directly. Home Assistant already
speaks local network to TP-Link Kasa/Tapo (and nearly everything else) with no
cloud round-trip once its TP-Link integration is added — so the only
integration point this file needs is HA's own REST API, regardless of what
brand of bulb or plug is actually behind it.

Scope is deliberately narrow: list devices, turn something on/off. No
brightness/color/scenes yet — on/off covers the overwhelming majority of
"turn on the lamp" requests, and the tool surface can grow once this is
proven out, same as everything else in this app.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Only these domains are ever exposed to the model. Home Assistant tracks
# dozens of entity types (automations, scripts, sensors, device trackers...);
# surfacing all of them would let the model "discover" and try to act on
# things far outside "turn my stuff on and off."
CONTROLLABLE_DOMAINS = {"light", "switch"}


class HomeAssistantError(RuntimeError):
    """Home Assistant is unreachable, misconfigured, or rejected the call."""


def _client() -> httpx.AsyncClient:
    # Created per-call rather than held open: this is a low-frequency,
    # low-latency-sensitive path (nobody minds an extra 10ms to toggle a
    # lamp), and it avoids holding a stale connection across a long-running
    # backend process while HA itself restarts or changes address.
    if not settings.ha_token:
        raise HomeAssistantError("no Home Assistant token configured")
    return httpx.AsyncClient(
        base_url=settings.ha_base_url,
        timeout=settings.ha_request_timeout,
        headers={
            "Authorization": f"Bearer {settings.ha_token}",
            "Content-Type": "application/json",
        },
    )


async def list_devices() -> list[dict]:
    """Controllable devices and their current state.

    Returns [{"entity_id": "light.living_room", "name": "Living Room Lamp",
    "domain": "light", "state": "on"}, ...] — only light/switch entities,
    everything else in Home Assistant is invisible to the model.
    """
    try:
        async with _client() as client:
            response = await client.get("/api/states")
            response.raise_for_status()
            states = response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HomeAssistantError("Home Assistant rejected the token") from e
        raise HomeAssistantError(f"Home Assistant returned {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise HomeAssistantError(f"Home Assistant is unreachable ({e})") from e

    devices = []
    for entity in states:
        entity_id = entity.get("entity_id", "")
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if domain not in CONTROLLABLE_DOMAINS:
            continue
        attrs = entity.get("attributes") or {}
        devices.append(
            {
                "entity_id": entity_id,
                "name": attrs.get("friendly_name", entity_id),
                "domain": domain,
                "state": entity.get("state", "unknown"),
            }
        )
    return devices


async def set_state(entity_id: str, turn_on: bool) -> None:
    """Call light.turn_on/off or switch.turn_on/off for one entity."""
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain not in CONTROLLABLE_DOMAINS:
        raise HomeAssistantError(f"'{entity_id}' is not a controllable device")

    service = "turn_on" if turn_on else "turn_off"
    try:
        async with _client() as client:
            response = await client.post(
                f"/api/services/{domain}/{service}", json={"entity_id": entity_id}
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HomeAssistantError("Home Assistant rejected the token") from e
        raise HomeAssistantError(f"Home Assistant returned {e.response.status_code}") from e
    except httpx.HTTPError as e:
        raise HomeAssistantError(f"Home Assistant is unreachable ({e})") from e


async def is_available() -> bool:
    """Cheap liveness probe for /health. Never raises."""
    if not settings.ha_enabled or not settings.ha_token:
        return False
    try:
        async with _client() as client:
            response = await client.get("/api/", timeout=3.0)
            return response.status_code == 200
    except httpx.HTTPError:
        return False
