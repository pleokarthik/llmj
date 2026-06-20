"""
Failing behavioral checks for gaps 1, 2, and 3.
Run: python check_gaps.py
Expected: all three FAIL on current code, demonstrating each defect.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.store import Store
from core.models import Event
from core.llm_client import LLMClient
from core.ulid import ulid


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


def check_1_dangling_start_event():
    """LLMClient.call() should journal status='error' end event on provider failure."""
    store, db_path = make_temp_store()
    client = LLMClient(store, api_keys={"openai": "test-key"})

    def failing_provider(provider, provider_payload):
        raise RuntimeError("Simulated provider failure")

    client._call_provider = failing_provider

    chat_id = "check1-chat"
    caught = False
    try:
        client.call(
            chat_id=chat_id,
            messages=[{"role": "user", "content": "hello"}],
            provider="openai",
        )
    except RuntimeError:
        caught = True

    assert caught, "Expected RuntimeError from provider, but call() succeeded"

    events = list(store.query({"chat_id": chat_id, "event_type": "llm_call"}))
    start_events = [e for e in events if (e.payload or {}).get("phase") == "started"]
    error_events = [e for e in events if e.status == "error"]

    assert len(start_events) == 1, f"Expected 1 start event, got {len(start_events)}"

    assert len(error_events) == 1, (
        f"DEFECT: start event {start_events[0].event_id} has no matching "
        f"status='error' end event. Found {len(error_events)} error events, "
        f"expected 1. The start event is dangling."
    )

    cleanup(store, db_path)


def check_2_summary_self_contamination():
    """current_rolling_summary() should exclude origin='system:summarizer' from its input."""
    from handshake.context import current_rolling_summary

    store, db_path = make_temp_store()
    chat_id = "check2-chat"
    root = ulid()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for i in range(2):
        store.append(Event(
            event_id=ulid(), event_type="llm_call", chat_id=chat_id,
            parent_id=None, root_id=root, origin="user", role="assistant",
            tool_name=None, status="ok", provider="openai", model="gpt-3.5-turbo",
            params=None, content=f"Normal response {i+1}",
            tokens_in=10, tokens_out=20, cost=0.001, latency_ms=100,
            scope="user", payload={"phase": "completed"}, created_at=ts,
        ))

    store.append(Event(
        event_id=ulid(), event_type="llm_call", chat_id=chat_id,
        parent_id=None, root_id=root, origin="system:summarizer",
        role="assistant", tool_name=None, status="ok",
        provider="groq", model="llama-3.3-70b-versatile",
        params=None, content="PRIOR SUMMARY: recycled summarizer output",
        tokens_in=10, tokens_out=20, cost=None, latency_ms=100,
        scope="user", payload={"phase": "completed"}, created_at=ts,
    ))

    # Short events hit the early-return path (combined < SUMMARY_CHAR_BUDGET),
    # so the actual function returns the combined text without an LLM call.
    llm = LLMClient(store, api_keys={"groq": "test-key"})
    result = current_rolling_summary(chat_id, store, llm)

    assert "PRIOR SUMMARY" not in result, (
        f"DEFECT: system:summarizer content appears in summary input. "
        f"Result: {result!r}. Prior summaries will compound."
    )

    cleanup(store, db_path)


class FakeEmbeddingAdapter:
    MODEL_NAME = "fake-model"
    def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]
    def dims(self):
        return 4


def _register_fake_adapter():
    from core.embedder import EMBEDDING_ADAPTER_REGISTRY
    EMBEDDING_ADAPTER_REGISTRY["test"] = FakeEmbeddingAdapter


def check_3_vectors_rebuild_from_journal():
    """After vectors data loss, rebuild must restore vectors from journaled embedding
    events using the exact text originally embedded — not events.content."""
    _register_fake_adapter()
    from core.embedder import Embedder

    store, db_path = make_temp_store()
    chat_id = "check3-chat"
    root = ulid()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Create event with content X (the original assistant response)
    event_id = ulid()
    original_content = "The capital of France is Paris"
    store.append(Event(
        event_id=event_id, event_type="llm_call", chat_id=chat_id,
        parent_id=None, root_id=root, origin="user", role="assistant",
        tool_name=None, status="ok", provider="openai", model="gpt-3.5-turbo",
        params=None, content=original_content,
        tokens_in=10, tokens_out=20, cost=0.001, latency_ms=100,
        scope="user", payload={"phase": "completed"}, created_at=ts,
    ))

    # Embed DIFFERENT text Y (simulating OKF summary path where the embedded
    # text is a model-generated summary, not the original event content)
    summary_text = "Paris is the capital city of France"
    embedder = Embedder(provider="test")
    embedding = embedder.embed(summary_text)
    store.upsert_vector(event_id, summary_text, embedding,
                        embedding_provider=embedder.provider_name,
                        embedding_model=embedder.model_name)

    # Precondition: search works before data loss
    results_before = store.search(embedding, top_k=5, chat_id=chat_id)
    assert len(results_before) > 0, "Precondition: search should work before data loss"

    # The journaled event has the ORIGINAL content, not the summary
    journal_event = store.get(event_id)
    assert journal_event.content == original_content

    # FAITHFULNESS ASSERTION 1: the journal must contain an embedding event
    # that records the EXACT text that was embedded (the summary, not events.content)
    emb_events = [e for e in store.query({"event_type": "embedding"})
                  if e.parent_id == event_id]

    assert len(emb_events) == 1, (
        f"DEFECT: No embedding event in journal for event {event_id}. "
        f"The embedded text {summary_text!r} differs from events.content "
        f"{original_content!r} and is not recoverable from the journal. "
        f"vectors table cannot be faithfully rebuilt."
    )

    journaled_text = emb_events[0].content
    assert journaled_text == summary_text, (
        f"DEFECT: Embedding event text {journaled_text!r} doesn't match "
        f"the originally embedded text {summary_text!r}."
    )

    # Verify embedding event recorded provider/model as-used
    assert emb_events[0].provider == "test", (
        f"Embedding event provider is {emb_events[0].provider!r}, expected 'test'"
    )
    assert emb_events[0].model == "fake-model", (
        f"Embedding event model is {emb_events[0].model!r}, expected 'fake-model'"
    )

    # Simulate vectors data loss
    store.conn.execute("DELETE FROM vectors")
    store.conn.commit()

    # Rebuild from journal
    store.rebuild_vectors_from_journal()

    # FAITHFULNESS ASSERTION 2: rebuilt vectors.text must be the summary
    # (from the embedding event), NOT events.content
    row = store.conn.execute(
        "SELECT text FROM vectors WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row is not None, (
        f"DEFECT: rebuild_vectors_from_journal did not restore vectors row "
        f"for event {event_id}"
    )
    assert row[0] == summary_text, (
        f"DEFECT: Rebuilt vectors.text is {row[0]!r}, expected {summary_text!r} "
        f"(the journaled embedded text). A rebuild that uses events.content "
        f"({original_content!r}) instead of the embedding event text is unfaithful."
    )

    # Search should work again after rebuild
    results_after = store.search(embedding, top_k=5, chat_id=chat_id)
    assert len(results_after) > 0, (
        "DEFECT: Search returns no results after rebuild."
    )

    cleanup(store, db_path)


def check_3b_transaction_rollback():
    """Embedding event must not persist if the vectors write fails —
    single-transaction commit-or-fail-together."""
    store, db_path = make_temp_store()
    chat_id = "check3b-chat"
    root = ulid()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    event_id = ulid()
    store.append(Event(
        event_id=event_id, event_type="llm_call", chat_id=chat_id,
        parent_id=None, root_id=root, origin="user", role="assistant",
        tool_name=None, status="ok", provider="openai", model="gpt-3.5-turbo",
        params=None, content="test content",
        tokens_in=10, tokens_out=20, cost=0.001, latency_ms=100,
        scope="user", payload={"phase": "completed"}, created_at=ts,
    ))

    # Drop vectors table so INSERT INTO vectors fails inside upsert_vector
    store.conn.execute("DROP TABLE IF EXISTS vectors")
    store.conn.commit()

    raised = False
    try:
        store.upsert_vector(event_id, "text", [0.1, 0.2, 0.3, 0.4],
                            "test", "fake-model")
    except Exception:
        raised = True
        store.conn.rollback()

    assert raised, "upsert_vector should have raised when vectors table is missing"

    # The embedding event must NOT be in the journal
    emb_events = list(store.query({"event_type": "embedding"}))
    assert len(emb_events) == 0, (
        f"DEFECT: {len(emb_events)} embedding event(s) persisted despite vectors "
        f"write failure. Single-transaction commit-or-fail-together violated."
    )

    # Source event (committed earlier via append()) must still be there
    source = store.get(event_id)
    assert source is not None

    cleanup(store, db_path)


def check_4_vec_events_dimension_mismatch_rollback():
    """When vec_events INSERT rejects a dimension mismatch, the embedding event
    and vectors row from the same transaction must also roll back."""
    import sqlite3 as sqlite3_mod
    import struct as struct_mod

    store, db_path = make_temp_store()

    if not store._has_vec:
        print("SKIPPED: _has_vec False, vec0 transaction compliance "
              "NOT runtime-verified.")
        print("  Install sqlite-vec and re-run to exercise the real vec0 path.")
        cleanup(store, db_path)
        return

    chat_id = "check4-chat"
    root = ulid()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Event A: establish vec_events table with 4 dimensions
    event_a = ulid()
    store.append(Event(
        event_id=event_a, event_type="llm_call", chat_id=chat_id,
        parent_id=None, root_id=root, origin="user", role="assistant",
        tool_name=None, status="ok", provider="openai", model="gpt-3.5-turbo",
        params=None, content="Event A content",
        tokens_in=10, tokens_out=20, cost=0.001, latency_ms=100,
        scope="user", payload={"phase": "completed"}, created_at=ts,
    ))
    store.upsert_vector(event_a, "Event A text", [0.1, 0.2, 0.3, 0.4],
                        "test", "fake-model")

    # Event B: will attempt 8 dimensions into float[4] vec_events
    event_b = ulid()
    store.append(Event(
        event_id=event_b, event_type="llm_call", chat_id=chat_id,
        parent_id=None, root_id=root, origin="user", role="assistant",
        tool_name=None, status="ok", provider="openai", model="gpt-3.5-turbo",
        params=None, content="Event B content",
        tokens_in=10, tokens_out=20, cost=0.001, latency_ms=100,
        scope="user", payload={"phase": "completed"}, created_at=ts,
    ))

    mismatched = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    raised = False
    exc_location = "unknown"
    try:
        store.upsert_vector(event_b, "Event B text", mismatched,
                            "test", "fake-model")
    except struct_mod.error:
        raised = True
        exc_location = "struct.pack (Python, before vec_events SQL)"
        store.conn.rollback()
    except sqlite3_mod.OperationalError:
        raised = True
        exc_location = "sqlite-vec INSERT (SQL level, after vectors INSERT)"
        store.conn.rollback()
    except Exception as e:
        raised = True
        exc_location = f"{type(e).__name__}: {e}"
        store.conn.rollback()

    if not raised:
        print("OBSERVATION: sqlite-vec accepted 8-dim blob into float[4] "
              "vec_events without raising. The partial-write concern for "
              "this path does not arise — no failure to roll back from.")
        cleanup(store, db_path)
        return

    print(f"  Dimension mismatch raised at: {exc_location}")

    # 1. No vectors row for event B
    vectors_row = store.conn.execute(
        "SELECT * FROM vectors WHERE event_id = ?", (event_b,)
    ).fetchone()
    assert vectors_row is None, (
        f"DEFECT: vectors row persisted for event B despite vec_events failure. "
        f"Partial write: vectors committed but vec_events did not."
    )

    # 2. No embedding event for event B
    emb_events = [e for e in store.query({"event_type": "embedding"})
                  if e.parent_id == event_b]
    assert len(emb_events) == 0, (
        f"DEFECT: {len(emb_events)} embedding event(s) persisted for event B "
        f"despite vec_events failure. Transaction rollback incomplete."
    )

    # 3. Source event B (committed earlier via append()) still present
    source_b = store.get(event_b)
    assert source_b is not None, "Source event B should still exist"

    # Event A's data should also be intact (committed in separate transaction)
    event_a_vec = store.conn.execute(
        "SELECT * FROM vectors WHERE event_id = ?", (event_a,)
    ).fetchone()
    assert event_a_vec is not None, "Event A's vectors row should be intact"

    cleanup(store, db_path)


def check_5_user_confirmed_provenance():
    """user_confirmed provenance: assert -> store -> derive -> weight, end to end."""
    from core.provenance import derive_provenance
    from handshake.context import weight_by_provenance, PROVENANCE_WEIGHTS

    store, db_path = make_temp_store()
    chat_id = "check5-chat"
    root = ulid()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Journal a user_confirmed event
    confirmed_id = ulid()
    store.append(Event(
        event_id=confirmed_id, event_type="user_input", chat_id=chat_id,
        parent_id=None, root_id=root, origin="user", role="user",
        tool_name=None, status=None, provider=None, model=None,
        params=None, content="Paris is the capital of France",
        tokens_in=None, tokens_out=None, cost=None, latency_ms=None,
        scope="user", payload=None, created_at=ts,
        provenance="user_confirmed",
    ))

    # Stored provenance survives round-trip
    event = store.get(confirmed_id)
    assert event.provenance == "user_confirmed", (
        f"Stored provenance is {event.provenance!r}, expected 'user_confirmed'"
    )

    # derive_provenance honors asserted user_confirmed
    prov = derive_provenance(event.origin, event.role, event.provenance)
    assert prov == "user_confirmed", f"derive_provenance returned {prov!r}"

    # Retrieval weighting applies user_confirmed weight (1.0)
    hits = [(confirmed_id, 0.9)]
    weighted = weight_by_provenance(hits, store)
    _, weighted_score, wprov, _ = weighted[0]
    assert wprov == "user_confirmed", f"weight_by_provenance returned {wprov!r}"
    expected = 0.9 * PROVENANCE_WEIGHTS["user_confirmed"]
    assert abs(weighted_score - expected) < 1e-9, (
        f"Weight: {weighted_score}, expected {expected}"
    )

    # NEGATIVE: model output cannot be promoted to user_confirmed
    try:
        derive_provenance("user", "assistant", "user_confirmed")
        assert False, "Should have raised on model output promotion"
    except ValueError:
        pass

    # NEGATIVE: system origin cannot be promoted
    try:
        derive_provenance("system:enrichment", None, "user_confirmed")
        assert False, "Should have raised on system origin promotion"
    except ValueError:
        pass

    # Absent provenance falls back to default derivation
    assert derive_provenance("user", None) == "user_statement"
    assert derive_provenance("user", "assistant") == "model_claim"
    assert derive_provenance("system:enrichment", None) == "model_claim"

    cleanup(store, db_path)


if __name__ == "__main__":
    checks = [
        ("Check 1: LLMClient.call() dangling start on error", check_1_dangling_start_event),
        ("Check 2: Rolling summary self-contamination", check_2_summary_self_contamination),
        ("Check 3: vectors rebuild-from-journal faithfulness", check_3_vectors_rebuild_from_journal),
        ("Check 3b: transaction rollback on vectors write failure", check_3b_transaction_rollback),
        ("Check 4: vec_events dimension mismatch rollback", check_4_vec_events_dimension_mismatch_rollback),
        ("Check 5: user_confirmed provenance end-to-end", check_5_user_confirmed_provenance),
    ]
    passed = 0
    failed = 0
    errored = 0
    for label, fn in checks:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        try:
            fn()
            print("PASSED")
            passed += 1
        except AssertionError as e:
            print(f"FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            errored += 1

    print(f"\n{'='*60}")
    print(f"  Summary: {passed} passed, {failed} failed, {errored} errored")
    print(f"{'='*60}")
