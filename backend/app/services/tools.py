"""Model-facing tools.

Deliberately not an agent framework: a tool is a plain callable plus a name, a
description and a JSON-schema-ish parameter block. The registry exists to render
those three things into the prompt and to dispatch by name — nothing else.

A tool NEVER raises to the caller. Failures come back as a string starting with
"ERROR:", because the model is supposed to read the failure and apologise for it
in its own words rather than have the request 500.
"""

import asyncio
import inspect
import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

# Weather lookups are a user-visible latency cost on every call, so they get a
# much tighter budget than the LLM itself.
WEATHER_TIMEOUT = 8.0
MAX_REMINDER_CHARS = 300

# Verify against the OS trust store rather than certifi's bundle.
#
# api.open-meteo.com serves an incomplete certificate chain. Windows (and so
# curl) transparently fetches the missing intermediate via the AIA extension;
# OpenSSL does not, so certifi-based verification fails with
# "unable to get local issuer certificate" while the same URL works in a
# browser. truststore delegates to the platform verifier, which handles it.
# Falls back to certifi if truststore is unavailable — verification is never
# disabled.
try:
    import truststore

    _SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:  # pragma: no cover - depends on the install
    logger.warning("truststore unavailable; falling back to certifi verification")
    _SSL_CONTEXT = True


@dataclass
class Tool:
    name: str
    description: str
    # JSON-schema-style, rendered verbatim into the prompt.
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    func: Callable[..., Any] = None  # type: ignore[assignment]


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def render_tool_schemas() -> list[dict]:
    """Ollama's native `tools` format — an OpenAI-style function schema per
    tool. Used for real tool-calling; render_tool_specs() below is the older
    prompt-text form, kept only for anything that still wants it."""
    schemas = []
    for tool in _REGISTRY.values():
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            key: {
                                "type": spec.get("type", "string"),
                                "description": spec.get("description", ""),
                                **({"enum": spec["enum"]} if "enum" in spec else {}),
                            }
                            for key, spec in tool.parameters.items()
                        },
                        "required": tool.required,
                    },
                },
            }
        )
    return schemas


def render_tool_specs() -> str:
    """The tool catalogue as it appears in the system prompt."""
    lines = []
    for tool in _REGISTRY.values():
        if tool.parameters:
            params = ", ".join(
                f'"{key}": {spec.get("type", "string")}'
                + (f' [{"/".join(spec["enum"])}]' if "enum" in spec else "")
                + ("" if key in tool.required else " (optional)")
                for key, spec in tool.parameters.items()
            )
            arg_hint = "{" + params + "}"
        else:
            arg_hint = "{}"
        lines.append(f"- {tool.name}: {tool.description}\n  args: {arg_hint}")
    return "\n".join(lines)


async def execute(name: str, args: dict | None) -> str:
    """Dispatch by name. Returns a short string for the model to read."""
    tool = get(name)
    if tool is None:
        known = ", ".join(_REGISTRY) or "none"
        return f"ERROR: no tool named '{name}'. Available tools: {known}"

    args = args if isinstance(args, dict) else {}
    # Drop anything the tool does not declare: a hallucinated extra key would
    # otherwise be a TypeError on call.
    accepted = {k: v for k, v in args.items() if k in tool.parameters}
    missing = [k for k in tool.required if k not in accepted]
    if missing:
        return f"ERROR: {name} is missing required argument(s): {', '.join(missing)}"

    try:
        result = tool.func(**accepted)
        if inspect.isawaitable(result):
            result = await result
        return str(result)
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e, exc_info=True)
        return f"ERROR: {name} failed ({e.__class__.__name__}: {e})"


# --- get_time -----------------------------------------------------------------


def _get_time() -> str:
    # Local wall-clock: this is a desk device and "what time is it" means here.
    now = datetime.now()
    return now.strftime("It is %A, %d %B %Y, %I:%M %p").replace(" 0", " ")


