import asyncio
import json
import logging
import re

import httpx

from app.config import settings
from app.models.schemas import EMOTIONS, ChatMessage
from app.services import tools

logger = logging.getLogger(__name__)

# How many prior turns to replay. The companion only needs enough to follow the
# current thread; a long tail costs prompt tokens and slows a local model on
# every request.
HISTORY_TURNS = 12

# Tool calls allowed per user message before the model is forced to answer.
# Two covers the realistic chains (look something up, then act on it) while
# making an infinite tool loop structurally impossible.
MAX_TOOL_CALLS = 2

# The prompt asks for about 200 characters, but a slightly larger hard ceiling
# leaves room for a natural two-sentence reply while still bounding text from a
# misconfigured or non-compliant local model.  It also matches /speak's input
# limit, so every chat response remains safe to send to TTS.
MAX_REPLY_CHARS = 600

SYSTEM_PROMPT = """You are a warm, familiar companion living on a small phone screen on someone's desk. You are not a search engine and not a corporate assistant.

How you speak:
- One or two short sentences. Never more than about 200 characters.
- Plain conversational language. No lists, no markdown, no headings, no emoji.
- Warm and present, like a friend who is glad they came back. Never bubbly or fake.
- Never say "As an AI", never mention being a model, never offer to "assist you today".
- It is fine to ask a short question back, but not every single turn.
- If you don't know something, say so plainly and briefly."""

# Appended when tools are offered. Deliberately says nothing about JSON,
# emotion, or output shape — mixing "decide whether to call a tool" with
# "also format your answer a particular way" measurably wrecks tool-call
# reliability (right down to ~46% in testing). Emotion for a plain reply is
# classified in a separate, tiny follow-up call instead — see
# EMOTION_CLASSIFY_PROMPT and CompanionService.chat.
TOOLS_PROMPT_TEMPLATE = """

Use the provided tools to actually perform anything the user asks you to set, cancel,
remember, forget, play, mute, or change. Call the tool BEFORE describing the result —
never say you did something without calling its tool first. A reply like "okay, cancelled
that" with no tool call changes nothing at all; you will have told the user something untrue.

Memory especially: if the user tells you to remember something, or tells you a lasting fact
about themselves — a name, a relationship, a preference, a routine, where they work, what
they are working on — call remember, or by tomorrow you will not know it.

To cancel a reminder you may call list_reminders first to find it, then cancel_reminder.

Otherwise — small talk, feelings, opinions, or once you already have a tool result to
report — just reply normally in plain text. Never mention tools, arguments, JSON or errors
by name; if a tool result starts with "ERROR", say plainly that it didn't work."""

# Durable facts, injected ahead of the tool block. Framed as things already
# known rather than as a transcript, so the model does not treat them as
# something the user just said and reply to them.
MEMORY_PROMPT_TEMPLATE = """

Things you already know about this person, from earlier conversations:
{facts}

Use these naturally when they matter. Do not recite them, do not mention that
you have notes, and do not bring them up unprompted just to show you remember."""

# Injected once the tool budget is spent, so the last call cannot start another
# chain no matter what the model would prefer to do. No output-shape
# instruction here either, for the same reason as TOOLS_PROMPT_TEMPLATE.
FINAL_ANSWER_NUDGE = (
    "You have used your tool budget for this message. Reply now in plain text — "
    "do not call another tool."
)

# The second, cheap call: given a plain-text reply that needed no tool, pick
# the expression that fits it. Kept to a single word so num_predict can stay
# tiny — this call costs ~300ms next to the ~800ms first pass, not another
# full generation.
EMOTION_CLASSIFY_PROMPT = (
    "Pick the ONE word that best describes the emotional tone of this reply. "
    "Reply with only that word, nothing else. Options: idle, happy, think, listen, sad, sleepy."
)
# Kept small and separate from llm_temperature (which is tuned for warm,
# varied conversation): a classification pick should be the model's most
# confident answer, not a varied one.
EMOTION_CLASSIFY_TEMPERATURE = 0.2
EMOTION_CLASSIFY_MAX_TOKENS = 6

# improvise() has no tool decision to protect, so unlike the templates above it
# can safely ask for strict JSON in the same call — that is what keeps a
# proactive line to one round trip instead of two.
IMPROVISE_JSON_TEMPLATE = """

Reply with ONLY a JSON object, nothing before or after it:
{{"reply": "your short line here", "emotion": "one of idle, happy, think, listen, sad, sleepy"}}"""

