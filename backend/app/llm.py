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

import os
from typing import Dict

from langchain_core.messages import HumanMessage

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Same ceiling as before -- see the old rag_core.get_llm comment: without an
# explicit limit, Ollama's own default (~128 tokens) cut longer answers off.
MAX_OUTPUT_TOKENS = 4096

_llms: Dict[float, object] = {}


def _build(temperature: float):
    if LLM_PROVIDER == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_HOST,
                          temperature=temperature, num_predict=MAX_OUTPUT_TOKENS)

    from langchain_google_genai import ChatGoogleGenerativeAI
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set.")
    return ChatGoogleGenerativeAI(model=GEMINI_MODEL, google_api_key=api_key,
                                  temperature=temperature, max_output_tokens=MAX_OUTPUT_TOKENS)


def get_llm(temperature: float = 0.0):
    if temperature not in _llms:
        _llms[temperature] = _build(temperature)
    return _llms[temperature]


def generate(prompt: str, temperature: float = 0.0) -> str:
    """Single-prompt completion returning plain text. Gemini can return
    content as a list of parts rather than a string, so normalise here
    instead of at every call site."""
    content = get_llm(temperature).invoke([HumanMessage(content=prompt)]).content
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content.strip()
