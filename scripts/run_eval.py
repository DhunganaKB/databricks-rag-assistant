"""Evaluation of the RAG assistant with expected facts and an LLM judge.

Two modes:
  local (default)  runs the agent in-process against the real index and models, with an
                   in-memory session store (nothing written to Lakebase or disk)
  --base-url URL   calls a running app over HTTP (local or deployed), so the pipeline can
                   gate a release on the deployed system; sessions it creates are deleted

Scores per question:
  doc_hit     local: the expected document was among the chunks given to the model
              http:  the expected document was cited
  correct     LLM judge: the answer contains the expected facts (or correctly declines)
  cited       answerable questions carry at least one citation
  located     every citation carries a location (bounding box or text span)
  declined    no-answer questions are declined without citations

    uv run --env-file .env python scripts/run_eval.py
    uv run python scripts/run_eval.py --base-url https://<app-url> --databricks-auth --min-correct 0.9

Writes output/eval_report.json; exits 1 if correctness is below --min-correct (CI gate).
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

from rag_app.llm import DatabricksLLM  # noqa: E402

JUDGE = """You are grading an answer from a company knowledge assistant.

Question: {question}
Expected facts (all must be present, wording may differ): {facts}
Answer: {answer}

Reply with JSON only: {{"correct": true|false, "reason": "<one sentence>"}}"""


def judge(llm, endpoint: str, question: str, facts: list[str], answer: str) -> tuple[bool, str]:
    raw = llm.complete(
        [{"role": "user", "content": JUDGE.format(question=question, facts="; ".join(facts), answer=answer)}],
        max_tokens=1000,
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
        verdict = {"correct": bool(flag and flag.group(1).lower() == "true"), "reason": raw[:200] or "(empty)"}
    return bool(verdict.get("correct")), str(verdict.get("reason", ""))


class LocalRunner:
    """In-process agent; records the chunks actually passed to the model."""

    def __init__(self):
        from databricks.sdk import WorkspaceClient

        from rag_app.agent import RagAgent
        from rag_app.config import Settings
        from rag_app.retriever import VectorSearchRetriever
        from rag_app.store.sqlite import SQLiteStore

        class RecordingAgent(RagAgent):
            last_hits: list = []

            def retrieve(self, query):
                self.last_hits = super().retrieve(query)
                return self.last_hits

        settings = Settings.from_env()
        w = WorkspaceClient()
        store = SQLiteStore()  # in-memory: evaluation never writes to Lakebase or disk
        store.ensure_schema()
        self.agent = RecordingAgent(
            settings, store, VectorSearchRetriever(w, settings.vs_index), DatabricksLLM(w, settings.llm_endpoint)
        )
        self.label = settings.llm_endpoint

    def ask(self, question: str, history: list[str]) -> tuple[str, list[dict], set[str]]:
        sid = None
        for prior in history:
            sid = self.agent.chat("eval", prior, session_id=sid).session_id
        r = self.agent.chat("eval", question, session_id=sid)
        return r.answer, [c.model_dump() for c in r.citations], {h.title for h in self.agent.last_hits}

    def close(self) -> None:
        pass


class HttpRunner:
    """Calls a running app; deletes the sessions it creates."""

    def __init__(self, base_url: str, databricks_auth: bool, user: str):
        import httpx

        if databricks_auth:
            from databricks.sdk import WorkspaceClient

            headers = WorkspaceClient().config.authenticate()
        else:
            headers = {"x-forwarded-email": user}
        self.c = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=180)
        health = self.c.get("/api/health").json()
        self.label = f"{base_url} ({health.get('llm')})"
        self.sessions: set[str] = set()

    def ask(self, question: str, history: list[str]) -> tuple[str, list[dict], set[str]]:
        sid = None
        for prior in history:
            sid = self._chat(prior, sid)["session_id"]
        r = self._chat(question, sid)
        return r["answer"], r["citations"], {c["title"] for c in r["citations"]}

    def _chat(self, message: str, sid: str | None) -> dict:
        resp = self.c.post("/api/chat", json={"message": message, "session_id": sid})
        resp.raise_for_status()
        body = resp.json()
        self.sessions.add(body["session_id"])
        return body

    def close(self) -> None:
        for sid in self.sessions:
            self.c.delete(f"/api/sessions/{sid}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(ROOT / "eval" / "eval_set.json"))
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--min-correct", type=float, default=0.0, help="fail (exit 1) below this correctness rate")
    p.add_argument("--base-url", help="evaluate a running app over HTTP instead of in-process")
    p.add_argument("--databricks-auth", action="store_true", help="HTTP mode: authenticate with the Databricks SDK")
    p.add_argument("--user", default="eval.runner@example.com", help="HTTP mode against a local app: identity header")
    p.add_argument("--judge-endpoint", default="databricks-claude-haiku-5-5")
    args = p.parse_args()

    from databricks.sdk import WorkspaceClient

    judge_llm = DatabricksLLM(WorkspaceClient(), args.judge_endpoint)
    runner = HttpRunner(args.base_url, args.databricks_auth, args.user) if args.base_url else LocalRunner()

    items = json.loads(Path(args.data).read_text())
    items = items[: args.limit] if args.limit else items
    rows = []
    try:
        for it in items:
            t = time.time()
            answer, citations, titles = runner.ask(it["question"], it.get("history", []))
            expected_doc = it.get("expected_doc")
            no_answer = expected_doc is None
            correct, reason = judge(judge_llm, args.judge_endpoint, it["question"], it["expected_facts"], answer)
            row = {
                "id": it["id"],
                "category": it["category"],
                "question": it["question"],
                "answer": answer,
                "doc_hit": None if no_answer else expected_doc in titles,
                "correct": correct,
                "judge_reason": reason,
                "cited": None if no_answer else bool(citations),
                "located": all(c.get("boxes") or c.get("span") for c in citations) if citations else None,
                "declined": (not citations) if no_answer else None,
                "citations": [
                    {
                        "n": c["n"],
                        "title": c["title"],
                        "pages": c.get("pages"),
                        "boxes": len(c.get("boxes") or []),
                        "span": bool(c.get("span")),
                    }
                    for c in citations
                ],
                "seconds": round(time.time() - t, 1),
            }
            rows.append(row)
            flag = "PASS" if correct else "FAIL"
            print(
                f"[{flag}] {it['id']:9s} {it['category']:11s} hit={row['doc_hit']} cited={row['cited']} "
                f"located={row['located']} {row['seconds']}s"
            )
            if not correct:
                print(f"         judge: {reason}")
    finally:
        runner.close()

    def rate(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {k: rate(k) for k in ("correct", "doc_hit", "cited", "located", "declined")}
    summary.update(n=len(rows), system=runner.label, mode="http" if args.base_url else "local")
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
