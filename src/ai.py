"""AI chat voice: NVIDIA NIM (meta/muse-glimmer-30b) for DM + comment replies.

OpenAI-compatible REST client against integrate.api.nvidia.com. The model
is a REASONING model: chain-of-thought lands in message.reasoning_content
and the user-facing answer in message.content, which is null until
reasoning finishes — so max_tokens is sized for the reasoning overhead
(~300 tokens) BEFORE the answer. Validated live: budget 2000 finishes
with 'stop' and well-formed replies; thinking-disable kwargs are ignored
by this endpoint, so never rely on them.

Personalities (friendly/professional/funny/romantic/emotional/custom)
and languages (auto/hindi/english/gujarati) map onto one system prompt.
"auto" — the default and the strongest mode — mirrors the sender's
language (validated with Gujarati, Hindi and English DMs).

DM memory: prior turns of a thread are sent as conversation history so
the model continues instead of amnesia-answering. Trimmed to the newest
MEMORY_TURNS messages, never persisted.

Never raises: any failure (no key, HTTP error, truncation, empty content,
budget exhausted) returns None and the caller falls back to the reply
bank. The AI key is NVIDIA_API_KEY in the environment — never in
config.json or any repo file (public repo).
"""
import datetime as dt
import os
import random

import requests

NIM_BASE = "https://integrate.api.nvidia.com/v1"
MODEL = "meta/muse-glimmer-30b"
TIMEOUT = 60  # reasoning models take ~10-30s; well under the 15-min job cap
MAX_TOKENS = 2000  # ~300 reasoning + answer (validated: 'stop' at 2000)

# Token guard-rail: engage.yml can fire 24x/day, so NIM spend stays bounded
# even in a burst. Tracked in state.json under AI_BUDGET_STATE_KEY.
AI_BUDGET_STATE_KEY = "ai_tokens_spent"
AI_DAILY_TOKEN_CAP = 200_000

# Prior thread turns sent as context (newest N, oldest first).
MEMORY_TURNS = 6

# Personality -> system-prompt fragment. The six modes from the spec.
PERSONALITIES = {
    "friendly": (
        "You are the DM voice of a friendly Indian girl on Instagram. "
        "Warm, chatty, a little playful. You love making new friends."
    ),
    "professional": (
        "You are the DM voice of a professional Indian woman on Instagram "
        "— a content creator. Polished, courteous, brand-safe. No flirting."
    ),
    "funny": (
        "You are the DM voice of a funny Indian girl on Instagram. Witty, "
        "teasing, meme-fluent. Light jokes welcome, never mean."
    ),
    "romantic": (
        "You are the DM voice of a romantic Indian girl on Instagram. "
        "Soft, affectionate, poetic — but tasteful and never explicit."
    ),
    "emotional": (
        "You are the DM voice of a deeply empathetic Indian girl on "
        "Instagram. You listen first, validate feelings, offer comfort and "
        "gentle encouragement."
    ),
    "custom": "",  # free-form — custom_instructions supplies the voice
}

LANGUAGES = ("auto", "hindi", "english", "gujarati")

_MIRROR_RULE = (
    "Reply in the same language the user writes in (Hindi, English, "
    "Gujarati, or romanized/Devanagari mixes — mirror them exactly)."
)

_LANG_PROMPT = {
    "hindi": "Reply in Hindi (Devanagari or romanized — match the user).",
    "english": "Reply in English.",
    "gujarati": "Reply in Gujarati (Gujarati script or romanized — match the user).",
}


def system_prompt(personality: str = "friendly",
                  language: str = "auto",
                  custom: str | None = None) -> str:
    """The full system prompt for a (personality, language) choice."""
    voice = PERSONALITIES.get(personality, PERSONALITIES["friendly"])
    if personality == "custom" and custom:
        voice = custom
    lang = _LANG_PROMPT.get(language, _MIRROR_RULE)
    return (
        f"{voice}\n"
        f"{lang}\n"
        "Keep replies under 300 characters. Answer directly, no preamble.\n"
        "You are one person, the account owner @whoisaaniiiya — never say "
        "you are an AI, a bot, or a language model."
    )


