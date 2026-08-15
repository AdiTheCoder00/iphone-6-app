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
import re
import ssl
import urllib.parse
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


def render_tool_schemas() -> list[dict]:
    """Ollama's native `tools` format — an OpenAI-style function schema per
    tool, used for real tool-calling."""
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


async def _geocode(city: str) -> dict:
    """Resolve a city name to coordinates + label. Shared by the current-
    conditions and daily-forecast fetchers so both agree on the place.

    Common Indian names collide (there are two Gorakhpurs — Uttar Pradesh and
    Haryana); Open-Meteo ranks by population within the response, so take the
    biggest match for the name rather than blindly the first result.
    """
    city = (city or "").strip()
    if not city:
        raise ValueError("needs a city name")
    async with httpx.AsyncClient(
        timeout=WEATHER_TIMEOUT, verify=_SSL_CONTEXT, headers=_USER_AGENT
    ) as client:
        geo = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 5, "language": "en", "format": "json"},
        )
        geo.raise_for_status()
        results = (geo.json() or {}).get("results") or []
        if not results:
            raise LookupError(f"could not find a place called '{city}'")
        place = max(results, key=lambda r: r.get("population") or 0)
    label = place.get("name", city)
    if place.get("country"):
        label = f"{label}, {place['country']}"
    return {"label": label, "latitude": place["latitude"], "longitude": place["longitude"]}


async def fetch_weather(city: str) -> dict:
    """Raw current conditions. Callers format it for their own audience:
    the tool spells it out for the model to read aloud, the idle screen needs
    something a person can take in at a glance."""
    where = await _geocode(city)

    async with httpx.AsyncClient(
        timeout=WEATHER_TIMEOUT, verify=_SSL_CONTEXT, headers=_USER_AGENT
    ) as client:
        forecast = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": where["latitude"],
                "longitude": where["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code",
            },
        )
        forecast.raise_for_status()
        current = (forecast.json() or {}).get("current") or {}

    return {
        "label": where["label"],
        "condition": _WMO.get(current.get("weather_code"), "unsettled"),
        "temp": current.get("temperature_2m"),
        "feels": current.get("apparent_temperature"),
    }


