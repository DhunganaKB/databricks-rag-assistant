import pytest

from rag_app.agent import RagAgent, SessionNotFound, build_citations, cited_numbers, parse_memories
from rag_app.retriever import Hit, diversify

from conftest import PDF_HIT, TXT_HIT, FakeLLM, FakeRetriever, make_settings


def _agent(store, llm=None, **settings):
    return RagAgent(make_settings(**settings), store, FakeRetriever(), llm or FakeLLM())


def _system_prompt(llm: FakeLLM) -> str:
    return llm.answer_calls()[-1][0]["content"]


# ------------------------------------------------------------------ answers and citations
def test_first_turn_has_no_history_and_cites_with_locations(agent):
    r = agent.chat("alice", "What was revenue?")
    assert r.turn_index == 1 and r.history_turns_used == 0 and not r.summary_used
    assert r.standalone_query == "What was revenue?"  # no history -> no rewrite call
    pdf, txt = r.citations
    assert pdf.n == 1 and pdf.doc_format == "paged" and pdf.pages == [1]
    assert pdf.boxes[0].x0 == pytest.approx(0.13)  # bounding box travels with the citation
    assert txt.n == 2 and txt.doc_format == "text" and txt.span.line_start == 11


def test_no_answer_has_no_citations(store):
    agent = RagAgent(make_settings(), store, FakeRetriever([]), FakeLLM("I could not find that in the knowledge base."))
    assert agent.chat("alice", "dogs in the office?").citations == []


def test_cited_number_formats():
    assert cited_numbers("a [1] b [2][3] c [1, 4] d [5;6] e [12]") == [1, 2, 3, 4, 5, 6, 12]


def test_build_citations_ignores_out_of_range():
    assert [c.n for c in build_citations("x [2] y [9] z [1]", [PDF_HIT, TXT_HIT])] == [2, 1]


def test_diversify_caps_per_document():
    hits = [Hit(f"c{i}", "d1", "T", "", "", "text", "t") for i in range(5)] + [
        Hit("c9", "d2", "T", "", "", "text", "t")
    ]
    assert [h.doc_id for h in diversify(hits, 4, 3)] == ["d1", "d1", "d1", "d2"]


# ------------------------------------------------------------------ sessions
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


def test_empty_message_rejected(agent):
    with pytest.raises(ValueError):
        agent.chat("alice", "   ")


def test_title_set_by_maintenance(agent):
    r = agent.chat("alice", "What was revenue?")
    assert agent.maintain("alice", r.session_id)["title"] == "Revenue And Vacation"
    assert agent.store.get_session(r.session_id, "alice")["title"] == "Revenue And Vacation"


# ------------------------------------------------------------------ short-term memory
def test_history_includes_exactly_last_five_turns(store):
    llm = FakeLLM()
    agent = _agent(store, llm, history_turns=5, long_term_memory=False)
    sid = None
    for i in range(1, 8):
        r = agent.chat("alice", f"question {i}", session_id=sid)
        sid = r.session_id
    assert r.turn_index == 7 and r.history_turns_used == 5
    final_prompt = llm.answer_calls()[-1]
    assert [m["role"] for m in final_prompt] == ["system"] + ["user", "assistant"] * 5 + ["user"]
    assert [m["content"] for m in final_prompt[1:-1] if m["role"] == "user"] == [f"question {i}" for i in range(2, 7)]


def test_follow_up_is_rewritten_with_history(agent):
    sid = agent.chat("alice", "What was revenue in Q3?").session_id
    r = agent.chat("alice", "and in Q2?", session_id=sid)
    assert r.standalone_query.startswith("standalone:")
    assert agent.retriever.queries[-1] == r.standalone_query


def test_turns_leaving_the_window_are_summarized(store):
    llm = FakeLLM()
    agent = _agent(store, llm, history_turns=2, long_term_memory=False)
    sid = None
    for i in range(1, 5):  # 4 turns, window of 2 -> turns 1-2 must be summarized
        sid = agent.chat("alice", f"question {i}", session_id=sid).session_id
        agent.maintain("alice", sid)
    session = store.get_session(sid, "alice")
    assert session["summary_upto_turn"] == 2 and session["running_summary"].startswith("SUMMARY")
    r = agent.chat("alice", "question 5", session_id=sid)
    assert r.summary_used and r.history_turns_used == 2
    system = _system_prompt(llm)
    assert "Summary of the earlier part of this conversation" in system and "SUMMARY" in system
    history_qs = [m["content"] for m in llm.answer_calls()[-1][1:-1] if m["role"] == "user"]
    assert history_qs == ["question 3", "question 4"]  # verbatim window; 1-2 only via the summary


