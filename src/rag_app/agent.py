"""The RAG turn, with short-term and long-term memory stored in Lakebase.

chat(user_id, message, session_id, client_message_id) ->
  1. load or create the session (ownership enforced by the store)
  2. idempotency: a retried client_message_id returns the stored answer
  3. SHORT-TERM memory: the last HISTORY_TURNS (default 5) question/answer pairs verbatim,
     plus the session's running summary of everything older
  4. LONG-TERM memory: up to MAX_MEMORIES_IN_PROMPT facts about this user from earlier sessions
  5. rewrite a follow-up into a standalone search query (helper model, only with history)
  6. hybrid retrieval -> max N chunks per document -> numbered context
  7. answer with [n] citations
  8. map [n] -> citations WITH their boxes / spans (already on the hits)
  9. persist the turn atomically (with the MLflow trace id, so feedback can be linked)

Each step is a span of the turn's MLflow trace (see tracing.py).

maintain(user_id, session_id) runs after the response is sent (background):
  - session title after the first turn
  - fold turns that left the 5-turn window into the running summary
  - every MEMORY_EVERY_N_TURNS turns, extract durable facts about the user
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from rag_app import prompts, tracing
from rag_app.config import Settings
from rag_app.llm import ChatModel
from rag_app.models import ChatResponse, Citation
from rag_app.retriever import Hit, Retriever, diversify
from rag_app.store.base import ConversationStore, uuid7

log = logging.getLogger(__name__)

# [1]  [1][3]  [1, 3]  [2; 4]
CITATION_RE = re.compile(r"\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")
# Second line of defense for long-term memory (the extraction prompt is the first).
SENSITIVE_RE = re.compile(
    r"\b(password|passcode|pin code|ssn|social security|passport|credit card|iban|bank account|diagnos|"
    r"medical|religio|sexual|pregnan|disabilit|salary)\w*|\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b|[\w.+-]+@[\w-]+\.\w+",
    re.IGNORECASE,
)


class SessionNotFound(Exception):
    pass


def format_context(hits: list[Hit]) -> str:
    if not hits:
        return prompts.NO_CONTEXT
    return "\n\n".join(f"[{i}] {h.label()}\n{h.text}" for i, h in enumerate(hits, start=1))


def cited_numbers(answer: str) -> list[int]:
    out: list[int] = []
    for m in CITATION_RE.finditer(answer):
        for part in re.split(r"\s*[,;]\s*", m.group(1)):
            n = int(part)
            if n not in out:
                out.append(n)
    return out


def build_citations(answer: str, hits: list[Hit]) -> list[Citation]:
    citations = []
    for n in cited_numbers(answer):
        if not 1 <= n <= len(hits):
            continue
        h = hits[n - 1]
        citations.append(
            Citation(
                n=n,
                chunk_id=h.chunk_id,
                doc_id=h.doc_id,
                title=h.title,
                section=h.section_path,
                path=h.path,
                doc_format=h.doc_format,
                pages=h.pages,
                boxes=h.boxes,
                span=h.span,
                snippet=h.text[:300],
            )
        )
    return citations


def parse_memories(raw: str, limit: int = 5) -> list[dict]:
    """Model output -> [{"type", "content"}], dropping malformed or sensitive items."""
    match = re.search(r"\[.*\]", raw or "", re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content", "")).strip()
        kind = item.get("type") if item.get("type") in ("profile", "preference") else "profile"
        if 5 <= len(content) <= 200 and not SENSITIVE_RE.search(content):
            out.append({"type": kind, "content": content})
    return out[:limit]


def _history_text(turns: list[dict], limit_chars: int = 800) -> str:
    return "\n".join(f"{t['role']}: {t['content'][:limit_chars]}" for t in turns) or "(none)"


@dataclass
class RagAgent:
    settings: Settings
    store: ConversationStore
    retriever: Retriever
    llm: ChatModel

    # ------------------------------------------------------------------ memory reads
    @tracing.trace(span_type=tracing.SpanType.MEMORY)
    def short_term(self, session: dict, user_id: str) -> tuple[str, list[dict]]:
        """(running summary of older turns, last HISTORY_TURNS turns verbatim)."""
        turn_count = int(session.get("turn_count") or 0)
        window_start = max(turn_count - self.settings.history_turns, 0)
        recent = self.store.turns_between(str(session["session_id"]), user_id, window_start) if turn_count else []
        return (session.get("running_summary") or ""), recent

    @tracing.trace(span_type=tracing.SpanType.MEMORY)
    def long_term(self, user_id: str) -> list[dict]:
        if not self.settings.long_term_memory:
            return []
        return self.store.list_memories(user_id, self.settings.max_memories_in_prompt)

    # ------------------------------------------------------------------ steps
    @tracing.trace(span_type=tracing.SpanType.CHAIN)
    def condense(self, message: str, summary: str, history: list[dict]) -> str:
        if not history and not summary:
            return message
        query = self.llm.complete(
            [
                {
                    "role": "user",
                    "content": prompts.CONDENSE.format(
                        summary=summary or "(none)", history=_history_text(history), message=message
                    ),
                }
            ],
            max_tokens=400,  # headroom: some models spend tokens on reasoning first
            temperature=0.0,
            endpoint=self.settings.helper_llm_endpoint,
        )
        query = query.strip().strip('"').splitlines()[0].strip() if query.strip() else ""
        return query or message

    def retrieve(self, query: str) -> list[Hit]:
        with tracing.start_span(name="retrieve", span_type=tracing.SpanType.RETRIEVER) as span:
            span.set_inputs({"query": query})
            candidates = self.retriever.search(query, self.settings.num_results * 3)
            hits = diversify(candidates, self.settings.num_results, self.settings.max_chunks_per_doc)
            # MLflow shows RETRIEVER outputs in this shape as a list of documents
            span.set_outputs(
                [
                    {
                        "id": h.chunk_id,
                        "page_content": h.text,
                        "metadata": {"doc_uri": h.path, "title": h.label(), "score": h.score},
                    }
                    for h in hits
                ]
            )
            span.set_attribute("candidates", len(candidates))
            return hits

    @tracing.trace(span_type=tracing.SpanType.CHAIN)
    def generate(self, message: str, hits: list[Hit], summary: str, history: list[dict], memories: list[dict]) -> str:
        system = prompts.ANSWER_SYSTEM.format(assistant_name=self.settings.assistant_name)
        if memories:
            system += "\n\n" + prompts.USER_MEMORY_BLOCK.format(
                memories="\n".join(f"- {m['content']}" for m in memories)
            )
        if summary:
            system += "\n\n" + prompts.SUMMARY_BLOCK.format(summary=summary)
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t["role"], "content": t["content"]} for t in history]
        messages.append(
            {"role": "user", "content": prompts.CONTEXT_TEMPLATE.format(context=format_context(hits), question=message)}
        )
        return self.llm.complete(messages, max_tokens=self.settings.max_answer_tokens, temperature=0.1)

    # ------------------------------------------------------------------ turn
    @tracing.trace(span_type=tracing.SpanType.AGENT)
    def chat(
        self, user_id: str, message: str, session_id: str | None = None, client_message_id: str | None = None
    ) -> ChatResponse:
        started = time.time()
        message = (message or "").strip()
        if not message:
            raise ValueError("message is empty")

        if session_id:
            session = self.store.get_session(session_id, user_id)
            if session is None:
                raise SessionNotFound(session_id)
        else:
            session = self.store.create_session(user_id)
        sid = str(session["session_id"])
        request_id = uuid7()
        tracing.tag_trace(user=user_id, session_id=sid, request_id=request_id)

        if client_message_id:
            previous = self.store.find_turn_by_client_id(sid, user_id, client_message_id)
            if previous:
                return ChatResponse(
                    session_id=sid,
                    request_id=previous.get("request_id") or "",
                    turn_index=previous["turn_index"],
                    answer=previous["content"],
                    citations=[Citation(**c) for c in previous.get("citations") or []],
                    standalone_query=previous.get("standalone_query") or "",
                    history_turns_used=0,
                    replayed=True,
                )

        summary, history = self.short_term(session, user_id)
        memories = self.long_term(user_id)
        query = self.condense(message, summary, history)
        hits = self.retrieve(query)
        answer = self.generate(message, hits, summary, history, memories)
        citations = build_citations(answer, hits)
        latency_ms = int((time.time() - started) * 1000)
        with tracing.start_span(name="save_turn"):
            turn = self.store.append_turn(
                sid,
                user_id,
                message,
                answer,
                client_message_id=client_message_id,
                standalone_query=query,
                citations=[c.model_dump() for c in citations],
                request_id=request_id,
                latency_ms=latency_ms,
                trace_id=tracing.current_trace_id(),
            )
        if memories:
            self.store.touch_memories(user_id, [str(m["memory_id"]) for m in memories])
        log.info(
            "turn session=%s turn=%s history=%s summary=%s memories=%s hits=%s citations=%s latency_ms=%s",
            sid,
            turn,
            len(history) // 2,
            bool(summary),
            len(memories),
            len(hits),
            len(citations),
            latency_ms,
        )
        tracing.tag_trace(user=user_id, session_id=sid, request_id=request_id, turn=turn, citations=len(citations))
        return ChatResponse(
            session_id=sid,
            request_id=request_id,
            turn_index=turn,
            answer=answer,
            citations=citations,
            standalone_query=query,
            history_turns_used=len(history) // 2,
            summary_used=bool(summary),
            memories_used=len(memories),
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------ background maintenance
    @tracing.trace(span_type=tracing.SpanType.TASK)
    def maintain(self, user_id: str, session_id: str) -> dict:
        """Runs after the response; never affects the answer the user already received."""
        tracing.tag_trace(user=user_id, session_id=session_id, kind="maintenance")
        session = self.store.get_session(session_id, user_id)
        if not session:
            return {}
        turns = int(session["turn_count"])
        done: dict = {}

        def step(name: str, fn) -> None:  # each step fails independently of the others
            try:
                done[name] = fn()
            except Exception as exc:
                log.warning("maintenance step %s failed for session %s: %s", name, session_id, exc)
                done[name] = f"error: {type(exc).__name__}"

        if turns == 1 and not session.get("title"):
            first = self.store.turns_between(session_id, user_id, 0, 1)
            if first:
                step("title", lambda: self.set_title(user_id, session_id, first[0]["content"]))
        step("summarized_upto", lambda: self.roll_summary(user_id, session))
        if self.settings.long_term_memory and turns % self.settings.memory_every_n_turns == 0:
            step("memories_added", lambda: self.extract_memories(user_id, session_id, turns))
        return done

    def set_title(self, user_id: str, session_id: str, first_message: str) -> str:
        title = self.llm.complete(
            [{"role": "user", "content": prompts.TITLE.format(message=first_message[:500])}],
            max_tokens=200,
            temperature=0.2,
            endpoint=self.settings.helper_llm_endpoint,
        )
        title = title.strip().strip('"')[:80] or "New chat"
        self.store.set_title(session_id, user_id, title)
        return title

    def roll_summary(self, user_id: str, session: dict) -> int | None:
        """Fold turns that left the verbatim window into the running summary."""
        turns = int(session["turn_count"])
        upto = int(session.get("summary_upto_turn") or 0)
        new_upto = turns - self.settings.history_turns
        if new_upto - upto < self.settings.summarize_batch_turns:
            return None
        sid = str(session["session_id"])
        evicted = self.store.turns_between(sid, user_id, upto, new_upto)
        summary = self.llm.complete(
            [
                {
                    "role": "user",
                    "content": prompts.SUMMARIZE.format(
                        summary=session.get("running_summary") or "(empty)", messages=_history_text(evicted, 1500)
                    ),
                }
            ],
            max_tokens=1500,
            temperature=0.0,
            endpoint=self.settings.helper_llm_endpoint,
            require_complete=True,  # never store a cut-off summary
        )
        if not summary.strip():
            log.warning("empty summary for session %s; will retry after the next turn", sid)
            return None
        self.store.update_summary(sid, user_id, summary.strip(), new_upto)
        return new_upto

    def extract_memories(self, user_id: str, session_id: str, upto_turn: int) -> int:
        start = max(0, upto_turn - self.settings.memory_every_n_turns)
        user_msgs = [m for m in self.store.turns_between(session_id, user_id, start, upto_turn) if m["role"] == "user"]
        if not user_msgs:
            return 0
        known = self.store.list_memories(user_id, 50)
        raw = self.llm.complete(
            [
                {
                    "role": "user",
                    "content": prompts.EXTRACT_MEMORIES.format(
                        known="\n".join(f"- {m['content']}" for m in known) or "(none)",
                        messages=_history_text(user_msgs, 1000),
                    ),
                }
            ],
            max_tokens=1200,
            temperature=0.0,
            endpoint=self.settings.helper_llm_endpoint,
            require_complete=True,  # a cut-off JSON array is not trusted
        )
        return self.store.add_memories(user_id, parse_memories(raw), session_id)