register(
    Tool(
        name="get_time",
        description="Get the current local date and time. Use for any question about what time or day it is.",
        parameters={},
        required=[],
        func=_get_time,
    )
)


# --- get_weather --------------------------------------------------------------

# WMO weather interpretation codes, collapsed to phrases a companion would say.
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "a thunderstorm", 96: "a thunderstorm with hail", 99: "a severe thunderstorm",
}


async def fetch_weather(city: str) -> dict:
    """Raw current conditions. Callers format it for their own audience:
    the tool spells it out for the model to read aloud, the idle screen needs
    something a person can take in at a glance."""
    city = (city or "").strip()
    if not city:
        raise ValueError("needs a city name")

    async with httpx.AsyncClient(timeout=WEATHER_TIMEOUT, verify=_SSL_CONTEXT) as client:
        # Open-Meteo is keyless but coordinate-based, so geocode first.
        geo = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en", "format": "json"},
        )
        geo.raise_for_status()
        results = (geo.json() or {}).get("results") or []
        if not results:
            raise LookupError(f"could not find a place called '{city}'")
        place = results[0]

        forecast = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code",
            },
        )
        forecast.raise_for_status()
        current = (forecast.json() or {}).get("current") or {}

    label = place.get("name", city)
    if place.get("country"):
        label = f"{label}, {place['country']}"

    return {
        "label": label,
        "condition": _WMO.get(current.get("weather_code"), "unsettled"),
        "temp": current.get("temperature_2m"),
        "feels": current.get("apparent_temperature"),
    }


def format_weather_full(w: dict) -> str:
    """For the model: names the place and includes the feels-like."""
    temp, feels = w["temp"], w["feels"]
    summary = f"{w['label']}: {w['condition']}, {round(temp)}°C" if temp is not None \
        else f"{w['label']}: {w['condition']}"
    if feels is not None and temp is not None and abs(feels - temp) >= 2:
        summary += f" (feels like {round(feels)}°C)"
    return summary


def format_weather_compact(w: dict) -> str:
    """For the idle screen: no place name (you know where you are) and no
    feels-like. The full string wrapped to three lines on a 375px screen,
    which is the opposite of glanceable."""
    if w["temp"] is None:
        return w["condition"]
    return f"{w['condition']} · {round(w['temp'])}°C"


async def _get_weather(city: str) -> str:
    try:
        return format_weather_full(await fetch_weather(city))
    except ValueError as e:
        return f"ERROR: get_weather {e}"
    except LookupError as e:
        return f"ERROR: {e}"


register(
    Tool(
        name="get_weather",
        description="Get the current weather for a city. Use whenever the user asks about weather, temperature, or whether to take a coat.",
        parameters={"city": {"type": "string", "description": "City name, e.g. 'Mumbai'"}},
        required=["city"],
        func=_get_weather,
    )
)


# --- set_reminder -------------------------------------------------------------
# The store itself lives in services/reminders.py; this is only the model-facing
# wrapper, so the tool layer stays free of scheduling concerns.


def _set_reminder(text: str, minutes_from_now: int) -> str:
    from app.services.reminders import reminder_service

    text = (text or "").strip()
    if not text:
        return "ERROR: set_reminder needs the reminder text"
    if len(text) > MAX_REMINDER_CHARS:
        return (
            f"ERROR: reminder text is too long ({len(text)} > {MAX_REMINDER_CHARS} chars); "
            "summarise it in one short sentence"
        )
    try:
        minutes = int(minutes_from_now)
    except (TypeError, ValueError):
        return "ERROR: minutes_from_now must be a whole number of minutes"
    if minutes < 0:
        return "ERROR: minutes_from_now cannot be negative"

    reminder = reminder_service.add(text, minutes)
    when = reminder["fire_time_dt"].strftime("%I:%M %p").lstrip("0")
    return f"Reminder set: '{text}' in {minutes} minute(s), at {when}."


