"""
RAG pipeline (BM25 + dense + ChromaDB + hybrid RRF). The retrieval core was
first written by the author for an earlier course (COMP2701); everything
below "Changes ..." was added for this cloud deployment.

Changes from the original notebook:
  - generate_answer_from_context() no longer returns a raw chunk via keyword-overlap
    heuristic. It now calls the configured LLM (see llm.py: hosted Gemini, or
    local Ollama) with the grounded prompt built by build_prompt(), exactly
    like the Agent pipeline does.
  - Added support for per-session uploaded documents that are indexed ON TOP of the
    base OrbitOps corpus (not a replacement), capped at MAX_UPLOADS documents.
    Each chat session (thread_id) gets its own Chroma collection name so uploads
    from one user never leak into another user's retrieval.

Changes for running as several replicas on Kubernetes:
  - Chroma is reached over HTTP (CHROMA_HOST/CHROMA_PORT) when configured, so
    every backend pod shares one vector store instead of each keeping its own
    copy on local disk. Without CHROMA_HOST it falls back to the on-disk
    PersistentClient for local development.
  - Uploaded documents are persisted in the chat database (store.py). Each
    pod's in-memory RagSession (BM25 + dense index) syncs from there, so an
    upload handled by one pod is visible to questions answered by another.
  - Chroma writes use upsert() rather than reset + add, so two pods building
    the same session's collection can't wipe or duplicate each other's work.
"""

from __future__ import annotations

import os
import re
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import chromadb
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from . import guardrails
from . import llm
from . import store

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "docs"
CHROMA_DIR = BASE_DIR / "chroma_store"
BASE_COLLECTION_NAME = "orbitops_base"
MAX_UPLOADS = 10
EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_embed_model: Optional[SentenceTransformer] = None


def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return _embed_model


def warm_up() -> None:
    """Load the embedding model at startup instead of on the first question.
    The pod only reports ready after this, so a new replica (scale-out,
    rolling update, rescheduled after a node failure) never makes a user
    wait for the model to load."""
    get_embed_model().encode(["warm up"])


@dataclass
class Document:
    text: str
    metadata: Dict[str, Any]


# ---------------------------------------------------------------------------
# Loading & chunking (unchanged from the original version)
# ---------------------------------------------------------------------------

def parse_metadata_and_text(raw_text: str) -> dict:
    parts = raw_text.split("\n\n", 1)
    header_section = parts[0]
    body_text = parts[1] if len(parts) > 1 else ""

    doc_data = {
        "doc_id": None,
        "title": None,
        "date": None,
        "team": None,
        "tags": None,
        "text": body_text.strip(),
    }

    for line in header_section.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            clean_key = key.strip().lower().replace(" ", "_")
            clean_value = value.strip()
            if clean_key in doc_data:
                doc_data[clean_key] = clean_value

    return doc_data


def load_documents(data_dir: Path) -> List[dict]:
    all_docs = []
    if not data_dir.exists():
        return all_docs
    for file_path in sorted(data_dir.glob("*.txt")):
        with open(file_path, "r", encoding="utf-8") as f:
            raw_content = f.read()
            doc_dict = parse_metadata_and_text(raw_content)
            doc_dict["source_file"] = file_path.name
            all_docs.append(doc_dict)
    return all_docs


def clean_text(text: str) -> str:
    text = text.replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    return "\n".join(non_empty)


