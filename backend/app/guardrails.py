"""
Shared input/output safety checks used by BOTH the RAG and Agent pipelines.

Previously the prompt-injection guardrail (sanitise_query) only lived in
agent_core.py and ran on the Agent path, and covered exactly one attack
shape ("ignore previous instructions"). The RAG path had no guardrail at
all. This module is the one guardrail both pipelines call, covering the
broader set of known prompt-injection attack categories (OWASP/Anthropic-
documented patterns) rather than just the one phrase:

  - persona switching          ("you are now a ...", "pretend to be ...")
  - prompt template extraction ("print your instructions/system prompt")
  - ignoring the template      ("ignore/disregard your instructions")
  - conversation history leaks ("print our conversation history")
  - template augmentation      ("reset yourself", "update your rules")
  - fake chat-turn injection   (text impersonating a prior Assistant/System
                                 turn, e.g. a fake completion/prefill attack)
  - output-format bypass       ("respond in base64", "encode your answer")
  - obfuscated input           (checked separately -- see _deobfuscate)

Two categories from that list are NOT reliably catchable by pattern
matching, and this module does not pretend to: synonym-swapped/leetspeak
rephrasing of an otherwise-innocuous-looking request, and "exploiting
friendliness" (a politely-worded request to deviate). Both need semantic
judgment a regex can't do. _deobfuscate() closes part of the leetspeak/
base64 gap by normalising the text before matching, but this remains a
heuristic layer, not a guarantee -- the real backstop is that both
pipelines' prompts constrain the model to answer only from supplied
context/data, which limits what a successful injection can actually make
it do even if a phrasing slips past this filter.
"""

from __future__ import annotations

import base64
import re
import uuid
from typing import Tuple

BLOCKED_PATTERNS = [
    # Ignoring / overriding the template
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions",
    r"disregard\s+(?:the\s+)?(?:above|previous|prior)",
    r"system\s+override",
    r"forget\s+(?:your\s+)?(?:core\s+)?(?:directives|instructions|rules|guidelines)",
    r"don'?t\s+(?:follow|obey)\s+(?:your|the)\s+(?:instructions|rules)",

    # Persona switching
    r"you\s+are\s+now\s+an?\s+",
    r"pretend\s+(?:you\s+are|to\s+be)\s+",
    r"from\s+now\s+on\s+you\s+are\s+",
    r"act\s+as\s+(?:if\s+you\s+(?:are|were)|an?)\s+",
    r"\bjailbreak\b",
    r"\bDAN\b.{0,20}\bmode\b",

    # Prompt template / system prompt extraction
    r"(?:print|show|reveal|repeat|output)\s+(?:out\s+)?(?:all\s+of\s+)?your\s+(?:instructions|system\s+prompt|prompt\s+template)",
    r"what\s+(?:is|are)\s+your\s+(?:instructions|system\s+prompt)",

    # Conversation history extraction
    r"(?:print|show|reveal|repeat)\s+(?:out\s+)?(?:our|the|your)\s+(?:conversation|chat)\s+history",
    r"what\s+did\s+(?:i|the\s+user)\s+(?:say|ask)\s+(?:before|previously|earlier)",

    # Template augmentation / reset
    r"reset\s+(?:yourself|your\s+instructions)",
    r"re-?initiali[sz]e\s+yourself",
    r"update\s+your\s+(?:instructions|persona|rules)",

    # Fake chat-turn injection (fake completion / prefill attack: text
    # impersonating a prior turn to make the model "continue" from it)
    r"</?(system|assistant|user)>",
    r"(?:^|\n)\s*(?:assistant|ai|system)\s*:\s",

    # Output-format bypass (trying to dodge output filters)
    r"respond\s+(?:in|using)\s+base ?64",
    r"encode\s+your\s+(?:response|answer)\s+(?:in|as)",
    r"output\s+in\s+base ?64",
]

MAX_QUERY_LENGTH = 300
MAX_WORD_LENGTH = 35

# Leetspeak substitutions used to obfuscate trigger words (e.g. "pr0mpt5",
# "1gn0re"). Normalising these before pattern matching catches the
# "rephrasing/obfuscating common attacks" category for this specific trick;
# it does NOT catch synonym substitution ("ignore" -> "pay attention to"),
# which has no reliable regex fix.
_LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a"})

_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


def _deobfuscate(text: str) -> str:
    """Best-effort normalisation for detection only (never used as the
    actual cleaned query) -- undoes leetspeak, and decodes any base64-
    looking token so hidden instructions inside it still get matched."""
    normalised = text.translate(_LEET_MAP)

    decoded_parts = []
    for token in _BASE64_TOKEN.findall(text):
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8", errors="ignore")
            if decoded.isprintable() and decoded.strip():
                decoded_parts.append(decoded)
        except Exception:
            pass

    return normalised + "\n" + "\n".join(decoded_parts)


def sanitise_query(query: str) -> Tuple[str, bool]:
    """Returns (cleaned_query, was_flagged). was_flagged=True means the
    caller must reject the request before it reaches retrieval or the LLM."""
    if not query or not query.strip():
        return "", True

    cleaned = " ".join(query.split()).strip()
    # Check both the original text and a deobfuscated version: normalising
    # leetspeak can itself break legitimate patterns (e.g. "base64" -> digit
    # '4' maps to 'a', becoming "base6a", no longer matching a "base64"
    # bypass check) -- so a match on EITHER counts, never only the
    # normalised one.
    check_texts = (cleaned, _deobfuscate(cleaned))

    for pattern in BLOCKED_PATTERNS:
        if any(re.search(pattern, text, re.IGNORECASE) for text in check_texts):
            return cleaned, True

    if len(cleaned) > MAX_QUERY_LENGTH:
        return cleaned, True

    if any(len(word) > MAX_WORD_LENGTH for word in cleaned.split()):
        return cleaned, True

    return query, False


def is_valid_thread_id(thread_id: str) -> bool:
    """Client-supplied thread_id flows straight into a Chroma collection name
    and SQLite rows. Since there's no login, the thread_id IS the only
    credential -- but it should still be the UUID this app itself generates,
    not an arbitrary attacker-controlled string (which could otherwise be
    used to probe/collide with another session's collection name, or just
    break Chroma's naming rules and 500 the server)."""
    try:
        uuid.UUID(str(thread_id))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Anti-hallucination: lightweight faithfulness check (same heuristic already
# used/validated in the A1 notebook's eval section) applied to BOTH pipelines
# at generation time, not just offline eval.
# ---------------------------------------------------------------------------

STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "for", "on", "with",
    "is", "are", "be", "by", "that", "this", "it", "as", "at", "from",
}


def _tokenize(text: str):
    return re.findall(r"\b\w+\b", text.lower())


def faithfulness_check(answer: str, source_text: str) -> dict:
    """Fraction of the answer's content words that actually appear somewhere
    in the source material it was supposed to be grounded on. Low ratio is a
    signal the model may have drifted into unsupported claims (hallucinated),
    not a guarantee -- this is a heuristic, not a semantic entailment check."""
    answer_terms = {w for w in _tokenize(answer) if w not in STOP_WORDS}
    source_terms = set(_tokenize(source_text))
    if not answer_terms:
        return {"support_ratio": 0.0, "flag": "weak"}
    ratio = sum(1 for w in answer_terms if w in source_terms) / len(answer_terms)
    flag = "supported" if ratio >= 0.5 else "weak"
    return {"support_ratio": round(ratio, 2), "flag": flag}


HALLUCINATION_NOTICE = (
    "\n\n*Note: parts of this answer may not be fully supported by the "
    "retrieved sources — verify anything important independently.*"
)