register(
    Tool(
        name="set_reminder",
        description="Set a reminder that will alert the user later. Use when the user asks to be reminded of something.",
        parameters={
            "text": {"type": "string", "description": "What to remind the user about"},
            "minutes_from_now": {"type": "integer", "description": "Delay in minutes"},
        },
        required=["text", "minutes_from_now"],
        func=_set_reminder,
    )
)


# --- list_reminders / cancel_reminder -----------------------------------------


def _format_reminder(reminder: dict) -> str:
    when = datetime.fromtimestamp(reminder["fire_time"])
    now = datetime.now()
    stamp = when.strftime("%I:%M %p").lstrip("0")
    if when.date() != now.date():
        stamp = when.strftime("%a ") + stamp
    return f"[{reminder['id']}] {reminder['text']} at {stamp}"


def _list_reminders() -> str:
    from app.services.reminders import reminder_service

    pending = reminder_service.pending()
    if not pending:
        return "No reminders are set."
    return "Pending reminders:\n" + "\n".join(_format_reminder(r) for r in pending)


register(
    Tool(
        name="list_reminders",
        description="List the reminders that are currently set. Use when the user asks what reminders they have, or before cancelling one.",
        parameters={},
        required=[],
        func=_list_reminders,
    )
)


def _cancel_reminder(reminder_id: int | None = None, text: str | None = None) -> str:
    from app.services.reminders import reminder_service

    if reminder_id is not None:
        try:
            rid = int(reminder_id)
        except (TypeError, ValueError):
            return "ERROR: reminder_id must be a number"
        return (
            f"Cancelled reminder {rid}."
            if reminder_service.cancel(rid)
            else f"ERROR: no pending reminder with id {rid}"
        )

    if not text:
        return "ERROR: cancel_reminder needs either reminder_id or text"

    matches = reminder_service.find_pending(text)
    if not matches:
        return f"ERROR: no pending reminder matching '{text}'"
    if len(matches) > 1:
        # Never guess which one they meant — hand the choice back.
        listed = "\n".join(_format_reminder(r) for r in matches)
        return f"Several reminders match '{text}'. Ask which one:\n{listed}"

    reminder = matches[0]
    if reminder_service.cancel(reminder["id"]):
        return f"Cancelled: '{reminder['text']}'."
    return f"ERROR: could not cancel '{reminder['text']}'"


register(
    Tool(
        name="cancel_reminder",
        description="Cancel a reminder, by its id or by roughly what it says. Use when the user wants to call one off.",
        parameters={
            "reminder_id": {"type": "integer", "description": "Id from list_reminders"},
            "text": {"type": "string", "description": "Roughly what the reminder says"},
        },
        required=[],
        func=_cancel_reminder,
    )
)


# --- remember / forget --------------------------------------------------------
# Durable facts about the user. Injected into every system prompt, so the cap
# is what keeps the prompt (and the latency) bounded.

MAX_FACTS = 40


def _remember(fact: str) -> str:
    from app.services.store import store

    fact = (fact or "").strip()
    if not fact:
        return "ERROR: remember needs something to remember"
    if len(fact) > 200:
        return "ERROR: that is too long to remember; summarise it in one short sentence"
    if store.fact_count() >= MAX_FACTS:
        return (
            f"ERROR: already remembering the maximum of {MAX_FACTS} things. "
            "Ask the user what to forget first."
        )

    stored = store.add_fact(fact)
    if stored is None:
        return f"Already knew that: '{fact}'."
    return f"Remembered: '{fact}'."


register(
    Tool(
        name="remember",
        description=(
            "Store something about the user permanently, so you still know it in future "
            "conversations. Use for names, relationships, preferences, routines and "
            "anything they say to remember. Write it as one short third-person sentence."
        ),
        parameters={"fact": {"type": "string", "description": "One short sentence, e.g. 'Their sister is called Priya'"}},
        required=["fact"],
        func=_remember,
    )
)