def chunk_text(text: str, chunk_size: int = 480, chunk_overlap: int = 80) -> List[str]:
    """Overlapping character-window chunking (same algorithm as the original
    version; the window size itself was raised from the original 180 chars
    to 480. 180 chars is short enough to slice a sentence apart mid-fact
    (e.g. "between 2018 and 2040" split across two separate chunks), and
    retrieval then hands the LLM disconnected fragments it can misattribute
    or splice together incorrectly -- a real accuracy problem on longer
    real-world uploaded documents, not just a quality tweak)."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    if chunk_overlap < 0:
        raise ValueError("overlap must be greater than 0")
    if chunk_size < chunk_overlap:
        raise ValueError("chunk_size must be greater than overlap")
    chunks = []
    for i in range(0, len(text), chunk_size - chunk_overlap):
        chunks.append(text[i:i + chunk_size])
    return chunks


def chunk_documents(documents: List[dict], chunk_size: int = 480, chunk_overlap: int = 80,
                     doc_id_prefix: str = "") -> List[Document]:
    chunked_docs = []
    for doc_dict in documents:
        cleaned_body = clean_text(doc_dict["text"])
        text_chunks = chunk_text(cleaned_body, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        doc_id = doc_id_prefix + str(doc_dict.get("doc_id") or doc_dict.get("source_file"))
        for i, chunk_content in enumerate(text_chunks):
            chunk_metadata = {
                "doc_id": doc_id,
                "title": doc_dict.get("title") or doc_dict.get("source_file"),
                "source_file": doc_dict.get("source_file"),
                "date": doc_dict.get("date"),
                "team": doc_dict.get("team"),
                "tags": doc_dict.get("tags"),
                "chunk_id": f"{doc_id}_chunk_{i}",
            }
            chunked_docs.append(Document(text=chunk_content, metadata=chunk_metadata))
    return chunked_docs


def simple_tokenize(text: str) -> List[str]:
    return re.findall(r"\b\w+\b", text.lower())


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

def build_bm25_index(chunks: List[Document]):
    tokenized_corpus = [simple_tokenize(doc.text) for doc in chunks]
    bm25_index = BM25Okapi(tokenized_corpus)
    return bm25_index, tokenized_corpus


def bm25_retrieve(query: str, chunks: List[Document], bm25_index, top_k: int = 5) -> List[Document]:
    if not chunks:
        return []
    tokenized_query = simple_tokenize(query)
    scores = bm25_index.get_scores(tokenized_query)
    top_indices = np.argsort(scores)[::-1][:top_k]
    return [chunks[i] for i in top_indices]


# ---------------------------------------------------------------------------
# Dense retrieval
# ---------------------------------------------------------------------------

def build_dense_index(chunks: List[Document], model) -> np.ndarray:
    if not chunks:
        return np.zeros((0, 384))
    texts = [doc.text for doc in chunks]
    return model.encode(texts, convert_to_numpy=True, show_progress_bar=False)


def dense_retrieve(query: str, chunks: List[Document], embeddings: np.ndarray, model, top_k: int = 5) -> List[Document]:
    if not chunks or embeddings.shape[0] == 0:
        return []
    query_embedding = model.encode([query], convert_to_numpy=True)
    similarities = cosine_similarity(query_embedding, embeddings)[0]
    top_indices = np.argsort(similarities)[::-1][:top_k]
    return [chunks[i] for i in top_indices]


# ---------------------------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------------------------

CHROMA_HOST = os.environ.get("CHROMA_HOST", "")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))

_client = None


def get_client():
    global _client
    if _client is None:
        if CHROMA_HOST:
            _client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        else:
            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client


def reset_collection(collection_name: str) -> None:
    client = get_client()
    existing = {c.name for c in client.list_collections()}
    if collection_name in existing:
        client.delete_collection(collection_name)


def get_or_create_collection(collection_name: str):
    client = get_client()
    return client.get_or_create_collection(name=collection_name)


def index_documents_chroma(collection_name: str, chunked_docs: List[Document], model) -> None:
    if not chunked_docs:
        return
    collection = get_or_create_collection(collection_name)
    texts = [doc.text for doc in chunked_docs]
    ids = [doc.metadata.get("chunk_id") for doc in chunked_docs]
    # Chroma rejects None metadata values (e.g. an uploaded doc with no
    # Date/Team/Tags header) — coerce to empty strings before indexing.
    metadatas = [
        {k: ("" if v is None else v) for k, v in doc.metadata.items()}
        for doc in chunked_docs
    ]
    embeddings = model.encode(texts, convert_to_numpy=True).tolist()
    collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=texts)


def chroma_retrieve(collection_name: str, query: str, top_k: int = 5) -> dict:
    collection = get_or_create_collection(collection_name)
    if collection.count() == 0:
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    model = get_embed_model()
    query_embedding = model.encode([query], convert_to_numpy=True).tolist()
    return collection.query(query_embeddings=query_embedding, n_results=min(top_k, collection.count()))


# ---------------------------------------------------------------------------
# Hybrid RRF fusion
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(rank_lists: List[List[str]], rrf_k: int = 60) -> List[str]:
    scores: Dict[str, float] = {}
    for ranked in rank_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (rrf_k + rank)
    return [item for item, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def docs_to_chunk_ids(results: List[Document]) -> List[str]:
    return [doc.metadata["chunk_id"] for doc in results]


def chroma_chunk_ids(results: dict) -> List[str]:
    if not results.get("metadatas") or not results["metadatas"][0]:
        return []
    return [meta["chunk_id"] for meta in results["metadatas"][0]]


def hybrid_retrieve(query: str, chunks: List[Document], bm25_index, embeddings: np.ndarray, model,
                     collection_name: str, top_k: int = 5, rrf_k: int = 60) -> List[Document]:
    # Fusion + the final lookup must key on chunk_id (unique per chunk), not
    # doc_id (shared by every chunk of the same document). Keying on doc_id
    # collapsed RRF's ranked list down to one entry per document -- for any
    # document with more than one chunk (i.e. almost all of them), this
    # silently returned a single arbitrary chunk no matter what top_k was,
    # regardless of which chunks BM25/dense/Chroma actually ranked highly.
    bm25_results = bm25_retrieve(query, chunks, bm25_index, top_k=top_k * 2)
    bm25_ids = docs_to_chunk_ids(bm25_results)

    chroma_results = chroma_retrieve(collection_name, query, top_k=top_k * 2)
    chroma_ids = chroma_chunk_ids(chroma_results)

    fused_ids = reciprocal_rank_fusion([bm25_ids, chroma_ids], rrf_k=rrf_k)
    top_fused_ids = fused_ids[:top_k]

    chunk_lookup = {doc.metadata["chunk_id"]: doc for doc in chunks}
    return [chunk_lookup[chunk_id] for chunk_id in top_fused_ids if chunk_id in chunk_lookup]


# ---------------------------------------------------------------------------
# Prompting + real LLM generation (replaces the original heuristic stub)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Exact word-count queries -- RAG only ever sees a handful of retrieved
# chunks, never the whole document, so asking the LLM "how many times does
# X occur" counts occurrences within that small slice and reports it as if
# it were the real total. That's not the model inventing facts -- it's
# answering a question it structurally cannot answer from what it was
# shown. Counting across an entire document is a different kind of task
# (exact, exhaustive) than semantic retrieval (relevant, partial), so it's
# handled separately here: detect the question shape and count directly
# over the full stored document text instead of going through the LLM.
# ---------------------------------------------------------------------------

_COUNT_QUERY_RE = re.compile(
    r"how many times\s+(?:does|did|is|was|has|have)?\s*(?:the word|the term)?\s*"
    r"[\"'“”]?([A-Za-z0-9][A-Za-z0-9\-']*)[\"'“”]?\s+"
    r"(?:appear|occur|show up|come up|appears|occurs|used|appeared|occurred)",
    re.IGNORECASE,
)


def extract_count_target(query: str) -> Optional[str]:
    """Returns the word/term being counted if the query is asking for an
    exact occurrence count, else None."""
    match = _COUNT_QUERY_RE.search(query)
    return match.group(1) if match else None


def count_occurrences(term: str, raw_docs: Dict[str, str]) -> int:
    pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    return sum(len(pattern.findall(text)) for text in raw_docs.values())


def build_context(results: List[Document], max_chunks: int = 4) -> str:
    selected = results[:max_chunks]
    parts = []
    for doc in selected:
        title = doc.metadata.get("title", "Untitled")
        doc_id = doc.metadata.get("doc_id", "unknown")
        parts.append(f"[{doc_id}] {title}\n{doc.text}")
    return "\n\n".join(parts)


HISTORY_TURNS = 6           # earlier messages shown to the model
HISTORY_CHARS = 600         # per message, so a long answer can't crowd out the docs


def format_history(history: Optional[List[dict]]) -> str:
    """The last few messages of this chat as plain text, oldest first, so
    follow-ups ("what about step 2?") and things the user said earlier
    ("call me Key") are understood."""
    lines = []
    for m in (history or [])[-HISTORY_TURNS:]:
        who = "User" if m.get("role") == "user" else "Assistant"
        text = " ".join((m.get("content") or "").split())
        lines.append(f"{who}: {text[:HISTORY_CHARS]}")
    return "\n".join(lines)


def build_prompt(question: str, context: str, history: str = "") -> str:
    conversation = f"""
