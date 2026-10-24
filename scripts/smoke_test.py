"""End-to-end smoke test against a running app (local or deployed).

Checks: health, a multi-turn conversation (follow-up rewriting and the 5-turn history
window), citations with bounding boxes (PDF) and spans (TXT), citation viewer endpoints,
the annotated page PNG, idempotent retries, session privacy, and no-answer behavior.

    uv run --env-file .env python scripts/smoke_test.py --base-url http://localhost:8000
    # deployed app (needs an OAuth token):
    uv run python scripts/smoke_test.py --base-url https://<app-url> --token "$TOKEN"

Exit code 0 = all checks passed. Writes the annotated page to output/smoke_annotated.png.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "output"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--token", help="Bearer token for a deployed Databricks App")
    p.add_argument(
        "--user",
        default=f"smoke.{uuid.uuid4().hex[:8]}@example.com",
        help="local runs only: X-Forwarded-Email (a fresh user per run keeps memories independent)",
    )
    p.add_argument("--keep", action="store_true", help="keep the smoke-test sessions and memories in Lakebase")
    args = p.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {"x-forwarded-email": args.user}
    c = httpx.Client(base_url=args.base_url, headers=headers, timeout=180)

    health = c.get("/api/health").json()
    check(
        "health",
        health.get("status") == "ok",
        f"llm={health.get('llm')} history_turns={health.get('history_turns')} store={health.get('store')}"
        f" schema={health.get('memory_schema')}",
    )
    check("history is stored in Databricks (Lakebase)", health.get("store") == "postgres")

    # --- multi-turn conversation -------------------------------------------------
    turns = [
        "What was revenue in Q3 2026?",
        "And how did it compare with Q3 2025?",
        "How many vacation days does an employee with 3 years of service get?",
        "What about after 6 years?",
        "Can I carry any of them over?",
        "What is the hotel limit in Amsterdam?",
        "And in New York?",
    ]
    sid, responses = None, []
    for i, q in enumerate(turns, start=1):
        t = time.time()
        r = c.post("/api/chat", json={"message": q, "session_id": sid, "client_message_id": f"smoke-{uuid.uuid4()}"})
        r.raise_for_status()
        body = r.json()
        sid = body["session_id"]
        responses.append(body)
        cites = ", ".join(
            f"[{x['n']}] {x['title']} "
            + (
                f"p{x['pages']}"
                if x["doc_format"] == "paged"
                else f"L{x['span']['line_start']}-{x['span']['line_end']}"
            )
            for x in body["citations"]
        )
        print(
            f"\n  Q{i}: {q}\n      searched: {body['standalone_query']}\n      history used: {body['history_turns_used']}"
            f" | {time.time() - t:.1f}s\n      A: {body['answer'][:220]}\n      sources: {cites}"
        )
    check(
        "history grows then caps at 5",
        [b["history_turns_used"] for b in responses] == [0, 1, 2, 3, 4, 5, 5],
        str([b["history_turns_used"] for b in responses]),
    )
    check(
        "follow-up rewritten using history",
        "2025" in responses[1]["standalone_query"] or "revenue" in responses[1]["standalone_query"].lower(),
        responses[1]["standalone_query"],
    )
    check(
        "follow-up 'And in New York?' resolved",
        "new york" in responses[6]["standalone_query"].lower() and "hotel" in responses[6]["standalone_query"].lower(),
        responses[6]["standalone_query"],
    )
    check("PDF answer correct (48.6)", "48.6" in responses[0]["answer"])
    check("TXT answer correct (22 days)", "22" in responses[2]["answer"])

    # --- bounding boxes ----------------------------------------------------------
    pdf_cite = next((x for b in responses for x in b["citations"] if x["doc_format"] == "paged"), None)
    txt_cite = next((x for b in responses for x in b["citations"] if x["doc_format"] == "text"), None)
    if check(
        "PDF citation carries boxes", bool(pdf_cite and pdf_cite["boxes"]), str(pdf_cite and pdf_cite["boxes"][0])
    ):
        b = pdf_cite["boxes"][0]
        check("box coordinates normalized 0..1", 0 <= b["x0"] < b["x1"] <= 1 and 0 <= b["y0"] < b["y1"] <= 1)
        detail = c.get(f"/api/citations/{pdf_cite['chunk_id']}").json()
        page = detail["pages"][0]
        check(
            "citation detail has page image + boxes",
            bool(page["image_url"] and page["boxes"]),
            f"page {page['page']} {page['width_px']}x{page['height_px']}",
        )
        img = c.get(page["image_url"])
        check(
            "page image served",
            img.status_code == 200 and img.headers["content-type"] == "image/jpeg",
            f"{len(img.content)} bytes",
        )
        ann = c.get(f"/api/citations/{pdf_cite['chunk_id']}/pages/{page['page']}/annotated.png")
        if check("annotated PNG with boxes", ann.status_code == 200 and ann.headers["content-type"] == "image/png"):
            OUT.mkdir(exist_ok=True)
            (OUT / "smoke_annotated.png").write_bytes(ann.content)
    if check("TXT citation carries span", bool(txt_cite and txt_cite["span"]), str(txt_cite and txt_cite["span"])):
        doc = c.get(f"/api/documents/{txt_cite['doc_id']}/text").json()
        s = txt_cite["span"]
        cited = doc["source_text"][s["char_start"] : s["char_end"]]
        # Compare a body line of the chunk (inline "### heading" lines are added by the chunker,
        # so they are not verbatim in the source) with the highlighted range.
        body_line = next(
            (ln.strip() for ln in txt_cite["snippet"].split("\n") if ln.strip() and not ln.startswith("#")), ""
        )
        check(
            "span points at the cited text", bool(body_line) and body_line[:40] in cited, f"{body_line[:50]!r} in span"
        )

    # --- short-term memory: rolling summary of turns older than the 5-turn window ---
    time.sleep(6)  # summarization runs in the background after each answer
    r8 = c.post("/api/chat", json={"message": "Summarize what we discussed about revenue.", "session_id": sid}).json()
    check(
        "turn 8 uses summary + last 5 turns",
        r8["summary_used"] and r8["history_turns_used"] == 5,
        f"summary_used={r8['summary_used']} history={r8['history_turns_used']}",
    )

    # --- long-term memory: facts from one session are used in a NEW session ----------
    msid = None
    for q in (
        "I'm a contractor working in the Rotterdam office.",
        "Which travel booking tool should I use?",
        "Please keep answers short, in bullet points.",
    ):
        msid = c.post("/api/chat", json={"message": q, "session_id": msid}).json()["session_id"]
    time.sleep(8)  # extraction runs in the background after the 3rd turn
    mems = [m["content"] for m in c.get("/api/memories").json()]
    check(
        "long-term memory extracted",
        any("contractor" in m.lower() or "rotterdam" in m.lower() for m in mems),
        str(mems),
    )
    fresh = c.post("/api/chat", json={"message": "Am I eligible for paid time off?"}).json()
    check(
        "new session uses long-term memory",
        fresh["memories_used"] > 0 and "contractor" in fresh["answer"].lower(),
        f"memories_used={fresh['memories_used']} | {fresh['answer'][:120]}",
    )

    # --- idempotency, privacy, no-answer -------------------------------------------
    cid = f"smoke-{uuid.uuid4()}"
    a = c.post("/api/chat", json={"message": "What is the SEV2 response time?", "client_message_id": cid}).json()
    b = c.post(
        "/api/chat",
        json={"message": "What is the SEV2 response time?", "session_id": a["session_id"], "client_message_id": cid},
    ).json()
    check("retry is idempotent", b["replayed"] and b["answer"] == a["answer"] and b["turn_index"] == 1)
    if not args.token:
        other = httpx.get(
            f"{args.base_url}/api/sessions/{sid}/messages", headers={"x-forwarded-email": "someone.else@example.com"}
        )
        check("other users cannot read the session", other.status_code == 404)
    na = c.post("/api/chat", json={"message": "What is the policy on bringing dogs to the office?"}).json()
    check(
        "no-answer declines without citations",
        na["citations"] == [] and ("could not find" in na["answer"].lower() or "not" in na["answer"].lower()),
        na["answer"][:120],
    )

    if not args.keep:  # leave no test data behind in Lakebase
        created = {sid, msid, a["session_id"], na["session_id"], fresh["session_id"]}
        for session_id in created:
            c.delete(f"/api/sessions/{session_id}")
        if not args.token:  # only the throwaway local test user: never wipe a real user's memories
            c.delete("/api/memories")
        print(f"cleaned up {len(created)} smoke-test sessions" + ("" if args.token else " and memories"))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