def _forget(text: str) -> str:
    from app.services.store import store

    needle = (text or "").strip().lower()
    if not needle:
        return "ERROR: forget needs to know what to forget"

    matches = [f for f in store.list_facts() if needle in f["text"].lower()]
    if not matches:
        return f"ERROR: nothing remembered matching '{text}'"
    if len(matches) > 1:
        listed = "\n".join(f"[{f['id']}] {f['text']}" for f in matches)
        return f"Several things match '{text}'. Ask which one:\n{listed}"

    store.delete_fact(matches[0]["id"])
    return f"Forgotten: '{matches[0]['text']}'."


register(
    Tool(
        name="forget",
        description="Forget something previously remembered about the user. Use when they ask you to forget or correct it.",
        parameters={"text": {"type": "string", "description": "Roughly what to forget"}},
        required=["text"],
        func=_forget,
    )
)


# --- local machine control ----------------------------------------------------
# Registered only on Windows, and only when PC_CONTROL_ENABLED. A fixed set of
# named actions; deliberately no arbitrary command execution.

def _register_pc_tools() -> None:
    from app.config import settings
    from app.services import pc_control

    if not settings.pc_control_enabled:
        logger.info("PC control tools disabled by config")
        return
    if not pc_control.IS_WINDOWS:
        logger.info("PC control tools skipped: not running on Windows")
        return

    def _media(action: str) -> str:
        try:
            done = pc_control.media(action)
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return f"Sent '{done}' to whatever is playing."

    register(
        Tool(
            name="control_media",
            description=(
                "Control media playback on the user's PC (whatever app is playing — "
                "Spotify, a browser, a video). Use for pause, resume, skip, previous."
            ),
            parameters={
                "action": {
                    "type": "string",
                    "description": "One of: play_pause, next, previous, stop",
                    "enum": list(pc_control.MEDIA_KEYS),
                }
            },
            required=["action"],
            func=_media,
        )
    )

    def _volume(percent: int) -> str:
        try:
            level = pc_control.set_volume(percent)
        except (pc_control.PCControlError, TypeError, ValueError) as e:
            return f"ERROR: could not set volume ({e})"
        return f"Volume set to {level}%."

    register(
        Tool(
            name="set_volume",
            description="Set the PC's speaker volume to a percentage from 0 to 100. Also unmutes.",
            parameters={"percent": {"type": "integer", "description": "0-100"}},
            required=["percent"],
            func=_volume,
        )
    )

    def _mute(muted: bool = True) -> str:
        try:
            # The model may send a real bool or the string "true".
            flag = muted if isinstance(muted, bool) else str(muted).strip().lower() in ("true", "1", "yes")
            pc_control.set_mute(flag)
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return "Muted." if flag else "Unmuted."

    register(
        Tool(
            name="set_mute",
            description=(
                "Immediately mute or unmute the PC's sound by calling this tool. Saying "
                "'muted' in a reply does not mute anything — only this call does."
            ),
            parameters={"muted": {"type": "boolean", "description": "true to mute, false to unmute"}},
            required=["muted"],
            func=_mute,
        )
    )

    def _status() -> str:
        try:
            level, muted = pc_control.get_volume()
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return f"Volume is {level}%{' (muted)' if muted else ''}."

    register(
        Tool(
            name="get_volume",
            description="Check the PC's current volume level and whether it is muted.",
            parameters={},
            required=[],
            func=_status,
        )
    )

    def _lock() -> str:
        try:
            pc_control.lock_screen()
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return "PC locked."

    register(
        Tool(
            name="lock_pc",
            description=(
                "Lock the user's PC screen. Use only when they clearly ask to lock it, "
                "or say they are stepping away or heading out."
            ),
            parameters={},
            required=[],
            func=_lock,
        )
    )

    async def _get_now_playing() -> str:
        try:
            track = await pc_control.now_playing()
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        if track is None:
            return "Nothing is currently playing."
        status = track["status"]
        who = f"{track['title']} by {track['artist']}" if track["artist"] else track["title"]
        if not who.strip():
            return f"Something is {status}, but no track info is available."
        return f"{who} is {status}."

    register(
        Tool(
            name="get_now_playing",
            description="Check what track, if anything, is currently playing on the PC — title, artist and app.",
            parameters={},
            required=[],
            func=_get_now_playing,
        )
    )

    async def _stats() -> str:
        try:
            # system_stats() samples CPU load over a blocking 0.3s window;
            # off the event loop so it doesn't stall every other in-flight
            # request (including SSE keepalives) for the duration.
            s = await asyncio.to_thread(pc_control.system_stats)
        except Exception as e:
            return f"ERROR: could not read system stats ({e})"
        parts = [f"CPU {round(s['cpu_percent'])}%", f"RAM {round(s['ram_percent'])}%"]
        if s["battery_percent"] is not None:
            plugged = " (charging)" if s["battery_plugged"] else ""
            parts.append(f"battery {s['battery_percent']}%{plugged}")
        return ", ".join(parts) + "."

    register(
        Tool(
            name="get_system_stats",
            description="Check the PC's CPU load, RAM usage, and battery level if it has one.",
            parameters={},
            required=[],
            func=_stats,
        )
    )

    if settings.pc_app_whitelist:

        def _launch(name: str) -> str:
            name = (name or "").strip().lower()
            path = settings.pc_app_whitelist.get(name)
            if path is None:
                known = ", ".join(sorted(settings.pc_app_whitelist)) or "none configured"
                return f"ERROR: '{name}' is not in the app whitelist. Known: {known}"
            try:
                pc_control.launch_app(path)
            except pc_control.PCControlError as e:
                return f"ERROR: {e}"
            return f"Launched {name}."

        register(
            Tool(
                name="launch_app",
                description=(
                    "Open an application on the PC by name, from a fixed list the user has "
                    "already approved. Never invent a name that isn't in the list."
                ),
                parameters={
                    "name": {
                        "type": "string",
                        "description": "One of: " + ", ".join(sorted(settings.pc_app_whitelist)),
                    }
                },
                required=["name"],
                func=_launch,
            )
        )

    if settings.pc_power_control_enabled:

        def _power(action: str, confirm: bool = False) -> str:
            action = (action or "").strip().lower()
            if action not in ("sleep", "shutdown"):
                return f"ERROR: unknown power action '{action}'. Use: sleep, shutdown"

            confirmed = confirm if isinstance(confirm, bool) else str(confirm).strip().lower() in ("true", "1", "yes")
            if not confirmed:
                # Not an error: this is the expected first call. The model is
                # meant to read this as "ask them, then call me again."
                return (
                    f"NEEDS CONFIRMATION: ask the user to confirm they want to {action} "
                    f"the PC. Only call {('power_action')} again with confirm=true after "
                    "they clearly say yes."
                )

            try:
                if action == "sleep":
                    pc_control.sleep_pc()
                    return "PC is going to sleep."
                pc_control.shutdown_pc(settings.pc_shutdown_delay_seconds)
                return (
                    f"Shutting down in {settings.pc_shutdown_delay_seconds} seconds. "
                    "Tell them to save anything open now."
                )
            except pc_control.PCControlError as e:
                return f"ERROR: {e}"

        register(
            Tool(
                name="power_action",
                description=(
                    "Sleep or shut down the PC. This is the riskiest action available — ALWAYS "
                    "ask the user to confirm in plain words first, then call this again with "
                    "confirm=true only once they clearly say yes. Never set confirm=true on the "
                    "first call."
                ),
                parameters={
                    "action": {"type": "string", "description": "sleep or shutdown", "enum": ["sleep", "shutdown"]},
                    "confirm": {"type": "boolean", "description": "true only after the user has explicitly confirmed"},
                },
                required=["action"],
                func=_power,
            )
        )

        def _cancel_power() -> str:
            try:
                pc_control.cancel_shutdown()
            except pc_control.PCControlError as e:
                return f"ERROR: {e}"
            return "Cancelled the pending shutdown, if there was one."

        register(
            Tool(
                name="cancel_shutdown",
                description=(
                    "Abort a shutdown that power_action already scheduled, during its warning "
                    "delay. Use immediately if the user changes their mind or says stop/cancel "
                    "after confirming a shutdown. Safe to call even if nothing is pending — "
                    "does nothing to sleep, which happens instantly and cannot be cancelled."
                ),
                parameters={},
                required=[],
                func=_cancel_power,
            )
        )

    logger.info(
        "PC control tools registered (media, volume, mute, lock, now-playing, stats%s%s)",
        ", launch" if settings.pc_app_whitelist else "",
        ", power, cancel-shutdown" if settings.pc_power_control_enabled else "",
    )