# Models sometimes answer with a near-miss label (the adjective rather than the
# state name). Mapping them beats discarding an otherwise correct choice.
_EMOTION_ALIASES = {
    "neutral": "idle",
    "calm": "idle",
    "default": "idle",
    "thinking": "think",
    "processing": "think",
    "curious": "think",
    "listening": "listen",
    "attentive": "listen",
    "happiness": "happy",
    "joy": "happy",
    "excited": "happy",
    "confused": "sad",
    "sorry": "sad",
    "sadness": "sad",
    "tired": "sleepy",
    "sleep": "sleepy",
}

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)


class CompanionUnavailable(RuntimeError):
    """Ollama could not be reached, or returned something unusable."""


def _strip_wrappers(raw: str) -> str:
    """Remove reasoning blocks and markdown fences from a model response."""
    text = _THINK_BLOCK_RE.sub("", raw)
    # A response truncated by num_predict can open <think> and never close it.
    text = _UNCLOSED_THINK_RE.sub("", text)
    text = _FENCE_RE.sub("", text)
    return text.strip()


def _extract_json_object(text: str) -> dict | None:
    """Best-effort parse of the model's JSON, tolerating surrounding prose."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost brace pair, which survives a stray sentence
    # before or after the object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_emotion(value: object) -> str:
    if not isinstance(value, str):
        return "idle"
    key = value.strip().lower()
    key = _EMOTION_ALIASES.get(key, key)
    return key if key in EMOTIONS else "idle"


def _extract_emotion_word(text: str) -> str:
    """Pull an emotion out of the classifier's short reply.

    num_predict is capped small for that call, so the output is normally just
    the bare word — but stray punctuation or a leading article ("the emotion
    is happy") is cheap to tolerate with a substring search rather than
    requiring an exact match.
    """
    lowered = text.lower()
    for emotion in EMOTIONS:
        if emotion in lowered:
            return emotion
    for alias, canonical in _EMOTION_ALIASES.items():
        if alias in lowered:
            return canonical
    return "idle"


def _normalize_reply(value: object) -> str:
    """Return one compact, UI-safe line from untrusted model output."""
    if not isinstance(value, str):
        return ""
    # A reply is shown in a small speech bubble and sent directly to TTS; line
    # breaks and huge whitespace runs provide neither UI nor conversational
    # value here.
    reply = re.sub(r"\s+", " ", value).strip()
    return reply[:MAX_REPLY_CHARS].rstrip()


def parse_model_output(raw: str) -> tuple[str, str]:
    """Turn a raw completion into (reply, emotion).

    Never raises: a model that ignores the JSON instruction still produces a
    usable reply, which matters more than a correct expression.
    """
    cleaned = _strip_wrappers(raw)
    payload = _extract_json_object(cleaned)
    if payload is None:
        logger.info("Model did not return JSON; using raw text as the reply")
        return _normalize_reply(cleaned), "idle"

    reply = _normalize_reply(payload.get("reply"))
    if not reply:
        # Valid JSON, empty/missing reply — the cleaned text is still better
        # than nothing only if it isn't just the JSON envelope itself.
        reply = "" if payload else _normalize_reply(cleaned)
    return reply, _normalize_emotion(payload.get("emotion"))


# Not used by chat() any more — tool routing went from this prompt-JSON
# scheme to Ollama's native tool-calling (see CompanionService.chat), which
# measured far more reliable. Kept for anything that still wants to parse a
# model's free-text {"tool": ...} attempt.
def parse_tool_request(raw: str) -> tuple[str, dict] | None:
    """Return (name, args) if the model asked for a tool, otherwise None.

    A payload carrying both "tool" and "reply" resolves to the tool: a reply
    written before the data arrives is a guess, and the second pass will
    produce a grounded one.
    """
    payload = _extract_json_object(_strip_wrappers(raw))
    if not isinstance(payload, dict):
        return None
    name = payload.get("tool")
    if not isinstance(name, str) or not name.strip():
        return None
    args = payload.get("args")
    return name.strip(), args if isinstance(args, dict) else {}


class CompanionService:
    """Chat against a local Ollama model, with a bounded tool loop."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # Startup schedules a one-token prewarm. Until it completes, the UI
        # can explain a short first-load wait instead of looking disconnected.
        self._model_status = "warming"

    @property
    def model_status(self) -> str:
        return self._model_status

    def _get_client(self) -> httpx.AsyncClient:
        # Created lazily so importing the module never opens a connection pool,
        # and reused so each request skips the TCP/TLS handshake.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.ollama_base_url,
                timeout=settings.llm_request_timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @staticmethod
    def _load_facts() -> list[str]:
        """Blocking; callers run it off the event loop."""
        from app.services.store import store

        try:
            return [row["text"] for row in store.list_facts()]
        except Exception as e:
            # Memory is an enhancement — never fail a reply because of it.
            logger.warning("Could not load facts: %s", e)
            return []

    @staticmethod
    def _persona(facts: list[str] | None) -> str:
        prompt = SYSTEM_PROMPT
        if facts:
            prompt += MEMORY_PROMPT_TEMPLATE.format(
                facts="\n".join("- " + fact for fact in facts)
            )
        return prompt

    def _tool_system_prompt(self, facts: list[str] | None) -> str:
        return self._persona(facts) + TOOLS_PROMPT_TEMPLATE

    def _json_system_prompt(self, facts: list[str] | None) -> str:
        return self._persona(facts) + IMPROVISE_JSON_TEMPLATE

    @staticmethod
    def _history_messages(history: list[ChatMessage]) -> list[dict]:
        return [{"role": turn.role, "content": turn.content} for turn in history[-HISTORY_TURNS:]]

    async def _post_chat(
        self,
        messages: list[dict],
        tools_schema: list[dict] | None = None,
        json_mode: bool = False,
        num_predict: int | None = None,
        temperature: float | None = None,
    ) -> dict:
        """One Ollama round trip. Returns the raw assistant message object —
        {"content": str, "tool_calls": [...]} — so callers can see whichever
        of the two the model produced instead of only ever getting text."""
        body: dict = {
            "model": settings.ollama_model,
            "messages": messages,
            "stream": False,
            # Without this Ollama unloads after 5 minutes idle, so a companion
            # used in short bursts pays a cold load almost every time.
            "keep_alive": settings.ollama_keep_alive,
            "options": {
                "temperature": settings.llm_temperature if temperature is None else temperature,
                "num_predict": settings.llm_max_tokens if num_predict is None else num_predict,
            },
        }
        if tools_schema:
            body["tools"] = tools_schema
        if json_mode:
            # Only used by improvise(), which has no tool decision to protect —
            # combining a JSON-output instruction with tool availability is
            # what wrecked routing reliability (measured down to ~46%).
            body["format"] = "json"
        if settings.llm_disable_thinking:
            # Ignored by Ollama builds predating the switch, and by models
            # without a thinking mode.
            body["think"] = False

        try:
            response = await self._get_client().post("/api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            self._model_status = "unavailable"
            logger.error(
                "Ollama returned %s: %s", e.response.status_code, e.response.text[:500]
            )
            raise CompanionUnavailable("Ollama rejected the request") from e
        except httpx.HTTPError as e:
            self._model_status = "unavailable"
            logger.error("Ollama unreachable at %s: %s", settings.ollama_base_url, e)
            raise CompanionUnavailable("Ollama is unreachable") from e
        except json.JSONDecodeError as e:
            self._model_status = "unavailable"
            logger.error("Ollama returned a non-JSON envelope")
            raise CompanionUnavailable("Ollama returned an unreadable response") from e

        # A successful HTTP response is not enough: a proxy, a different API
        # version, or a malformed local server can still return JSON in an
        # unexpected shape. Keep that implementation detail from escaping as
        # an AttributeError and turning into a generic 500 at the route.
        if not isinstance(data, dict):
            self._model_status = "unavailable"
            logger.error("Ollama returned a non-object response envelope")
            raise CompanionUnavailable("Ollama returned an unreadable response")
        message = data.get("message")
        if not isinstance(message, dict):
            self._model_status = "unavailable"
            logger.error("Ollama response did not contain a message object")
            raise CompanionUnavailable("Ollama returned an unreadable response")
        content = message.get("content")
        if not isinstance(content, str):
            self._model_status = "unavailable"
            logger.error("Ollama response did not contain text content")
            raise CompanionUnavailable("Ollama returned an unreadable response")
        self._model_status = "ready"
        return message

    async def _classify_emotion(self, reply_text: str) -> str:
        """Cheap follow-up call: given a plain-text reply, pick the expression
        that fits it. ~300ms next to the ~800ms routing call, not another full
        generation — and it only runs when the routing call produced plain
        text rather than a tool call.

        Never raises: a wrong or missing classification still has a real reply
        to show, so this defaults to "idle" rather than failing the turn.
        """
        try:
            message = await self._post_chat(
                [
                    {"role": "system", "content": EMOTION_CLASSIFY_PROMPT},
                    {"role": "user", "content": reply_text},
                ],
                num_predict=EMOTION_CLASSIFY_MAX_TOKENS,
                temperature=EMOTION_CLASSIFY_TEMPERATURE,
            )
        except CompanionUnavailable as e:
            logger.warning("Emotion classification failed, defaulting to idle: %s", e)
            return "idle"
        return _extract_emotion_word(message.get("content") or "")

    async def chat(self, message: str, history: list[ChatMessage]) -> dict:
        """Return {"reply": str, "emotion": str}.

        Native tool-calling, not the prompt-JSON scheme this used to run:
        measured on this model, asking it to simultaneously decide whether a
        tool is needed AND format its answer in a particular way collapsed
        routing accuracy as low as ~46%. Splitting the two — Ollama's own
        `tools` API for the decision, plain text for the answer, a separate
        cheap call to classify emotion only when no tool fired — measured
        90%+ with zero false tool-fires on small talk.

        A tool call is executed and its result fed back as a "tool" message;
        the loop continues until the model answers in plain text or
        MAX_TOOL_CALLS is spent, at which point tools are withheld so a final
        answer is the only thing the model can produce.

        Raises CompanionUnavailable when Ollama is unreachable or errors, so
        the route can answer with the frontend's "sad" fallback path.
        """
        facts = await asyncio.to_thread(self._load_facts)
        messages: list[dict] = [
            {"role": "system", "content": self._tool_system_prompt(facts)},
            *self._history_messages(history),
            {"role": "user", "content": message},
        ]
        schemas = tools.render_tool_schemas()
        tool_calls_used = 0

        while True:
            forced = tool_calls_used >= MAX_TOOL_CALLS
            # The nudge is passed per-call rather than appended to `messages`,
            # so it cannot accumulate across iterations. Tools are withheld
            # entirely once forced, so a further call is not just discouraged
            # but structurally impossible.
            call_messages = (
                messages + [{"role": "system", "content": FINAL_ANSWER_NUDGE}]
                if forced
                else messages
            )
            reply_msg = await self._post_chat(
                call_messages, tools_schema=None if forced else schemas
            )
            calls = reply_msg.get("tool_calls") or []

            if not calls:
                reply = _normalize_reply(reply_msg.get("content"))
                if not reply:
                    logger.error("Model produced an empty reply")
                    raise CompanionUnavailable("Model produced an empty reply")
                emotion = await self._classify_emotion(reply)
                if tool_calls_used:
                    logger.info("Replied after %d tool call(s)", tool_calls_used)
                return {"reply": reply, "emotion": emotion}

            messages.append(
                {
                    "role": "assistant",
                    "content": reply_msg.get("content") or "",
                    "tool_calls": calls,
                }
            )
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name")
                args = fn.get("arguments")
                # Ollama's native format returns arguments already parsed, but
                # a model can still emit them as a JSON string.
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                result = await tools.execute(name, args)
                tool_calls_used += 1
                logger.info("Tool %s(%s) -> %s", name, args, result[:200])
                messages.append({"role": "tool", "content": result, "name": name})

    async def prewarm(self) -> None:
        """Make the model weights resident before the user says anything.

        Nothing in startup issues an inference call, so without this the first
        message of a session pays the full load (~12s for qwen3:8b) before a
        single token is generated. One token is enough to force the load.

        Never raises: this is an optimisation, and a missing Ollama must not
        stop the server from coming up.
        """
        if not settings.llm_prewarm_enabled:
            self._model_status = "ready"
            return
        self._model_status = "warming"
        try:
            await asyncio.wait_for(
                self._post_chat([{"role": "user", "content": "ok"}], num_predict=1),
                timeout=settings.llm_prewarm_timeout,
            )
            self._model_status = "ready"
            logger.info(
                "LLM prewarmed (%s, keep_alive=%s)", settings.ollama_model, settings.ollama_keep_alive
            )
        except asyncio.TimeoutError:
            self._model_status = "unavailable"
            logger.warning("LLM prewarm timed out after %.0fs", settings.llm_prewarm_timeout)
        except Exception as e:
            self._model_status = "unavailable"
            logger.info("LLM prewarm skipped: %s", e)

    async def improvise(self, instruction: str) -> dict:
        """One-shot line in character, with no tools and no conversation.

        Used by the proactive layer so an unprompted greeting sounds like the
        companion rather than a canned string. Deliberately does NOT offer
        tools: an unprompted line should never set a reminder or touch the PC
        as a side effect of saying good morning. With no tool decision to
        protect, this is the one path that can safely ask for strict JSON in
        a single call.
        """
        facts = await asyncio.to_thread(self._load_facts)
        messages = [
            {"role": "system", "content": self._json_system_prompt(facts)},
            {"role": "user", "content": instruction},
        ]
        message = await self._post_chat(messages, json_mode=True)
        reply, emotion = parse_model_output(message.get("content") or "")
        if not reply:
            raise CompanionUnavailable("Model produced an empty line")
        return {"reply": reply, "emotion": emotion}

    async def is_available(self) -> bool:
        """Cheap liveness probe for /health. Never raises."""
        try:
            response = await self._get_client().get("/api/tags", timeout=3.0)
            available = response.status_code == 200
            if not available:
                self._model_status = "unavailable"
            elif self._model_status != "warming":
                self._model_status = "ready"
            return available
        except httpx.HTTPError:
            self._model_status = "unavailable"
            return False


companion_service = CompanionService()
