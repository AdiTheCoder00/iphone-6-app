"""Direct TP-Link Kasa/Tapo control over the local network, via python-kasa.

The alternative was Home Assistant as a hub (see home_assistant.py), which is
still supported — but HA does not run natively on Windows, so on this machine
it would mean installing Docker Desktop just to reach two smart plugs. This
path talks to the devices directly on the LAN with no extra service at all.

AUTHENTICATION: newer Kasa firmware and all Tapo devices use TP-Link's KLAP
protocol, which needs your TP-Link *account* email and password. Those are
used to derive the local handshake — control still happens entirely on the
LAN, nothing is routed through TP-Link's cloud — but they are real account
credentials sitting in .env, so treat that file accordingly. Older Kasa
devices ignore them and work unauthenticated.
"""

import asyncio
import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

# Discovery is a multi-second network broadcast, far too slow to run on every
# "turn on the lamp". The device map is cached and only re-scanned when stale,
# so a repeat command is effectively instant.
_DISCOVERY_TTL_SECONDS = 120.0

# A failed scan (dead network, offline plug) must not re-pay the full 6s
# discovery on every /health probe; failures are cached for a short window
# too. Empty-but-successful scans ride the same window via _cached_at.
_NEGATIVE_TTL_SECONDS = 20.0

_cache: dict[str, object] = {}
_cached_at = 0.0
_last_error: str | None = None
_last_error_at = 0.0
# Serialises discovery: without it, two concurrent tool calls on a cold cache
# would each kick off their own full LAN scan.
_discovery_lock = asyncio.Lock()


class TPLinkError(RuntimeError):
    """Discovery or control failed."""


def _credentials():
    from kasa import Credentials

    if not settings.tplink_username or not settings.tplink_password:
        return None
    return Credentials(
        username=settings.tplink_username, password=settings.tplink_password
    )


async def _discover(force: bool = False) -> dict[str, object]:
    """Return {alias: device}, re-scanning only when the cache is stale."""
    global _cache, _cached_at, _last_error, _last_error_at

    async with _discovery_lock:
        if not force and _last_error and (
            time.monotonic() - _last_error_at < _NEGATIVE_TTL_SECONDS
        ):
            raise TPLinkError(_last_error)

        fresh = time.monotonic() - _cached_at < _DISCOVERY_TTL_SECONDS
        # Timestamp-based, not dict-based: an empty result ({}) must also be
        # cached, or a scan that finds nothing re-runs on every call.
        if _cached_at and fresh and not force:
            return _cache

        from kasa import Discover

        try:
            found = await Discover.discover(
                credentials=_credentials(), discovery_timeout=6
            )
        except Exception as e:
            _last_error, _last_error_at = (
                f"device discovery failed ({e})",
                time.monotonic(),
            )
            raise TPLinkError(_last_error) from e

        devices: dict[str, object] = {}
        auth_failures = 0
        for addr, device in found.items():
            try:
                await device.update()
            except Exception as e:
                # An unauthenticated device is the expected failure when no
                # credentials are set, and is worth reporting distinctly from
                # a device that is simply offline.
                if "auth" in type(e).__name__.lower() or "Authentication" in str(e):
                    auth_failures += 1
                else:
                    logger.info("Skipping TP-Link device at %s: %s", addr, e)
                continue
            alias = getattr(device, "alias", None) or addr
            devices[alias] = device

        if not devices and auth_failures:
            _last_error, _last_error_at = (
                f"{auth_failures} device(s) found but rejected the credentials — "
                "check TPLINK_USERNAME and TPLINK_PASSWORD in backend/.env",
                time.monotonic(),
            )
            raise TPLinkError(_last_error)

        _cache, _cached_at = devices, time.monotonic()
        _last_error = None
        return devices


async def list_devices() -> list[dict]:
    """Controllable devices and their current state.

    Same shape as home_assistant.list_devices() so the tool layer above does
    not care which backend is in use.
    """
    devices = await _discover()
    out = []
    for alias, device in devices.items():
        is_on = getattr(device, "is_on", None)
        out.append(
            {
                "entity_id": alias,
                "name": alias,
                # Best-effort domain so the UI can label it; python-kasa's
                # device_type is an enum whose str() is like "DeviceType.Plug".
                "domain": str(getattr(device, "device_type", "device")).split(".")[-1].lower(),
                "state": "on" if is_on else "off",
            }
        )
    return out


async def set_state(entity_id: str, turn_on: bool) -> None:
    devices = await _discover()
    device = devices.get(entity_id)
    if device is None:
        # A renamed or newly added device would otherwise stay invisible for
        # the whole cache window.
        devices = await _discover(force=True)
        device = devices.get(entity_id)
    if device is None:
        raise TPLinkError(f"no device called '{entity_id}'")

    try:
        if turn_on:
            await device.turn_on()
        else:
            await device.turn_off()
        await device.update()
    except Exception as e:
        raise TPLinkError(f"could not switch '{entity_id}' ({e})") from e


async def is_available() -> bool:
    """Cheap probe for /health. Never raises."""
    if settings.smart_home_provider != "kasa":
        return False
    try:
        return len(await _discover()) > 0
    except TPLinkError:
        return False
