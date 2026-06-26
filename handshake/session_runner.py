from __future__ import annotations

import time
from typing import Any

from core.event_model import Event
from core.journal_store import Store
from core.id_generator import ulid


TERMINAL_STATUSES = {"run_completed", "run_failed", "run_aborted"}


def start_run(chat_id: str, trigger: str, store: Store) -> str:
    root_id = ulid()
    event = Event(
        event_id=ulid(),
        event_type="agent_run",
        chat_id=chat_id,
        parent_id=None,
        root_id=root_id,
        origin="user",
        role=None,
        tool_name=None,
        status=None,
        provider=None,
        model=None,
        params=None,
        content=None,
        tokens_in=None,
        tokens_out=None,
        cost=None,
        latency_ms=None,
        scope="user",
        payload={"phase": "started", "trigger": trigger},
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store.append(event)
    return root_id


def resume_run(root_id: str, store: Store) -> str:
    has_start = False
    has_terminal = False
    chat_id = ""
    for event in store.query({"root_id": root_id, "event_type": "agent_run"}):
        payload = event.payload or {}
        phase = payload.get("phase", "")
        if phase == "started":
            has_start = True
            chat_id = event.chat_id
        if phase == "terminal":
            has_terminal = True

    if not has_start:
        raise ValueError(f"No agent_run start event found for root_id: {root_id}")
    if has_terminal:
        raise ValueError(f"Run {root_id} already has a terminal event")

    event = Event(
        event_id=ulid(),
        event_type="agent_run",
        chat_id=chat_id,
        parent_id=None,
        root_id=root_id,
        origin="user",
        role=None,
        tool_name=None,
        status=None,
        provider=None,
        model=None,
        params=None,
        content=None,
        tokens_in=None,
        tokens_out=None,
        cost=None,
        latency_ms=None,
        scope="user",
        payload={"phase": "resumed"},
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store.append(event)
    return root_id


def end_run(root_id: str, status: str, store: Store) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Invalid terminal status: {status!r}. Must be one of {TERMINAL_STATUSES}")

    chat_id = ""
    for event in store.query({"root_id": root_id, "event_type": "agent_run"}):
        payload = event.payload or {}
        if payload.get("phase") == "started":
            chat_id = event.chat_id
            break

    event = Event(
        event_id=ulid(),
        event_type="agent_run",
        chat_id=chat_id,
        parent_id=None,
        root_id=root_id,
        origin="user",
        role=None,
        tool_name=None,
        status=status,
        provider=None,
        model=None,
        params=None,
        content=None,
        tokens_in=None,
        tokens_out=None,
        cost=None,
        latency_ms=None,
        scope="user",
        payload={"phase": "terminal", "status": status},
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store.append(event)


def get_run_status(
    root_id: str,
    store: Store,
    crashed_threshold_seconds: int = 600,
) -> dict[str, Any]:
    events = list(store.query({"root_id": root_id}))
    if not events:
        raise KeyError(f"No events found for root_id: {root_id}")

    terminal_event = None
    start_event = None
    latest_event = None
    steps = 0
    tool_calls = 0
    tool_errors = 0
    total_tokens_in = 0
    total_tokens_out = 0
    total_cost = 0.0

    for event in events:
        if start_event is None or event.created_at < start_event.created_at:
            start_event = event
        if latest_event is None or event.created_at > latest_event.created_at:
            latest_event = event

        if event.event_type == "agent_run":
            payload = event.payload or {}
            if payload.get("phase") == "terminal":
                terminal_event = event

        if event.event_type in ("llm_call", "tool_call"):
            payload = event.payload or {}
            if payload.get("phase") == "completed" or payload.get("phase") == "failed":
                steps += 1
            if event.event_type == "tool_call" and payload.get("phase") in ("completed", "failed"):
                tool_calls += 1
                if event.status == "error":
                    tool_errors += 1

        if event.tokens_in is not None:
            total_tokens_in += event.tokens_in
        if event.tokens_out is not None:
            total_tokens_out += event.tokens_out
        if event.cost is not None:
            total_cost += event.cost

    if terminal_event is not None:
        status = terminal_event.status or "unknown"
        termination_reason = status
    else:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        assert latest_event is not None
        from datetime import datetime, timezone
        latest_dt = datetime.fromisoformat(latest_event.created_at.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        elapsed = (now_dt - latest_dt).total_seconds()
        if elapsed > crashed_threshold_seconds:
            status = "crashed"
            termination_reason = "no_terminal_event"
        else:
            status = "running"
            termination_reason = None

    wall_clock = 0.0
    if start_event and latest_event:
        from datetime import datetime
        start_dt = datetime.fromisoformat(start_event.created_at.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(latest_event.created_at.replace("Z", "+00:00"))
        wall_clock = (end_dt - start_dt).total_seconds()

    error_rate = (tool_errors / tool_calls) if tool_calls > 0 else 0.0

    return {
        "root_id": root_id,
        "status": status,
        "steps": steps,
        "tool_call_count": tool_calls,
        "error_rate": error_rate,
        "total_tokens": total_tokens_in + total_tokens_out,
        "total_cost": total_cost,
        "wall_clock_seconds": wall_clock,
        "termination_reason": termination_reason,
    }


def reconcile_crashed_runs(
    store: Store,
    crashed_threshold_seconds: int = 600,
) -> list[str]:
    open_root_ids: set[str] = set()
    terminal_root_ids: set[str] = set()

    for event in store.query({"event_type": "agent_run"}):
        payload = event.payload or {}
        phase = payload.get("phase", "")
        if phase == "started":
            open_root_ids.add(event.root_id)
        if phase == "terminal":
            terminal_root_ids.add(event.root_id)

    open_root_ids -= terminal_root_ids

    crashed: list[str] = []
    for root_id in open_root_ids:
        status = get_run_status(root_id, store, crashed_threshold_seconds)
        if status["status"] == "crashed":
            crashed.append(root_id)

    return crashed