def test_no_summary_while_within_window(store):
    agent = _agent(store, history_turns=5)
    sid = agent.chat("alice", "q1").session_id
    assert agent.maintain("alice", sid)["summarized_upto"] is None


# ------------------------------------------------------------------ long-term memory
def test_long_term_memory_extracted_filtered_and_used_across_sessions(store):
    llm = FakeLLM()
    agent = _agent(store, llm, memory_every_n_turns=3)
    sid = None
    for q in ("I'm a contractor in the Rotterdam office.", "Do I get vacation?", "Please use bullet points."):
        sid = agent.chat("alice", q, session_id=sid).session_id
        agent.maintain("alice", sid)
    facts = sorted(m["content"] for m in store.list_memories("alice"))
    assert facts == ["User is a contractor in the Rotterdam office.", "User prefers bullet points."]  # password dropped
    # a NEW session still knows the user
    r = agent.chat("alice", "How many vacation days do I get?")
    assert r.memories_used == 2
    assert "Known facts about the user (from earlier conversations" in _system_prompt(llm)
    assert "- User is a contractor in the Rotterdam office." in _system_prompt(llm)
    assert all(m["last_used_at"] for m in store.list_memories("alice"))
    # other users never see them
    assert store.list_memories("bob") == []
    assert agent.chat("bob", "hi").memories_used == 0


def test_memories_are_deduplicated(store):
    agent = _agent(store, memory_every_n_turns=1)
    sid = agent.chat("alice", "I'm a contractor in Rotterdam").session_id
    assert agent.maintain("alice", sid)["memories_added"] == 2
    sid2 = agent.chat("alice", "I'm a contractor in Rotterdam").session_id
    assert agent.maintain("alice", sid2)["memories_added"] == 0
    assert len(store.list_memories("alice")) == 2


def test_forget_memories(store):
    store.add_memories(
        "alice",
        [{"type": "profile", "content": "User is in HR."}, {"type": "profile", "content": "User is in Berlin."}],
        None,
    )
    one = store.list_memories("alice")[0]["memory_id"]
    assert store.delete_memory("bob", one) == 0  # cannot delete someone else's
    assert store.delete_memory("alice", one) == 1
    assert store.delete_memory("alice", None) == 1
    assert store.list_memories("alice") == []


def test_long_term_memory_can_be_disabled(store):
    llm = FakeLLM()
    agent = _agent(store, llm, long_term_memory=False, memory_every_n_turns=1)
    store.add_memories("alice", [{"type": "profile", "content": "User is in HR."}], None)
    sid = agent.chat("alice", "hi").session_id
    assert "memories_added" not in agent.maintain("alice", sid)
    assert "(from earlier conversations" not in _system_prompt(llm) and "User is in HR" not in _system_prompt(llm)


def test_parse_memories_is_robust():
    assert parse_memories("no json") == [] and parse_memories("[not json]") == []
    assert parse_memories('Sure: [{"type": "preference", "content": "User prefers short answers."}]') == [
        {"type": "preference", "content": "User prefers short answers."}
    ]
    sensitive = (
        '[{"content": "Call me at 555-123-4567"}, {"content": "My email is a@b.com"},'
        ' {"content": "User has a medical condition."}]'
    )
    assert parse_memories(sensitive) == []


def test_failed_summary_does_not_block_memory_extraction(store):
    from rag_app.llm import TruncatedOutput

    class TruncatingSummaryLLM(FakeLLM):
        def complete(self, messages, **kwargs):
            if "Updated summary:" in messages[-1]["content"]:
                raise TruncatedOutput("stopped at max_tokens")
            return super().complete(messages, **kwargs)

    agent = _agent(store, TruncatingSummaryLLM(), history_turns=1, memory_every_n_turns=2)
    sid = agent.chat("alice", "I'm a contractor in Rotterdam").session_id
    sid = agent.chat("alice", "Do I get vacation?", session_id=sid).session_id
    done = agent.maintain("alice", sid)
    assert done["summarized_upto"] == "error: TruncatedOutput"
    assert store.get_session(sid, "alice")["running_summary"] == ""  # nothing truncated was stored
    assert done["memories_added"] == 2  # the next step still ran
