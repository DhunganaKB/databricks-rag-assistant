import pytest

from rag_app.agent import RagAgent, SessionNotFound, build_citations, cited_numbers
from rag_app.retriever import Hit, diversify
from rag_app.store.sqlite import SQLiteStore

from conftest import PDF_HIT, TXT_HIT, FakeLLM, FakeRetriever, make_settings


def test_first_turn_has_no_history_and_cites_with_locations(agent):
    r = agent.chat("alice", "What was revenue?")
    assert r.turn_index == 1 and r.history_turns_used == 0
    assert r.standalone_query == "What was revenue?"  # no history -> no rewrite call
    pdf, txt = r.citations
    assert pdf.n == 1 and pdf.doc_format == "paged" and pdf.pages == [1]
    assert pdf.boxes[0].x0 == pytest.approx(0.13)  # bounding box travels with the citation
    assert txt.n == 2 and txt.doc_format == "text" and txt.span.line_start == 11


def test_history_includes_exactly_last_five_turns(store):
    llm = FakeLLM()
    agent = RagAgent(make_settings(history_turns=5), store, FakeRetriever(), llm)
    sid = None
    for i in range(1, 8):
        r = agent.chat("alice", f"question {i}", session_id=sid)
        sid = r.session_id
    assert r.turn_index == 7 and r.history_turns_used == 5
    final_prompt = llm.answer_calls()[-1]
    roles = [m["role"] for m in final_prompt]
    assert roles == ["system"] + ["user", "assistant"] * 5 + ["user"]
    history_questions = [m["content"] for m in final_prompt[1:-1] if m["role"] == "user"]
    assert history_questions == [f"question {i}" for i in range(2, 7)]  # turns 2..6, not 1


def test_history_window_is_configurable(store):
    agent = RagAgent(make_settings(history_turns=2), store, FakeRetriever(), FakeLLM())
    sid = agent.chat("alice", "q1").session_id
    for q in ("q2", "q3", "q4"):
        r = agent.chat("alice", q, session_id=sid)
    assert r.history_turns_used == 2


def test_follow_up_is_rewritten_with_history(agent):
    sid = agent.chat("alice", "What was revenue in Q3?").session_id
    r = agent.chat("alice", "and in Q2?", session_id=sid)
    assert r.standalone_query.startswith("standalone:")
    assert agent.retriever.queries[-1] == r.standalone_query


def test_other_users_session_is_not_found(agent):
    sid = agent.chat("alice", "hi").session_id
    with pytest.raises(SessionNotFound):
        agent.chat("mallory", "show me", session_id=sid)


def test_idempotent_retry(agent):
    r1 = agent.chat("alice", "What was revenue?", client_message_id="c-1")
    r2 = agent.chat("alice", "What was revenue?", session_id=r1.session_id, client_message_id="c-1")
    assert r2.replayed and r2.answer == r1.answer and r2.turn_index == 1
    assert r2.citations[0].boxes == r1.citations[0].boxes  # stored citations round-trip with boxes
    assert agent.store.get_session(r1.session_id, "alice")["turn_count"] == 1


def test_no_answer_has_no_citations(store):
    agent = RagAgent(make_settings(), store, FakeRetriever([]), FakeLLM("I could not find that in the knowledge base."))
    r = agent.chat("alice", "dogs in the office?")
    assert r.citations == []


def test_empty_message_rejected(agent):
    with pytest.raises(ValueError):
        agent.chat("alice", "   ")


def test_cited_number_formats():
    assert cited_numbers("a [1] b [2][3] c [1, 4] d [5;6] e [12]") == [1, 2, 3, 4, 5, 6, 12]


def test_build_citations_ignores_out_of_range():
    cites = build_citations("x [2] y [9] z [1]", [PDF_HIT, TXT_HIT])
    assert [c.n for c in cites] == [2, 1]


def test_diversify_caps_per_document():
    hits = [Hit(f"c{i}", "d1", "T", "", "", "text", "t") for i in range(5)] + [
        Hit("c9", "d2", "T", "", "", "text", "t")
    ]
    assert [h.doc_id for h in diversify(hits, 4, 3)] == ["d1", "d1", "d1", "d2"]


def test_title_generation(agent):
    r = agent.chat("alice", "What was revenue?")
    agent.set_title("alice", r.session_id, "What was revenue?")
    assert agent.store.get_session(r.session_id, "alice")["title"] == "Revenue And Vacation"


def test_store_is_persistent_across_instances(tmp_path):
    path = str(tmp_path / "db.sqlite")
    s1 = SQLiteStore(path)
    s1.ensure_schema()
    sid = s1.create_session("alice")["session_id"]
    s2 = SQLiteStore(path)
    assert s2.get_session(sid, "alice") is not None