async def fetch_forecast(city: str) -> dict:
    """Today's precipitation probability and conditions, for the rain alert.

    Same geocoder as fetch_weather so the briefing and the alert agree on the
    place; the alert only ever compares numbers, never free text.
    """
    where = await _geocode(city)

    async with httpx.AsyncClient(
        timeout=WEATHER_TIMEOUT, verify=_SSL_CONTEXT, headers=_USER_AGENT
    ) as client:
        forecast = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": where["latitude"],
                "longitude": where["longitude"],
                "daily": "weather_code,precipitation_probability_max",
                "timezone": "auto",
            },
        )
        forecast.raise_for_status()
        daily = (forecast.json() or {}).get("daily") or {}

    codes = daily.get("weather_code") or [None]
    probs = daily.get("precipitation_probability_max") or [None]
    return {
        "label": where["label"],
        "today_precip_prob": probs[0],
        "today_condition": _WMO.get(codes[0], "unsettled") if codes[0] is not None else None,
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


def _next_occurrence(hour: int, minute: int) -> float:
    """Unix timestamp of the next HH:MM, today if still ahead, else tomorrow."""
    from datetime import timedelta

    now = datetime.now()
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when.timestamp()


def _set_reminder(
    text: str,
    minutes_from_now: int | None = None,
    repeat: str | None = None,
    at: str | None = None,
) -> str:
    from app.services.reminders import reminder_service

    text = (text or "").strip()
    if not text:
        return "ERROR: set_reminder needs the reminder text"
    if len(text) > MAX_REMINDER_CHARS:
        return (
            f"ERROR: reminder text is too long ({len(text)} > {MAX_REMINDER_CHARS} chars); "
            "summarise it in one short sentence"
        )
    if repeat and repeat not in ("daily", "weekly"):
        return "ERROR: repeat must be 'daily' or 'weekly'"

    if at:
        # "at 7pm" style: an exact clock time, optionally repeating. The model
        # hands over HH:MM in 24-hour form; see the tool description.
        m = re.match(r"^(\d{1,2}):(\d{2})$", (at or "").strip())
        if not m:
            return "ERROR: at must be 'HH:MM' in 24-hour time, e.g. '19:00'"
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return "ERROR: at must be a valid time, e.g. '19:00'"
        fire = _next_occurrence(hour, minute)
        reminder = reminder_service.add_at(text, fire, repeat=repeat)
        when = datetime.fromtimestamp(fire).strftime("%I:%M %p").lstrip("0")
        suffix = f", repeating {repeat}" if repeat else ""
        return f"Reminder set: '{text}' at {when}{suffix}."

    if minutes_from_now is None:
        return "ERROR: set_reminder needs either minutes_from_now or at"

    try:
        minutes = int(minutes_from_now)
    except (TypeError, ValueError):
        return "ERROR: minutes_from_now must be a whole number of minutes"
    if minutes < 0:
        return "ERROR: minutes_from_now cannot be negative"

    reminder = reminder_service.add(text, minutes, repeat=repeat)
    when = reminder["fire_time_dt"].strftime("%I:%M %p").lstrip("0")
    suffix = f", repeating {repeat}" if repeat else ""
    return f"Reminder set: '{text}' in {minutes} minute(s), at {when}{suffix}."


register(
    Tool(
        name="set_reminder",
        description=(
            "Set a reminder that will alert the user later. Use when the user asks "
            "to be reminded of something. For a one-shot relative reminder pass "
            "minutes_from_now. For a clock time ('remind me at 7pm') pass at='HH:MM' "
            "in 24-hour form and omit minutes_from_now. For something that repeats "
            "('every morning at 7', 'water plants every Sunday') also pass "
            "repeat='daily' or 'weekly'."
        ),
        parameters={
            "text": {"type": "string", "description": "What to remind the user about"},
            "minutes_from_now": {
                "type": "integer",
                "description": "Delay in minutes (use with repeat='daily'/'weekly' for relative repeating reminders)",
            },
            "at": {
                "type": "string",
                "description": "Clock time in 24-hour HH:MM, e.g. '07:00' — instead of minutes_from_now",
            },
            "repeat": {
                "type": "string",
                "description": "Optional: 'daily' or 'weekly'",
            },
        },
        required=["text"],
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
    repeat = reminder.get("repeat")
    if repeat:
        stamp += f" (repeats {repeat})"
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


# --- timers -------------------------------------------------------------------
# Countdown timers ("10 minute timer"), distinct from datetime reminders. The
# service itself lives in services/timers.py; these are the model-facing
# wrappers, like the reminder tools above.


def _set_timer(minutes: int, label: str = "") -> str:
    from app.services.timers import timer_service

    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "ERROR: minutes must be a whole number"
    if minutes < 1 or minutes > 180:
        return "ERROR: timers must be between 1 and 180 minutes"
    label = (label or "").strip()
    if len(label) > 60:
        return "ERROR: timer label is too long; keep it short"
    timer = timer_service.add(minutes, label)
    return f"Timer set for {minutes} minute(s){': ' + label if label else ''}."


register(
    Tool(
        name="set_timer",
        description=(
            "Set a countdown timer that alerts the user when the time is up. Use for "
            "kitchen-timer style requests ('set a 10 minute timer', 'timer for 5 minutes')."
        ),
        parameters={
            "minutes": {"type": "integer", "description": "Minutes to count down (1-180)"},
            "label": {"type": "string", "description": "Optional short label, e.g. 'pasta'"},
        },
        required=["minutes"],
        func=_set_timer,
    )
)


def _cancel_timer(text: str) -> str:
    from app.services.timers import timer_service

    cancelled = timer_service.cancel_by_text(text)
    if cancelled is None:
        active = timer_service.active()
        if not active:
            return "ERROR: no timer is running"
        listed = "\n".join(f"[{t['id']}] {t['text']}" for t in active)
        return f"No timer matches. Running timers:\n{listed}"
    return f"Cancelled: '{cancelled[1]}'."


register(
    Tool(
        name="cancel_timer",
        description="Cancel a running countdown timer. Use when the user asks to stop or cancel a timer.",
        parameters={"text": {"type": "string", "description": "Roughly what the timer is for"}},
        required=["text"],
        func=_cancel_timer,
    )
)


# --- briefing -----------------------------------------------------------------
# A deterministic daily-ish digest: weather plus today's reminders. No LLM in
# the assembly — the model merely reads the result aloud, and the proactive
# morning push reuses it so the day starts with something useful.

_NEWS_FEEDS = (
    "https://www.hindustantimes.com/feeds/rss/top-news/rssfeed.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
)


async def _fetch_headlines() -> list[str]:
    """Top 3 headlines from the first feed that answers. Each feed is
    fetched in its own client so one slow host cannot stall the briefing;
    a broken feed is skipped, not fatal."""
    import xml.etree.ElementTree as ET

    titles: list[str] = []
    for url in _NEWS_FEEDS:
        try:
            async with httpx.AsyncClient(
                timeout=6.0, verify=_SSL_CONTEXT, headers=_USER_AGENT
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").strip()
                    if title and title not in titles:
                        titles.append(title)
                    if len(titles) >= 3:
                        break
        except (httpx.HTTPError, ET.ParseError):
            continue
        if len(titles) >= 3:
            break
    return titles[:3]


async def _get_briefing(city: str | None = None) -> str:
    from app.config import settings
    from app.services.reminders import reminder_service

    city = (city or "").strip() or (settings.weather_city or "").strip()
    lines: list[str] = []
    if city:
        try:
            lines.append("Weather: " + format_weather_full(await fetch_weather(city)))
        except (ValueError, LookupError, httpx.HTTPError) as e:
            lines.append(f"Weather: unavailable right now ({e})")

    today = datetime.now().date()
    today_reminders = [
        r
        for r in reminder_service.pending()
        if datetime.fromtimestamp(r["fire_time"]).date() == today
    ]
    if today_reminders:
        lines.append(
            "Reminders today: " + "; ".join(_format_reminder(r) for r in today_reminders)
        )
    else:
        lines.append("No reminders set for today.")

    headlines = await _fetch_headlines()
    if headlines:
        lines.append("News: " + " | ".join(headlines))

    return " | ".join(lines)


register(
    Tool(
        name="get_briefing",
        description=(
            "Get a short daily briefing: current weather and reminders scheduled for "
            "today. Use when the user asks for a briefing, the plan for the day, or "
            "what is coming up."
        ),
        parameters={"city": {"type": "string", "description": "Optional city for the weather"}},
        required=[],
        func=_get_briefing,
    )
)


# --- web lookup ---------------------------------------------------------------
# The local model knows what it was trained on and nothing newer. A cheap,
# keyless lookup makes "what is X" answers real instead of guessed. The query
# never interpolates into a URL — both endpoints are fixed and take it as a
# query parameter.


_USER_AGENT = {"User-Agent": "iphone-6-companion/1.0 (personal desk device)"}
_LITE_HTML_SNIPPET = re.compile(r"<td class=['\"]result-snippet['\"]>(.*?)</td>", re.S)
_HTML_TAGS = re.compile(r"<[^>]+>|&[a-z#0-9]+;")


def _scrape_ddg_lite(html: str) -> str:
    """First result snippet from DDG's lite HTML endpoint.

    The last-resort fallback: it works even on networks where Wikipedia is
    blocked, and its markup is stable enough to target one class attribute.
    """
    m = _LITE_HTML_SNIPPET.search(html)
    if not m:
        return ""
    return _HTML_TAGS.sub("", m.group(1)).strip()


async def _web_lookup(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "ERROR: web_lookup needs a query"

    async with httpx.AsyncClient(
        timeout=6.0, verify=_SSL_CONTEXT, headers=_USER_AGENT
    ) as client:
        # DuckDuckGo Instant Answer: direct facts, no key. Not every query has
        # one, but when it does the abstract is a full Wikipedia-style summary.
        try:
            r = await client.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": query,
                    "format": "json",
                    "no_html": 1,
                    "skip_disambig": 1,
                },
            )
            r.raise_for_status()
            abstract = ((r.json() or {}).get("AbstractText") or "").strip()
            if abstract:
                return abstract[:600]
        except httpx.HTTPError:
            pass  # fall through

        # Wikipedia search -> top hit's summary. Works on most networks;
        # some ISPs block the Wikimedia range, which is what the third tier
        # is for.
        try:
            r = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 1,
                    "format": "json",
                    "utf8": 1,
                },
            )
            r.raise_for_status()
            hits = ((r.json() or {}).get("query") or {}).get("search") or []
            if hits:
                summary_r = await client.get(
                    "https://en.wikipedia.org/api/rest_v1/page/summary/"
                    + urllib.parse.quote(hits[0]["title"].replace(" ", "_"), safe="")
                )
                summary_r.raise_for_status()
                summary = ((summary_r.json() or {}).get("extract") or "").strip()
                if summary:
                    return summary[:600]
        except httpx.HTTPError:
            pass  # fall through

        # DDG lite HTML: the snippet is short but real, and this host is not
        # the Wikimedia range so it survives the block above. It serves an
        # empty page to obviously-bot user agents, so use a browser one.
        try:
            lite_r = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0 Safari/537.36"
                    )
                },
            )
            lite_r.raise_for_status()
            snippet = _scrape_ddg_lite(lite_r.text)
            if snippet:
                return snippet[:600]
        except httpx.HTTPError:
            pass

        return f"ERROR: nothing found for '{query}'"


