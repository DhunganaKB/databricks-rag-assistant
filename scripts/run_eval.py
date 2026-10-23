"""Offline evaluation of the RAG agent against the real index and models.

Runs every item of eval/eval_set.json through the agent (in-memory SQLite, so it never
touches real conversations) and scores:

  retrieval_hit     the expected document is among the retrieved chunks
  correct           LLM judge: the answer contains the expected facts (or correctly declines)
  cited             answerable questions carry at least one citation
  located           every citation carries a location (bounding box or text span)
  declined          no-answer questions are declined without citations

    uv run --env-file .env python scripts/run_eval.py
    uv run --env-file .env python scripts/run_eval.py --limit 5 --min-correct 0.8

Writes output/eval_report.json; exits non-zero if --min-correct is not met (CI gate).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rag_app.agent import RagAgent  # noqa: E402
from rag_app.config import Settings  # noqa: E402
from rag_app.llm import DatabricksLLM  # noqa: E402
from rag_app.retriever import VectorSearchRetriever  # noqa: E402
from rag_app.store.sqlite import SQLiteStore  # noqa: E402

JUDGE = """You are grading an answer from a company knowledge assistant.

Question: {question}
Expected facts (all must be present, wording may differ): {facts}
Answer: {answer}

Reply with JSON only: {{"correct": true|false, "reason": "<one sentence>"}}"""


class RecordingAgent(RagAgent):
    """Remembers the chunks actually passed to the model (after diversification)."""

    last_hits: list = []

    def retrieve(self, query):
        self.last_hits = super().retrieve(query)
        return self.last_hits


def judge(llm, endpoint: str, question: str, facts: list[str], answer: str) -> tuple[bool, str]:
    raw = llm.complete(
        [{"role": "user", "content": JUDGE.format(question=question, facts="; ".join(facts), answer=answer)}],
        max_tokens=400,
        temperature=0.0,
        endpoint=endpoint,
    )
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        verdict = json.loads(m.group(0)) if m else {}
    except ValueError:
        verdict = {}
    if "correct" not in verdict:  # truncated or fenced JSON: fall back to the boolean literal
        flag = re.search(r'"correct"\s*:\s*(true|false)', raw, re.I)
        verdict = {
            "correct": bool(flag and flag.group(1).lower() == "true"),
            "reason": raw[:200] or "(empty judge reply)",
        }
    return bool(verdict.get("correct")), str(verdict.get("reason", ""))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(ROOT / "eval" / "eval_set.json"))
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--min-correct", type=float, default=0.0, help="fail (exit 1) below this correctness rate")
    args = p.parse_args()

    from databricks.sdk import WorkspaceClient

    settings = Settings.from_env()
    w = WorkspaceClient()
    store = SQLiteStore(":memory:")
    store.ensure_schema()
    llm = DatabricksLLM(w, settings.llm_endpoint)
    agent = RecordingAgent(settings, store, VectorSearchRetriever(w, settings.vs_index), llm)

    items = json.loads(Path(args.data).read_text())
    items = items[: args.limit] if args.limit else items
    rows = []
    for it in items:
        t = time.time()
        sid = None
        for prior in it.get("history", []):
            sid = agent.chat("eval", prior, session_id=sid).session_id
        r = agent.chat("eval", it["question"], session_id=sid)
        titles = {h.title for h in agent.last_hits}
        expected_doc = it.get("expected_doc")
        no_answer = expected_doc is None
        correct, reason = judge(llm, settings.helper_llm_endpoint, it["question"], it["expected_facts"], r.answer)
        row = {
            "id": it["id"],
            "category": it["category"],
            "question": it["question"],
            "answer": r.answer,
            "retrieval_hit": None if no_answer else expected_doc in titles,
            "correct": correct,
            "judge_reason": reason,
            "cited": None if no_answer else bool(r.citations),
            "located": all(c.boxes or c.span for c in r.citations) if r.citations else None,
            "declined": (not r.citations) if no_answer else None,
            "citations": [
                {"n": c.n, "title": c.title, "pages": c.pages, "boxes": len(c.boxes), "span": bool(c.span)}
                for c in r.citations
            ],
            "seconds": round(time.time() - t, 1),
        }
        rows.append(row)
        flag = "PASS" if correct else "FAIL"
        print(
            f"[{flag}] {it['id']:9s} {it['category']:11s} hit={row['retrieval_hit']} cited={row['cited']} located={row['located']} {row['seconds']}s"
        )
        if not correct:
            print(f"         judge: {reason}")

    def rate(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {k: rate(k) for k in ("correct", "retrieval_hit", "cited", "located", "declined")}
    summary.update(n=len(rows), llm=settings.llm_endpoint, history_turns=settings.history_turns)
    out = ROOT / "output"
    out.mkdir(exist_ok=True)
    (out / "eval_report.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print("\nsummary:", json.dumps(summary))
    if summary["correct"] is not None and summary["correct"] < args.min_correct:
        print(f"correctness {summary['correct']} is below the gate {args.min_correct}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