EARLIER IN THIS CHAT:
{history}
---
""" if history else ""
    return f"""You are the OrbitOps Assistant, a technical assistant for the OrbitOps team.
Use the provided context to answer the user's question.
{conversation}
---
CONTEXT:
{context}
---

INSTRUCTIONS:
1. Answer questions about OrbitOps using ONLY the information in the context above.
2. Use the earlier messages to understand follow-up questions, and to answer
   questions about the chat itself (for example a name the user asked you to use).
3. If the answer is in neither, state that you do not have enough information.
4. Reference the [doc_id] and title when providing specific details. Write
   citations as plain text like [OO-001]; never add links or URLs.

QUESTION: {question}

ANSWER:"""


def build_no_context_prompt(question: str, history: str = "") -> str:
    """Used when retrieval finds nothing to ground on -- either no documents
    are indexed yet, or the question is just conversational ("hi", "thanks").
    Without this, the bot could never reply to a plain greeting: it would hit
    the hardcoded 'Insufficient information' message below without ever
    calling the LLM."""
    return f"""You are a helpful assistant for the OrbitOps team. No indexed
documents matched this message (either none are indexed yet, or the message
isn't a documentation question).

- If this is a greeting or general chat, just respond naturally and briefly,
  taking the earlier messages below into account.
- If it reads like a real question about OrbitOps docs/procedures, say you
  don't have any indexed documents to answer that from yet, and suggest the
  user upload one via Attach.

EARLIER IN THIS CHAT:
{history or "(none)"}

MESSAGE: {question}

ANSWER:"""


def generate_answer_from_context(query: str, retrieved_chunks: List[Document], history: str = "") -> str:
    """Real local LLM call (qwen2.5:7b via Ollama). Always calls the LLM --
    previously this returned a hardcoded string with no retrieved chunks,
    which meant a plain "hi" (no docs indexed, no retrieval hits) never
    reached the model at all."""
    if not retrieved_chunks:
        prompt = build_no_context_prompt(query, history)
    else:
        context = build_context(retrieved_chunks, max_chunks=12)
        prompt = build_prompt(query, context, history)

    return llm.generate(prompt)


# ---------------------------------------------------------------------------
# Session-scoped index: base corpus + per-session uploaded docs
# ---------------------------------------------------------------------------

class RagSession:
    """One retrieval index per chat thread: base OrbitOps corpus + up to
    MAX_UPLOADS uploaded documents, indexed on top of (not replacing) the base
    corpus, as decided for this project."""

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.collection_name = f"session_{thread_id}"
        self.model = get_embed_model()
        # Database ids of the uploads indexed on this pod, so additions and
        # removals made through any replica are picked up on the next sync.
        self.indexed_upload_ids: set = set()
        # Requests for one chat can arrive together on this pod. Changes to
        # the index happen under this lock and replace the lists/dicts rather
        # than editing them, so a search running meanwhile keeps a
        # consistent snapshot (see _snapshot).
        self._lock = threading.RLock()

        base_docs = load_documents(DATA_DIR)
        self.chunks: List[Document] = chunk_documents(base_docs)

        # Full, unchunked text per doc_id -- kept alongside the chunked index
        # so exact-count queries ("how many times does X appear") can scan
        # the whole document directly instead of only the handful of chunks
        # retrieval happens to surface (retrieval answers "what's relevant",
        # not "count every occurrence" -- those are different tasks).
        self.raw_docs: Dict[str, str] = {
            (doc.get("doc_id") or doc.get("source_file")): doc["text"] for doc in base_docs
        }

        self.bm25_index, _ = build_bm25_index(self.chunks) if self.chunks else (None, None)
        self.embeddings = build_dense_index(self.chunks, self.model)

        index_documents_chroma(self.collection_name, self.chunks, self.model)
        self.sync_uploads()

    def add_uploaded_document(self, filename: str, raw_text: str) -> None:
        # The limit is checked by the database in the same step as the insert,
        # so two uploads arriving together on any replicas can't both take
        # the last slot.
        if not store.add_upload_if_room(self.thread_id, filename, raw_text, MAX_UPLOADS):
            raise ValueError(f"Upload limit reached ({MAX_UPLOADS} documents max per session).")
        self.sync_uploads()

    @property
    def uploaded_count(self) -> int:
        return len(self.indexed_upload_ids)

    def sync_uploads(self) -> None:
        """Bring this pod's index in line with the thread's uploads in the
        database: index new ones and drop removed ones, whichever replica
        handled the change."""
        with self._lock:
            current = set(store.get_upload_ids(self.thread_id))
            removed = self.indexed_upload_ids - current
            if removed:
                self._remove_uploads(removed)
            for upload_id in sorted(current - self.indexed_upload_ids):
                upload = store.get_upload(self.thread_id, upload_id)
                if upload:
                    self._index_upload(upload_id, upload["filename"], upload["content"])

    def _snapshot(self):
        """Chunks, BM25 index and full texts that belong together."""
        with self._lock:
            return self.chunks, self.bm25_index, self.raw_docs

    def _remove_uploads(self, upload_ids: set) -> None:
        doc_ids = {f"upload_{i}" for i in upload_ids}
        keep = [i for i, c in enumerate(self.chunks) if c.metadata["doc_id"] not in doc_ids]
        gone = [c.metadata["chunk_id"] for c in self.chunks if c.metadata["doc_id"] in doc_ids]
        self.chunks = [self.chunks[i] for i in keep]
        self.embeddings = self.embeddings[keep] if len(keep) else self.embeddings[:0]
        self.raw_docs = {k: v for k, v in self.raw_docs.items() if k not in doc_ids}
        self.indexed_upload_ids = self.indexed_upload_ids - upload_ids
        self.bm25_index, _ = build_bm25_index(self.chunks) if self.chunks else (None, None)
        if gone:
            try:
                get_or_create_collection(self.collection_name).delete(ids=gone)
            except Exception:
                # Another replica may have deleted them already; stale ids
                # are also filtered out at lookup time in hybrid_retrieve.
                pass

    def _index_upload(self, upload_id: int, filename: str, raw_text: str) -> None:
        doc_dict = {
            "doc_id": f"upload_{upload_id}",
            "title": filename,
            "date": None,
            "team": None,
            "tags": "uploaded",
            "text": raw_text,
            "source_file": filename,
        }
        new_chunks = chunk_documents([doc_dict], doc_id_prefix="")
        self.chunks = self.chunks + new_chunks
        self.raw_docs = {**self.raw_docs, doc_dict["doc_id"]: raw_text}
        self.indexed_upload_ids = self.indexed_upload_ids | {upload_id}

        # Rebuild BM25 (cheap at this corpus size) and extend dense + chroma indexes.
        self.bm25_index, _ = build_bm25_index(self.chunks)
        new_embeddings = build_dense_index(new_chunks, self.model)
        if self.embeddings.shape[0] == 0:
            self.embeddings = new_embeddings
        else:
            self.embeddings = np.vstack([self.embeddings, new_embeddings])
        index_documents_chroma(self.collection_name, new_chunks, self.model)

    def retrieve(self, query: str, top_k: int = 4) -> List[Document]:
        """Hybrid retrieval only (no LLM call) -- used by cluster_ops to pull
        the runbooks relevant to a live incident."""
        clean_query, flagged = guardrails.sanitise_query(query)
        if flagged:
            return []
        chunks, bm25_index, _ = self._snapshot()
        return hybrid_retrieve(
            clean_query, chunks, bm25_index, self.embeddings, self.model,
            self.collection_name, top_k=top_k,
        )

    def answer(self, query: str, top_k: int = 12, history: Optional[List[dict]] = None) -> dict:
        # top_k was 4 (fine for the short original OrbitOps docs, ~180-char
        # chunks each). Once uploads can be full reports (10-20 pages, 150+
        # chunks), 4 is too small a slice to reliably contain the right
        # chunk -- raised to 12 so longer uploaded documents get a fair
        # shot at retrieval without touching the original chunking itself.
        # Guardrail: previously only the Agent path sanitised input. With
        # multiple strangers able to reach this app once deployed, the RAG
        # path needed the same prompt-injection/length checks.
        clean_query, flagged = guardrails.sanitise_query(query)
        chunks, bm25_index, raw_docs = self._snapshot()
        if flagged:
            return {
                "answer": "This message was rejected by input safety checks.",
                "citations": [],
                "docs_indexed": len(set(c.metadata["doc_id"] for c in chunks)),
            }

        # Exact-count questions ("how many times does X appear/occur") can't
        # be answered from a handful of retrieved chunks -- answer directly
        # from the full stored document text instead of asking the LLM to
        # count within a partial context it was shown.
        count_target = extract_count_target(clean_query)
        if count_target and raw_docs:
            total = count_occurrences(count_target, raw_docs)
            return {
                "answer": (
                    f'"{count_target}" occurs {total} time{"s" if total != 1 else ""} '
                    f"across the indexed document(s) (exact count, not an LLM estimate)."
                ),
                "citations": [],
                "docs_indexed": len(set(c.metadata["doc_id"] for c in chunks)),
            }

        # A follow-up like "what's the second step?" names nothing to search
        # for, so retrieval also uses the user's previous message.
        previous = [m["content"] for m in (history or []) if m.get("role") == "user"]
        search_query = f"{previous[-1][:300]} {clean_query}" if previous else clean_query
        retrieved = hybrid_retrieve(
            search_query, chunks, bm25_index, self.embeddings, self.model,
            self.collection_name, top_k=top_k,
        )
        history_text = format_history(history)
        answer_text = generate_answer_from_context(clean_query, retrieved, history_text)

        # "I do not have enough information" used nothing from the docs, so
        # listing sources or flagging it as unsupported would be misleading.
        no_answer = "not have enough information" in answer_text.lower()

        # Anti-hallucination: only meaningful when there was actual context to
        # be grounded on -- a no-context chitchat reply isn't "unfaithful",
        # it was never supposed to cite anything.
        if retrieved and not no_answer:
            # The chat so far counts as a source too: an answer that repeats
            # what the user said earlier isn't a hallucination.
            source_text = build_context(retrieved, max_chunks=12) + "\n" + history_text
            check = guardrails.faithfulness_check(answer_text, source_text)
            if check["flag"] == "weak":
                answer_text += guardrails.HALLUCINATION_NOTICE

        # Several retrieved chunks often come from the same document; list
        # each document once.
        citations = []
        for d in [] if no_answer else retrieved:
            doc_id = d.metadata.get("doc_id")
            if doc_id not in {c["doc_id"] for c in citations}:
                citations.append({"doc_id": doc_id, "title": d.metadata.get("title")})
            if len(citations) == 3:
                break
        return {"answer": answer_text, "citations": citations, "docs_indexed": len(set(
            c.metadata["doc_id"] for c in chunks
        ))}


# One RagSession per thread_id, kept in memory. Only the most recently used
# MAX_SESSIONS stay loaded; an evicted chat is rebuilt from the database the
# next time it is used, so memory stays bounded however many chats exist.
MAX_SESSIONS = int(os.environ.get("MAX_RAG_SESSIONS", "50"))
_sessions: "OrderedDict[str, RagSession]" = OrderedDict()
_sessions_lock = threading.RLock()


PRUNE_EVERY_SECONDS = 60
_last_prune = 0.0


def _prune_deleted_sessions() -> None:
    """Forget indexes of chats that no longer exist in the database. When the
    admin deletes an account, only one chat pod is told directly; every other
    chat and ops pod catches up here within a minute."""
    global _last_prune
    if time.monotonic() - _last_prune < PRUNE_EVERY_SECONDS or not _sessions:
        return
    _last_prune = time.monotonic()
    try:
        existing = store.existing_thread_ids(list(_sessions))
    except Exception:
        return
    for thread_id in list(_sessions):
        if thread_id not in existing:
            _sessions.pop(thread_id, None)


def get_or_create_session(thread_id: str) -> RagSession:
    """The thread's index, built on first use. Only chats saved in the
    database get one, so made-up thread ids can't fill memory or Chroma."""
    with _sessions_lock:
        _prune_deleted_sessions()
        session = _sessions.get(thread_id)
        if session is None:
            if not store.existing_thread_ids([thread_id]):
                raise LookupError(f"No chat {thread_id}")
            # Built under the lock so two first requests for a chat don't
            # both build (and both write to Chroma).
            session = RagSession(thread_id)
            _sessions[thread_id] = session
            while len(_sessions) > MAX_SESSIONS:
                _sessions.popitem(last=False)
            return session
        _sessions.move_to_end(thread_id)
    session.sync_uploads()
    return session


def base_docs_count() -> int:
    """Documents a chat with no uploads searches: the base corpus."""
    return len(load_documents(DATA_DIR))


def drop_session(thread_id: str) -> None:
    """Forget a deleted thread's index on this pod and remove its shared
    Chroma collection. Other pods drop theirs on their next prune."""
    with _sessions_lock:
        session = _sessions.pop(thread_id, None)
    try:
        reset_collection(session.collection_name if session else f"session_{thread_id}")
    except Exception:
        pass