_register_pc_tools()


# --- smart home (Home Assistant) -----------------------------------------------
# Only two tools on purpose: list what exists, and turn something on or off.
# Matching a spoken name to a device follows the same shape as reminders and
# facts — list, then substring-match in either direction — rather than
# expecting the model to know or invent an entity_id.


def _find_devices(devices: list[dict], name: str) -> list[dict]:
    needle = (name or "").strip().lower()
    if not needle:
        return []
    matches = []
    for device in devices:
        haystack = device["name"].lower()
        if needle in haystack or haystack in needle:
            matches.append(device)
    return matches


def _format_device(device: dict) -> str:
    return f"{device['name']} ({device['state']})"


def _register_smart_home_tools() -> None:
    from app.config import settings
    from app.services import home_assistant as ha

    if not settings.ha_enabled:
        logger.info("Smart home tools disabled by config")
        return
    if not settings.ha_token:
        logger.info("Smart home tools skipped: no HA_TOKEN configured")
        return

    async def _list_devices() -> str:
        try:
            devices = await ha.list_devices()
        except ha.HomeAssistantError as e:
            return f"ERROR: {e}"
        if not devices:
            return "No smart home devices found."
        return "Devices:\n" + "\n".join(_format_device(d) for d in devices)

    register(
        Tool(
            name="list_smart_devices",
            description="List the smart home lights and switches, and whether each is on or off.",
            parameters={},
            required=[],
            func=_list_devices,
        )
    )

    async def _control_device(name: str, turn_on: bool) -> str:
        try:
            devices = await ha.list_devices()
        except ha.HomeAssistantError as e:
            return f"ERROR: {e}"

        matches = _find_devices(devices, name)
        if not matches:
            known = ", ".join(d["name"] for d in devices) or "none configured"
            return f"ERROR: no device matching '{name}'. Known devices: {known}"
        if len(matches) > 1:
            listed = "\n".join(_format_device(d) for d in matches)
            return f"Several devices match '{name}'. Ask which one:\n{listed}"

        device = matches[0]
        flag = turn_on if isinstance(turn_on, bool) else str(turn_on).strip().lower() in ("true", "1", "yes")
        try:
            await ha.set_state(device["entity_id"], flag)
        except ha.HomeAssistantError as e:
            return f"ERROR: {e}"
        return f"{device['name']} turned {'on' if flag else 'off'}."

    register(
        Tool(
            name="control_smart_device",
            description=(
                "Turn a smart home light or switch on or off, by roughly what it's called. "
                "Use list_smart_devices first if you're not sure of the exact name."
            ),
            parameters={
                "name": {"type": "string", "description": "Roughly what the device is called"},
                "turn_on": {"type": "boolean", "description": "true to turn on, false to turn off"},
            },
            required=["name", "turn_on"],
            func=_control_device,
        )
    )

    logger.info("Smart home tools registered (list, control)")


_register_smart_home_tools()


__all__ = ["Tool", "execute", "render_tool_specs", "all_tools", "get", "register", "MAX_FACTS"]
