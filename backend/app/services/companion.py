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
- If you don't know something, say so plainly and briefly.

You also pick the facial expression that fits your reply. Choose exactly one:
- "happy" — good news, warmth, playfulness, shared enthusiasm.
- "think" — you are reasoning, recalling, or the question is genuinely hard.
- "listen" — you are inviting them to say more, or asking them a question.
- "sad" — they shared something difficult, or you are letting them down.
- "sleepy" — the mood is low-key, winding down, late-night, quiet.
- "idle" — calm and neutral; the default when none of the above clearly fits.

Reply with ONLY a JSON object, nothing before or after it:
{"reply": "your short reply here", "emotion": "one of idle, happy, think, listen, sad, sleepy"}"""

# Appended to the persona prompt. Kept separate so the persona above stays
# readable and can be edited without picking through tool mechanics.
TOOLS_PROMPT_TEMPLATE = """

You can use tools. If answering needs one, respond with ONLY this shape:
{{"tool": "tool_name", "args": {{...}}}}

Available tools:
{tool_specs}

Tool rules:
- Only call a tool when you genuinely need it. Small talk, feelings and opinions need none.
- Actions must be PERFORMED, not narrated. Setting, cancelling, remembering and forgetting all
  happen only when you call the tool. Replying "okay, cancelled that" or "got it, I'll
  remember" without calling the tool changes nothing at all — you will have told the user
  something untrue. If they ask you to do one of these things, call the tool first and describe
  it afterwards.
- Memory especially: if the user tells you to remember something, or tells you a lasting fact
  about themselves — a name, a relationship, a preference, a routine, where they work, what
  they are working on — call remember, or by tomorrow you will not know it.
- To cancel a reminder you may call list_reminders first to find it, then cancel_reminder.
- Never invent a tool name or an argument that is not listed above.
- After you are given a tool result, use it to write your normal reply.
- If a tool result begins with "ERROR", do not retry it. Tell the user briefly and plainly that it did not work, and use emotion "sad".
- Never mention JSON, tools, arguments or errors by name in your reply. Just speak naturally."""

# Durable facts, injected ahead of the tool block. Framed as things already
# known rather than as a transcript, so the model does not treat them as
# something the user just said and reply to them.
MEMORY_PROMPT_TEMPLATE = """

Things you already know about this person, from earlier conversations:
{facts}

Use these naturally when they matter. Do not recite them, do not mention that
you have notes, and do not bring them up unprompted just to show you remember."""

# Injected once the tool budget is spent, so the last call cannot start another
# chain no matter what the model would prefer to do.
FINAL_ANSWER_NUDGE = (
    'You have used your tool budget for this message. Reply now using ONLY '
    '{"reply": "...", "emotion": "..."} — do not call another tool.'
)

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

    def _system_prompt(self, facts: list[str] | None = None, with_tools: bool = True) -> str:
        prompt = SYSTEM_PROMPT
        if facts:
            prompt += MEMORY_PROMPT_TEMPLATE.format(
                facts="\n".join("- " + fact for fact in facts)
            )
        if with_tools:
            prompt += TOOLS_PROMPT_TEMPLATE.format(tool_specs=tools.render_tool_specs())
        return prompt

    def _build_messages(
        self, message: str, history: list[ChatMessage], facts: list[str] | None = None
    ) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": self._system_prompt(facts)}]
        for turn in history[-HISTORY_TURNS:]:
            messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": message})
        return messages

    async def _complete(self, messages: list[dict]) -> str:
        """One Ollama round trip. Returns the raw assistant content."""
        body: dict = {
            "model": settings.ollama_model,
            "messages": messages,
            "stream": False,
            # Constrains decoding to valid JSON, so parsing is a fallback for
            # older Ollama builds rather than the primary defence.
            "format": "json",
            # Without this Ollama unloads after 5 minutes idle, so a companion
            # used in short bursts pays a cold load almost every time.
            "keep_alive": settings.ollama_keep_alive,
            "options": {
                "temperature": settings.llm_temperature,
                "num_predict": settings.llm_max_tokens,
            },
        }
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
        return content

    async def chat(self, message: str, history: list[ChatMessage]) -> dict:
        """Return {"reply": str, "emotion": str}.

        Runs the tool loop: the model either asks for a tool or answers. A tool
        request is executed, its result appended to the conversation, and the
        model asked again — at most MAX_TOOL_CALLS times before it is forced to
        answer.

        Raises CompanionUnavailable when Ollama is unreachable or errors, so
        the route can answer with the frontend's "sad" fallback path.
        """
        facts = await asyncio.to_thread(self._load_facts)
        messages = self._build_messages(message, history, facts)
        tool_calls = 0

        while True:
            forced = tool_calls >= MAX_TOOL_CALLS
            # The nudge is passed per-call rather than appended to `messages`,
            # so it cannot accumulate across iterations.
            call_messages = (
                messages + [{"role": "system", "content": FINAL_ANSWER_NUDGE}]
                if forced
                else messages
            )
            raw = await self._complete(call_messages)
            request = parse_tool_request(raw) if not forced else None

            if request is None:
                reply, emotion = parse_model_output(raw)
                if not reply:
                    logger.error("Model produced an empty reply (raw=%r)", raw[:300])
                    raise CompanionUnavailable("Model produced an empty reply")
                if tool_calls:
                    logger.info("Replied after %d tool call(s)", tool_calls)
                return {"reply": reply, "emotion": emotion}

            name, args = request
            result = await tools.execute(name, args)
            tool_calls += 1
            logger.info("Tool %s(%s) -> %s", name, args, result[:200])

            # Echoed back as assistant + user rather than the "tool" role: not
            # every local model is trained on that role, and a labelled user
            # message is understood universally.
            messages.append(
                {"role": "assistant", "content": json.dumps({"tool": name, "args": args})}
            )
            messages.append({"role": "user", "content": f"[tool result] {name} -> {result}"})

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
            response = await asyncio.wait_for(
                self._get_client().post(
                    "/api/chat",
                    json={
                        "model": settings.ollama_model,
                        "messages": [{"role": "user", "content": "ok"}],
                        "stream": False,
                        "keep_alive": settings.ollama_keep_alive,
                        "options": {"num_predict": 1},
                    },
                ),
                timeout=settings.llm_prewarm_timeout,
            )
            response.raise_for_status()
            self._model_status = "ready"
            logger.info("LLM prewarmed (%s, keep_alive=%s)", settings.ollama_model, settings.ollama_keep_alive)
        except asyncio.TimeoutError:
            self._model_status = "unavailable"
            logger.warning("LLM prewarm timed out after %.0fs", settings.llm_prewarm_timeout)
        except Exception as e:
            self._model_status = "unavailable"
            logger.info("LLM prewarm skipped: %s", e)

    async def improvise(self, instruction: str) -> dict:
        """One-shot line in character, with no tools and no conversation.

        Used by the proactive layer so an unprompted greeting sounds like the
        companion rather than a canned string. Deliberately does NOT use the
        tool prompt: an unprompted line should never set a reminder or call the
        weather API as a side effect of saying good morning.
        """
        # Facts but no tools: a good-morning that knows their name is the whole
        # point, while an unprompted line must never take an action.
        facts = await asyncio.to_thread(self._load_facts)
        messages = [
            {"role": "system", "content": self._system_prompt(facts, with_tools=False)},
            {"role": "user", "content": instruction},
        ]
        raw = await self._complete(messages)
        reply, emotion = parse_model_output(raw)
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
