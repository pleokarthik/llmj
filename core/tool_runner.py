from __future__ import annotations

import time
from typing import Any, Callable

from core.models import Event
from core.store import Store
from core.ulid import ulid


class ToolRunner:
    def __init__(self, store: Store) -> None:
        self.store = store

    def run(
        self,
        tool_name: str,
        tool_fn: Callable[..., Any],
        args: dict[str, Any],
        root_id: str,
        chat_id: str,
    ) -> Any:
        start_event = Event(
            event_id=ulid(),
            event_type="tool_call",
            chat_id=chat_id,
            parent_id=None,
            root_id=root_id,
            origin="user",
            role=None,
            tool_name=tool_name,
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
            payload={"phase": "started", "args": args},
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.store.append(start_event)

        started = time.perf_counter()
        try:
            result = tool_fn(**args)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            end_event = Event(
                event_id=ulid(),
                event_type="tool_call",
                chat_id=chat_id,
                parent_id=start_event.event_id,
                root_id=root_id,
                origin="user",
                role=None,
                tool_name=tool_name,
                status="ok",
                provider=None,
                model=None,
                params=None,
                content=str(result),
                tokens_in=None,
                tokens_out=None,
                cost=None,
                latency_ms=elapsed_ms,
                scope="user",
                payload={"phase": "completed", "result": str(result)},
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self.store.append(end_event)
            return result
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            end_event = Event(
                event_id=ulid(),
                event_type="tool_call",
                chat_id=chat_id,
                parent_id=start_event.event_id,
                root_id=root_id,
                origin="user",
                role=None,
                tool_name=tool_name,
                status="error",
                provider=None,
                model=None,
                params=None,
                content=str(exc),
                tokens_in=None,
                tokens_out=None,
                cost=None,
                latency_ms=elapsed_ms,
                scope="user",
                payload={"phase": "failed", "error": str(exc)},
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self.store.append(end_event)
            raise
