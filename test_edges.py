"""
Edge case and boundary tests for llmj.
Exercises code paths not covered by check_gaps.py.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.journal_store import Store
from core.event_model import Event
from core.llm_client import LLMClient
from core.tool_runner import ToolRunner
from core.provenance import derive_provenance, VALID_PROVENANCE_TIERS
from core.id_generator import ulid


def make_temp_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


def cleanup(store, db_path):
    store.conn.close()
    try:
        os.unlink(db_path)
    except OSError:
        pass


results = {"passed": 0, "failed": 0, "errored": 0}


def run_test(name, fn):
    try:
        fn()
        print(f"  PASS: {name}")
        results["passed"] += 1
    except AssertionError as e:
        print(f"  FAIL: {name} — {e}")
        results["failed"] += 1
    except Exception as e:
        print(f"  ERROR: {name} — {type(e).__name__}: {e}")
        results["errored"] += 1


# ═══════════════════════════════════════════════════════════
# PROVENANCE EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_provenance_invalid_tier():
    try:
        derive_provenance("user", "user", "invented_tier")
        assert False, "Should have raised"
    except ValueError as e:
        assert "Invalid provenance tier" in str(e)


def test_provenance_user_confirmed_on_none_role():
    result = derive_provenance("user", None, "user_confirmed")
    assert result == "user_confirmed"


def test_provenance_user_confirmed_on_user_role():
    result = derive_provenance("user", "user", "user_confirmed")
    assert result == "user_confirmed"


def test_provenance_model_claim_assertable():
    result = derive_provenance("user", "user", "model_claim")
    assert result == "model_claim"


def test_provenance_user_statement_assertable():
    result = derive_provenance("user", None, "user_statement")
    assert result == "user_statement"


def test_provenance_system_summarizer_default():
    assert derive_provenance("system:summarizer", None) == "model_claim"


def test_provenance_system_embedding_default():
    assert derive_provenance("system:embedding", None) == "model_claim"


def test_provenance_user_confirmed_blocked_on_all_system_origins():
    for origin in ["system:summarizer", "system:enrichment", "system:embedding", "system:x"]:
        try:
            derive_provenance(origin, None, "user_confirmed")
            assert False, f"Should have raised for origin={origin}"
        except ValueError:
            pass


def test_provenance_none_asserted_falls_through():
    assert derive_provenance("user", None, None) == "user_statement"
    assert derive_provenance("user", "assistant", None) == "model_claim"
    assert derive_provenance("system:x", None, None) == "model_claim"


# ═══════════════════════════════════════════════════════════
# STORE EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_store_get_nonexistent():
    store, db_path = make_temp_store()
    try:
        store.get("nonexistent-id")
        assert False, "Should have raised KeyError"
    except KeyError:
        pass
    cleanup(store, db_path)


def test_store_query_empty_filter():
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i in range(3):
        store.append(Event(
            event_id=ulid(), event_type="test", chat_id="c",
            parent_id=None, root_id=ulid(), origin="user", role=None,
            tool_name=None, status=None, provider=None, model=None,
            params=None, content=f"event {i}", tokens_in=None,
            tokens_out=None, cost=None, latency_ms=None,
            scope="user", payload=None, created_at=ts,
        ))
    all_events = list(store.query({}))
    assert len(all_events) == 3, f"Expected 3, got {len(all_events)}"
    cleanup(store, db_path)


def test_store_query_no_matches():
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    store.append(Event(
        event_id=ulid(), event_type="test", chat_id="c",
        parent_id=None, root_id=ulid(), origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="x", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    ))
    assert list(store.query({"chat_id": "nonexistent"})) == []
    cleanup(store, db_path)


def test_store_duplicate_event_id_raises():
    import sqlite3
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    eid = ulid()
    event = Event(
        event_id=eid, event_type="test", chat_id="c",
        parent_id=None, root_id=ulid(), origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="x", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    )
    store.append(event)
    try:
        store.append(event)
        assert False, "Should have raised on duplicate"
    except sqlite3.IntegrityError:
        pass
    cleanup(store, db_path)


def test_store_immutability_update_blocked():
    import sqlite3
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    eid = ulid()
    store.append(Event(
        event_id=eid, event_type="test", chat_id="c",
        parent_id=None, root_id=ulid(), origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="original", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    ))
    try:
        store.conn.execute("UPDATE events SET content = 'modified' WHERE event_id = ?", (eid,))
        assert False, "UPDATE should be blocked"
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
        assert "immutable" in str(e).lower()
    cleanup(store, db_path)


def test_store_immutability_delete_blocked():
    import sqlite3
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    eid = ulid()
    store.append(Event(
        event_id=eid, event_type="test", chat_id="c",
        parent_id=None, root_id=ulid(), origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="x", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    ))
    try:
        store.conn.execute("DELETE FROM events WHERE event_id = ?", (eid,))
        assert False, "DELETE should be blocked"
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
        assert "immutable" in str(e).lower()
    cleanup(store, db_path)


def test_store_event_with_provenance_roundtrip():
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    eid = ulid()
    store.append(Event(
        event_id=eid, event_type="test", chat_id="c",
        parent_id=None, root_id=ulid(), origin="user", role="user",
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="confirmed fact", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
        provenance="user_confirmed",
    ))
    event = store.get(eid)
    assert event.provenance == "user_confirmed"
    assert event.content == "confirmed fact"
    cleanup(store, db_path)


def test_store_event_without_provenance_reads_none():
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    eid = ulid()
    store.append(Event(
        event_id=eid, event_type="test", chat_id="c",
        parent_id=None, root_id=ulid(), origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="x", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    ))
    assert store.get(eid).provenance is None
    cleanup(store, db_path)


def test_store_search_empty_vectors():
    store, db_path = make_temp_store()
    assert store.search([0.1, 0.2, 0.3], top_k=5) == []
    cleanup(store, db_path)


def test_store_upsert_vector_rejects_json():
    store, db_path = make_temp_store()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    eid = ulid()
    store.append(Event(
        event_id=eid, event_type="test", chat_id="c",
        parent_id=None, root_id=ulid(), origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="x", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    ))
    try:
        store.upsert_vector(eid, '[{"role": "user"}]', [0.1, 0.2], "test", "m")
        assert False, "Should have rejected JSON text"
    except ValueError as e:
        assert "raw JSON" in str(e)
    cleanup(store, db_path)


# ═══════════════════════════════════════════════════════════
# TOOL RUNNER EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_toolrunner_returns_none():
    store, db_path = make_temp_store()
    runner = ToolRunner(store)
    root = ulid()
    result = runner.run("noop", lambda: None, {}, root, "chat")
    assert result is None
    events = list(store.query({"root_id": root, "event_type": "tool_call"}))
    end_events = [e for e in events if e.status == "ok"]
    assert len(end_events) == 1
    assert end_events[0].content == "None"
    cleanup(store, db_path)


def test_toolrunner_exception_preserves_type():
    store, db_path = make_temp_store()
    runner = ToolRunner(store)
    try:
        runner.run("bad", lambda: (_ for _ in ()).throw(TypeError("type error")),
                   {}, ulid(), "chat")
        assert False, "Should have raised"
    except TypeError as e:
        assert "type error" in str(e)
    cleanup(store, db_path)


def test_toolrunner_error_event_content():
    store, db_path = make_temp_store()
    runner = ToolRunner(store)
    root = ulid()
    try:
        runner.run("fail", lambda: 1/0, {}, root, "chat")
    except ZeroDivisionError:
        pass
    events = list(store.query({"root_id": root, "event_type": "tool_call"}))
    error_events = [e for e in events if e.status == "error"]
    assert len(error_events) == 1
    assert "division by zero" in error_events[0].content
    cleanup(store, db_path)


# ═══════════════════════════════════════════════════════════
# LLM CLIENT EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_llmclient_unsupported_provider():
    store, db_path = make_temp_store()
    client = LLMClient(store, api_keys={"fake": "key"})
    try:
        client.call(chat_id="c", messages=[{"role": "user", "content": "hi"}],
                    provider="nonexistent_provider")
        assert False, "Should have raised"
    except ValueError as e:
        assert "Unsupported provider" in str(e)
    events = list(store.query({"chat_id": "c"}))
    assert len(events) == 0
    cleanup(store, db_path)


def test_llmclient_error_event_has_parent_id():
    store, db_path = make_temp_store()
    client = LLMClient(store, api_keys={"openai": "key"})
    client._call_provider = lambda p, pp: (_ for _ in ()).throw(RuntimeError("fail"))
    try:
        client.call(chat_id="c", messages=[{"role": "user", "content": "hi"}])
    except RuntimeError:
        pass
    events = list(store.query({"chat_id": "c", "event_type": "llm_call"}))
    start = [e for e in events if (e.payload or {}).get("phase") == "started"][0]
    error = [e for e in events if e.status == "error"][0]
    assert error.parent_id == start.event_id
    cleanup(store, db_path)


def test_llmclient_root_id_propagates():
    store, db_path = make_temp_store()
    client = LLMClient(store, api_keys={"openai": "key"})
    client._call_provider = lambda p, pp: {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    my_root = ulid()
    client.call(chat_id="c", messages=[{"role": "user", "content": "hi"}],
                root_id=my_root)
    for e in store.query({"chat_id": "c"}):
        assert e.root_id == my_root, f"Event {e.event_id} has wrong root_id"
    cleanup(store, db_path)


# ═══════════════════════════════════════════════════════════
# CONTEXT ASSEMBLY EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_rolling_summary_empty_chat():
    from handshake.context_assembler import current_rolling_summary
    store, db_path = make_temp_store()
    llm = LLMClient(store, api_keys={"groq": "key"})
    assert current_rolling_summary("nonexistent-chat", store, llm) == ""
    cleanup(store, db_path)


def test_weight_by_provenance_empty_hits():
    from handshake.context_assembler import weight_by_provenance
    store, db_path = make_temp_store()
    assert weight_by_provenance([], store) == []
    cleanup(store, db_path)


def test_recency_tiebreak_empty():
    from handshake.context_assembler import recency_tiebreak
    assert recency_tiebreak([]) == []


def test_recency_tiebreak_single_item():
    from handshake.context_assembler import recency_tiebreak
    item = [("id1", 0.8, "user_statement", "2026-01-01T00:00:00Z")]
    assert recency_tiebreak(item) == item


def test_recency_tiebreak_orders_within_threshold():
    from handshake.context_assembler import recency_tiebreak
    hits = [
        ("old", 0.80, "user_statement", "2026-01-01T00:00:00Z"),
        ("new", 0.79, "user_statement", "2026-06-01T00:00:00Z"),
    ]
    result = recency_tiebreak(hits)
    assert result[0][0] == "new"
    assert result[1][0] == "old"


def test_recency_tiebreak_respects_score_gap():
    from handshake.context_assembler import recency_tiebreak
    hits = [
        ("high", 0.90, "user_statement", "2026-01-01T00:00:00Z"),
        ("low", 0.80, "user_statement", "2026-06-01T00:00:00Z"),
    ]
    result = recency_tiebreak(hits)
    assert result[0][0] == "high"
    assert result[1][0] == "low"


# ═══════════════════════════════════════════════════════════
# RUN LIFECYCLE EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_run_end_invalid_status():
    from handshake.session_runner import start_run, end_run
    store, db_path = make_temp_store()
    root = start_run("chat", "test", store)
    try:
        end_run(root, "invalid_status", store)
        assert False, "Should have raised"
    except ValueError as e:
        assert "Invalid terminal status" in str(e)
    cleanup(store, db_path)


def test_run_resume_nonexistent():
    from handshake.session_runner import resume_run
    store, db_path = make_temp_store()
    try:
        resume_run("nonexistent-root", store)
        assert False, "Should have raised"
    except ValueError as e:
        assert "No agent_run start event" in str(e)
    cleanup(store, db_path)


def test_run_resume_after_terminal():
    from handshake.session_runner import start_run, end_run, resume_run
    store, db_path = make_temp_store()
    root = start_run("chat", "test", store)
    end_run(root, "run_completed", store)
    try:
        resume_run(root, store)
        assert False, "Should have raised"
    except ValueError as e:
        assert "already has a terminal event" in str(e)
    cleanup(store, db_path)


def test_get_run_status_nonexistent():
    from handshake.session_runner import get_run_status
    store, db_path = make_temp_store()
    try:
        get_run_status("nonexistent", store)
        assert False, "Should have raised"
    except KeyError:
        pass
    cleanup(store, db_path)


# ═══════════════════════════════════════════════════════════
# ULID EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_ulid_uniqueness():
    ids = {ulid() for _ in range(1000)}
    assert len(ids) == 1000


def test_ulid_format():
    from core.id_generator import CROCKFORD_BASE32
    id_val = ulid()
    assert len(id_val) == 26
    for c in id_val:
        assert c in CROCKFORD_BASE32


# ═══════════════════════════════════════════════════════════
# EVENT MODEL EDGE CASES
# ═══════════════════════════════════════════════════════════

def test_event_frozen():
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    event = Event(
        event_id="x", event_type="test", chat_id="c",
        parent_id=None, root_id="r", origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="x", tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    )
    try:
        event.content = "modified"
        assert False, "Should be frozen"
    except AttributeError:
        pass


def test_event_roundtrip_all_fields():
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    original = Event(
        event_id="eid", event_type="llm_call", chat_id="chat",
        parent_id="pid", root_id="rid", origin="user", role="assistant",
        tool_name="tool", status="ok", provider="openai", model="gpt-4",
        params={"key": "value"}, content="hello world",
        tokens_in=100, tokens_out=50, cost=0.005, latency_ms=1200,
        scope="user", payload={"phase": "completed"}, created_at=ts,
        provenance="user_statement",
    )
    restored = Event.from_row(original.to_row())
    assert restored == original


def test_event_roundtrip_all_none():
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    event = Event(
        event_id="x", event_type="test", chat_id="c",
        parent_id=None, root_id="r", origin="user", role=None,
        tool_name=None, status=None, provider=None, model=None,
        params=None, content=None, tokens_in=None,
        tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
    )
    row = event.to_row()
    assert row[11] is None  # params
    assert row[18] is None  # payload
    assert row[20] is None  # provenance
    assert Event.from_row(row) == event


if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Edge Case & Boundary Tests")
    print("="*60)

    sections = [
        ("Provenance", [
            test_provenance_invalid_tier,
            test_provenance_user_confirmed_on_none_role,
            test_provenance_user_confirmed_on_user_role,
            test_provenance_model_claim_assertable,
            test_provenance_user_statement_assertable,
            test_provenance_system_summarizer_default,
            test_provenance_system_embedding_default,
            test_provenance_user_confirmed_blocked_on_all_system_origins,
            test_provenance_none_asserted_falls_through,
        ]),
        ("Store", [
            test_store_get_nonexistent,
            test_store_query_empty_filter,
            test_store_query_no_matches,
            test_store_duplicate_event_id_raises,
            test_store_immutability_update_blocked,
            test_store_immutability_delete_blocked,
            test_store_event_with_provenance_roundtrip,
            test_store_event_without_provenance_reads_none,
            test_store_search_empty_vectors,
            test_store_upsert_vector_rejects_json,
        ]),
        ("ToolRunner", [
            test_toolrunner_returns_none,
            test_toolrunner_exception_preserves_type,
            test_toolrunner_error_event_content,
        ]),
        ("LLMClient", [
            test_llmclient_unsupported_provider,
            test_llmclient_error_event_has_parent_id,
            test_llmclient_root_id_propagates,
        ]),
        ("Context Assembly", [
            test_rolling_summary_empty_chat,
            test_weight_by_provenance_empty_hits,
            test_recency_tiebreak_empty,
            test_recency_tiebreak_single_item,
            test_recency_tiebreak_orders_within_threshold,
            test_recency_tiebreak_respects_score_gap,
        ]),
        ("Run Lifecycle", [
            test_run_end_invalid_status,
            test_run_resume_nonexistent,
            test_run_resume_after_terminal,
            test_get_run_status_nonexistent,
        ]),
        ("ULID", [
            test_ulid_uniqueness,
            test_ulid_format,
        ]),
        ("Event Model", [
            test_event_frozen,
            test_event_roundtrip_all_fields,
            test_event_roundtrip_all_none,
        ]),
    ]

    for section_name, tests in sections:
        print(f"\n  [{section_name}]")
        for test_fn in tests:
            run_test(test_fn.__name__, test_fn)

    total = results["passed"] + results["failed"] + results["errored"]
    print(f"\n{'='*60}")
    print(f"  {results['passed']}/{total} passed, "
          f"{results['failed']} failed, {results['errored']} errored")
    print(f"{'='*60}")