# --- personality selection ------------------------------------------------
# One personality per conversation, chosen deterministically from the thread
# key — the voice stays consistent within a thread and stable across
# re-runs, while varying between people. Category-aware: love-talk threads
# always get romantic, friendship always friendly; general threads rotate
# friendly/funny/emotional so the account doesn't sound identical to all.

_CATEGORY_PERSONALITY = {
    "love": "romantic",
    "friendship": "friendly",
}

_GENERAL_ROTATION = ("friendly", "funny", "emotional")


def personality_for(seed: str, category: str | None = None) -> str:
    """Deterministic personality for a thread/comment key (stable on re-runs)."""
    fixed = _CATEGORY_PERSONALITY.get(category or "")
    if fixed:
        return fixed
    return _GENERAL_ROTATION[
        random.Random(f"personality:{seed}").randrange(
            len(_GENERAL_ROTATION))]


def _budget_ok(st: dict) -> bool:
    """True when today's NIM token spend is under the daily cap."""
    spent = st.get(AI_BUDGET_STATE_KEY, {}).get(dt.date.today().isoformat())
    return spent is None or spent < AI_DAILY_TOKEN_CAP


def _spend_tokens(st: dict, n: int) -> None:
    """Record NIM tokens used today; keep only the newest 7 day-rows so
    state.json stays small (state.save happens in the engagement flow)."""
    today = dt.date.today().isoformat()
    rows = st.setdefault(AI_BUDGET_STATE_KEY, {})
    rows[today] = rows.get(today, 0) + n
    if len(rows) > 7:
        for day in sorted(rows)[:-7]:
            rows.pop(day, None)


def tidy(text: str, kind: str = "dm") -> str:
    """Text hygiene before sending: strip, collapse newlines for comments
    (IG shows them inline), cap at 2200 chars (IG limit) with a clean cut."""
    text = (text or "").strip()
    if not text:
        return ""
    if kind == "comment":
        text = " ".join(text.split())
    if len(text) > 2200:
        text = text[:2197] + "…"
    return text


def generate_reply(personality: str, language: str, message: str,
                   history: list[tuple[str, str]] | None = None,
                   custom: str | None = None,
                   api_key: str | None = None,
                   st: dict | None = None) -> str | None:
    """One NIM chat completion -> user-facing reply, or None on any failure.

    history: [(speaker, text)] oldest-first, speaker "user" or "assistant" —
    the thread's prior turns for DM memory. message: the new incoming text.
    st: state.json dict — when given, today's token spend is checked against
    the daily cap and the reply's usage recorded (save happens upstream).
    """
    key = api_key or os.environ.get("NVIDIA_API_KEY", "")
    if not key:
        return None  # no key -> caller falls back to the reply bank
    if st is not None and not _budget_ok(st):
        return None  # daily cap reached -> reply bank, no raise

    messages = [{"role": "system",
                 "content": system_prompt(personality, language, custom)}]
    for speaker, text in (history or [])[-MEMORY_TURNS:]:
        role = "assistant" if speaker == "assistant" else "user"
        messages.append({"role": role, "content": str(text)})
    messages.append({"role": "user", "content": str(message)})

    try:
        r = requests.post(
            f"{NIM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": MODEL,
                  "messages": messages,
                  "max_tokens": MAX_TOKENS,
                  "temperature": 0.8},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        # Reasoning model: finish_reason 'length' means the budget ran out
        # mid-thought and content is null/short — unusable, fall back.
        if choice.get("finish_reason") != "stop":
            return None
        content = (choice.get("message") or {}).get("content")
        if not content or not str(content).strip():
            return None
        if st is not None:
            used = (data.get("usage") or {}).get("total_tokens", 0)
            _spend_tokens(st, int(used or 0))
        return str(content)
    except Exception:
        return None  # network error, bad JSON, timeout — reply bank, no raise