register(
    Tool(
        name="web_lookup",
        description=(
            "Look up a fact on the web. Use whenever the user asks about something "
            "you are unsure of — news, history, science, definitions, current events."
        ),
        parameters={"query": {"type": "string", "description": "What to look up, e.g. 'largest planet'"}},
        required=["query"],
        func=_web_lookup,
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

    async def _volume(percent: int) -> str:
        try:
            level = await asyncio.to_thread(pc_control.set_volume, percent)
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

    async def _mute(muted: bool = True) -> str:
        try:
            # The model may send a real bool or the string "true".
            flag = muted if isinstance(muted, bool) else str(muted).strip().lower() in ("true", "1", "yes")
            await asyncio.to_thread(pc_control.set_mute, flag)
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

    async def _status() -> str:
        try:
            level, muted = await asyncio.to_thread(pc_control.get_volume)
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

    async def _lock() -> str:
        try:
            await asyncio.to_thread(pc_control.lock_screen)
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return "PC locked."

    async def _find_file(name: str) -> str:
        try:
            hits = await asyncio.to_thread(pc_control.find_files, name)
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        if not hits:
            return f"No files named like '{name}' in Desktop, Documents or Downloads."
        return "Found: " + " | ".join(hits[:5])

    register(
        Tool(
            name="find_file",
            description=(
                "Search the user's Desktop, Documents and Downloads for files with "
                "a matching name. Use when they ask to find a file — 'find my resume', "
                "'where is that report'. Returns up to 5 full paths."
            ),
            parameters={"name": {"type": "string", "description": "Part of the file name, e.g. 'resume'"}},
            required=["name"],
            func=_find_file,
        )
    )

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

    def _copy_to_clipboard(text: str) -> str:
        try:
            pc_control.set_clipboard(text)
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return "Copied to the clipboard."

    register(
        Tool(
            name="copy_to_clipboard",
            description=(
                "Copy text to the PC's clipboard, so the user can paste it anywhere — "
                "an address, a code snippet, a name. Use when they ask to copy something."
            ),
            parameters={"text": {"type": "string", "description": "The exact text to copy"}},
            required=["text"],
            func=_copy_to_clipboard,
        )
    )

    def _read_clipboard() -> str:
        try:
            content = pc_control.get_clipboard()
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return f"The clipboard contains: {content}" if content else "The clipboard is empty."

    register(
        Tool(
            name="read_clipboard",
            description="Read whatever text is currently on the PC's clipboard.",
            parameters={},
            required=[],
            func=_read_clipboard,
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

        async def _launch(name: str) -> str:
            name = (name or "").strip().lower()
            path = settings.pc_app_whitelist.get(name)
            if path is None:
                known = ", ".join(sorted(settings.pc_app_whitelist)) or "none configured"
                return f"ERROR: '{name}' is not in the app whitelist. Known: {known}"
            try:
                await asyncio.to_thread(pc_control.launch_app, path)
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

    async def _open_browser(target: str) -> str:
        target = (target or "").strip()
        if not target:
            return "ERROR: open_in_browser needs an address or a shortcut name"

        # A shortcut name wins over treating the text as an address, so
        # "open my email" resolves to the configured URL rather than trying
        # to visit https://my email.
        resolved = settings.pc_url_shortcuts.get(target.lower(), target)
        try:
            opened = await asyncio.to_thread(pc_control.open_url, resolved)
        except pc_control.PCControlError as e:
            return f"ERROR: {e}"
        return f"Opened {opened} in the browser."

    shortcut_hint = (
        " Known shortcuts: " + ", ".join(sorted(settings.pc_url_shortcuts))
        if settings.pc_url_shortcuts
        else ""
    )
    register(
        Tool(
            name="open_in_browser",
            description=(
                "Open a website in a new browser tab on the user's PC. Pass a full "
                "address like 'https://youtube.com', or a bare domain like "
                "'youtube.com'. Use this whenever they ask to open, pull up, or go "
                "to a site." + shortcut_hint
            ),
            parameters={
                "target": {
                    "type": "string",
                    "description": "A web address, or a configured shortcut name",
                }
            },
            required=["target"],
            func=_open_browser,
        )
    )

    if settings.pc_power_control_enabled:

        async def _power(action: str, confirm: bool = False) -> str:
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
                    await asyncio.to_thread(pc_control.sleep_pc)
                    return "PC is going to sleep."
                await asyncio.to_thread(
                    pc_control.shutdown_pc, settings.pc_shutdown_delay_seconds
                )
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

    def _schedule_power(action: str, minutes_from_now: int) -> str:
        from app.services.reminders import reminder_service

        action = (action or "").strip().lower()
        if action not in ("sleep", "shutdown", "lock"):
            return "ERROR: action must be one of: sleep, shutdown, lock"
        if settings.pc_power_control_enabled is False and action != "lock":
            return "ERROR: power scheduling is disabled by config"
        try:
            minutes = int(minutes_from_now)
        except (TypeError, ValueError):
            return "ERROR: minutes_from_now must be a whole number of minutes"
        if minutes < 1 or minutes > 4320:
            return "ERROR: minutes_from_now must be between 1 and 4320 (3 days)"
        # Reminder text and the cancel hint both use the bare action name, so
        # the suggested phrase ("cancel the scheduled lock") matches the row
        # through the same substring lookup as any other reminder.
        reminder = reminder_service.add(
            f"Scheduled {action}", minutes, power_action=action
        )
        when = reminder["fire_time_dt"].strftime("%I:%M %p").lstrip("0")
        return (
            f"Scheduled {action} for {minutes} minute(s) from now, at {when}. "
            f"Say 'cancel the scheduled {action}' to abort it."
        )

    if settings.pc_power_control_enabled:
        register(
            Tool(
                name="schedule_power_action",
                description=(
                    "Schedule a PC power action for later — 'shut down in 30 minutes', "
                    "'sleep in an hour', 'lock in 5 minutes'. The action fires on its own "
                    "at the scheduled time. To abort it, cancel the matching reminder."
                ),
                parameters={
                    "action": {"type": "string", "description": "sleep, shutdown or lock"},
                    "minutes_from_now": {"type": "integer", "description": "Minutes until it runs (1-4320)"},
                },
                required=["action", "minutes_from_now"],
                func=_schedule_power,
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
    from app.services import smart_home as sh

    active = sh.provider()
    if active == "none":
        logger.info("Smart home tools skipped: no provider configured")
        return

    async def _list_devices() -> str:
        try:
            devices = await sh.list_devices()
        except sh.SmartHomeError as e:
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
            devices = await sh.list_devices()
        except sh.SmartHomeError as e:
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
            await sh.set_state(device["entity_id"], flag)
        except sh.SmartHomeError as e:
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

    logger.info("Smart home tools registered (provider=%s)", active)


_register_smart_home_tools()


__all__ = ["Tool", "execute", "get", "register", "MAX_FACTS"]
