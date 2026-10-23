"""The RAG turn.

chat(user_id, message, session_id, client_message_id) ->
  1. load or create the session (ownership enforced by the store)
  2. idempotency: a retried client_message_id returns the stored answer
  3. history: the last HISTORY_TURNS (default 5) question/answer pairs
  4. rewrite a follow-up into a standalone search query (helper model, only with history)
  5. hybrid retrieval -> max N chunks per document -> numbered context
  6. answer with [n] citations (history included as prior messages)
  7. map [n] -> citations WITH their boxes / spans (already on the hits)
  8. persist the turn atomically
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from rag_app import prompts
from rag_app.config import Settings
from rag_app.llm import ChatModel
from rag_app.models import ChatResponse, Citation
from rag_app.retriever import Hit, Retriever, diversify
from rag_app.store.base import ConversationStore, uuid7

log = logging.getLogger(__name__)

# [1]  [1][3]  [1, 3]  [2; 4]
CITATION_RE = re.compile(r"\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")


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


def _history_text(turns: list[dict]) -> str:
    return "\n".join(f"{t['role']}: {t['content'][:800]}" for t in turns) or "(none)"


@dataclass
class RagAgent:
    settings: Settings
    store: ConversationStore
    retriever: Retriever
    llm: ChatModel

    def condense(self, message: str, history: list[dict]) -> str:
        if not history:
            return message
        query = self.llm.complete(
            [{"role": "user", "content": prompts.CONDENSE.format(history=_history_text(history), message=message)}],
            max_tokens=120,
            temperature=0.0,
            endpoint=self.settings.helper_llm_endpoint,
        )
        query = query.strip().strip('"').splitlines()[0].strip() if query.strip() else ""
        return query or message

    def retrieve(self, query: str) -> list[Hit]:
        candidates = self.retriever.search(query, self.settings.num_results * 3)
        return diversify(candidates, self.settings.num_results, self.settings.max_chunks_per_doc)

    def generate(self, message: str, hits: list[Hit], history: list[dict]) -> str:
        messages = [
            {"role": "system", "content": prompts.ANSWER_SYSTEM.format(assistant_name=self.settings.assistant_name)}
        ]
        messages += [{"role": t["role"], "content": t["content"]} for t in history]
        messages.append(
            {"role": "user", "content": prompts.CONTEXT_TEMPLATE.format(context=format_context(hits), question=message)}
        )
        return self.llm.complete(messages, max_tokens=self.settings.max_answer_tokens, temperature=0.1)

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

        history = self.store.recent_turns(sid, user_id, self.settings.history_turns)
        query = self.condense(message, history)
        hits = self.retrieve(query)
        answer = self.generate(message, hits, history)
        citations = build_citations(answer, hits)
        request_id = uuid7()
        latency_ms = int((time.time() - started) * 1000)
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
        )
        log.info(
            "turn session=%s turn=%s history=%s hits=%s citations=%s latency_ms=%s",
            sid,
            turn,
            len(history) // 2,
            len(hits),
            len(citations),
            latency_ms,
        )
        return ChatResponse(
            session_id=sid,
            request_id=request_id,
            turn_index=turn,
            answer=answer,
            citations=citations,
            standalone_query=query,
            history_turns_used=len(history) // 2,
            latency_ms=latency_ms,
        )

    def set_title(self, user_id: str, session_id: str, first_message: str) -> None:
        title = self.llm.complete(
            [{"role": "user", "content": prompts.TITLE.format(message=first_message[:500])}],
            max_tokens=20,
            temperature=0.2,
            endpoint=self.settings.helper_llm_endpoint,
        )
        self.store.set_title(session_id, user_id, title.strip().strip('"')[:80] or "New chat")
