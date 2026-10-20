"""
One place that decides which LLM both pipelines (RAG + Agent) talk to.

Moved here from rag_core/agent_core (which each hard-coded a local Ollama
qwen2.5:7b) so the cloud deployment can switch model provider with
environment variables instead of code changes:

  LLM_PROVIDER=gemini  (default) -> Google Gemini via the hosted API.
                                    Needs GEMINI_API_KEY. A 7B model on CPU
                                    needed a large VM and answered in 20-60s;
                                    the hosted free tier is ~1-3s and costs
                                    nothing extra on GCP.
  LLM_PROVIDER=ollama            -> the original local Ollama setup, kept so
                                    the app still runs fully offline/free on
                                    a dev machine. OLLAMA_HOST and
                                    OLLAMA_MODEL override the defaults.

GEMINI_API_KEY is deliberately separate from GOOGLE_API_KEY, which is
already used by google_search.py for the Custom Search tool.
"""

from __future__ import annotations

import logging
import os
from typing import Dict

from langchain_core.messages import HumanMessage

log = logging.getLogger("uvicorn.error")

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()
# gemini-3.8-flash timed out (504) even for a one-word question on the free
# tier, while gemini-3.5-flash answered in a few seconds.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
# On the free tier Google sometimes answers 503/504 (busy) or 429 (that
# model's limit) for one model while others still work, and each model has
# its own limit. Such a call is tried once more on this model; leave it empty
# to turn the fallback off.
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Same ceiling as before -- see the old rag_core.get_llm comment: without an
# explicit limit, Ollama's own default (~128 tokens) cut longer answers off.
MAX_OUTPUT_TOKENS = 4096

# Every answer has to fit inside nginx's 180 s proxy timeout (frontend/
# nginx.conf), and a call can be tried on the fallback model too, so each
# attempt gets up to 60 s. Without a limit the Gemini client waits
# indefinitely and retries a 429 several times, so the browser gives up while
# the backend is still working. gemini-3.5-flash answers in a few seconds;
# a call that takes a minute has stalled on Google's side.
# LLM_MAX_RETRIES counts attempts for Gemini, so 1 means no automatic retry.
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
LLM_MAX_RETRIES = 1

# Gemini 3 models "think" before answering, at a high level by default, which
# made some answers take over 50 s (Google then returns 504 DEADLINE_EXCEEDED).
# Answering from retrieved docs or search results needs little reasoning, so
# use a low level; GEMINI_THINKING_LEVEL can raise it (minimal|low|medium|high).
GEMINI_THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "low").lower()

_llms: Dict[tuple, object] = {}


def _build(temperature: float, model: str):
    if LLM_PROVIDER == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_HOST,
                          temperature=temperature, num_predict=MAX_OUTPUT_TOKENS,
                          client_kwargs={"timeout": LLM_TIMEOUT_SECONDS})

    from langchain_google_genai import ChatGoogleGenerativeAI
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set.")
    return ChatGoogleGenerativeAI(model=model, google_api_key=api_key,
                                  temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS,
                                  timeout=LLM_TIMEOUT_SECONDS, max_retries=LLM_MAX_RETRIES,
                                  thinking_level=GEMINI_THINKING_LEVEL)


def status() -> dict:
    """What the UI shows in its model pill: which provider/model answers, and
    whether it's configured (for Gemini: an API key is set)."""
    if LLM_PROVIDER == "ollama":
        return {"provider": "ollama", "model": OLLAMA_MODEL, "configured": True}
    return {"provider": "gemini", "model": GEMINI_MODEL,
            "configured": bool(os.environ.get("GEMINI_API_KEY"))}


def get_llm(temperature: float = 0.0, model: str = GEMINI_MODEL):
    key = (temperature, model)
    if key not in _llms:
        _llms[key] = _build(temperature, model)
    return _llms[key]


def is_quota_error(exc: BaseException) -> bool:
    """True when the provider refused the call for rate/quota reasons (HTTP
    429). Every user shares one API key, so this is the app-wide free-tier
    limit, not something the current user did."""
    text = f"{type(exc).__name__} {exc}"
    return any(s in text for s in ("429", "RESOURCE_EXHAUSTED", "ResourceExhausted", "quota"))


def is_unavailable_error(exc: BaseException) -> bool:
    """True when the provider timed out or failed on its side (a 5xx such as
    504 DEADLINE_EXCEEDED or 503 UNAVAILABLE, or no reply in time). Worth
    retrying in a moment, and not a bug in the app."""
    text = f"{type(exc).__name__} {exc}"
    return any(s in text for s in (
        "DEADLINE_EXCEEDED", "UNAVAILABLE", "INTERNAL", "Timeout", "timed out",
        "500 ", "502 ", "503 ", "504 ",
    ))


UNAVAILABLE_MESSAGE = ("The AI model took too long to answer or is busy right now. "
                       "Please try again in a moment.")


QUOTA_MESSAGE = ("The AI model's free usage limit has been reached for now. "
                 "Please try again in a minute, or later today if the daily limit is used up.")


def generate(prompt: str, temperature: float = 0.0) -> str:
    """Single-prompt completion returning plain text. Gemini can return
    content as a list of parts rather than a string, so normalise here
    instead of at every call site."""
    messages = [HumanMessage(content=prompt)]
    try:
        content = get_llm(temperature).invoke(messages).content
    except Exception as e:
        if (LLM_PROVIDER == "ollama" or not GEMINI_FALLBACK_MODEL
                or GEMINI_FALLBACK_MODEL == GEMINI_MODEL
                or not (is_unavailable_error(e) or is_quota_error(e))):
            raise
        log.warning("%s failed (%s); retrying on %s", GEMINI_MODEL,
                    str(e)[:120], GEMINI_FALLBACK_MODEL)
        content = get_llm(temperature, GEMINI_FALLBACK_MODEL).invoke(messages).content
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content.strip()
