from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    chat_id: str
    parent_id: Optional[str]
    root_id: str
    origin: str
    role: Optional[str]
    tool_name: Optional[str]
    status: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    params: Optional[dict[str, Any]]
    content: Optional[str]
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    cost: Optional[float]
    latency_ms: Optional[int]
    scope: str
    payload: Optional[dict[str, Any]]
    created_at: str

    def to_row(self) -> tuple:
        return (
            self.event_id,
            self.event_type,
            self.chat_id,
            self.parent_id,
            self.root_id,
            self.origin,
            self.role,
            self.tool_name,
            self.status,
            self.provider,
            self.model,
            json.dumps(self.params) if self.params is not None else None,
            self.content,
            self.tokens_in,
            self.tokens_out,
            self.cost,
            self.latency_ms,
            self.scope,
            json.dumps(self.payload) if self.payload is not None else None,
            self.created_at,
        )

    @staticmethod
    def from_row(row: tuple) -> "Event":
        params = json.loads(row[11]) if row[11] is not None else None
        payload = json.loads(row[18]) if row[18] is not None else None
        return Event(
            event_id=row[0],
            event_type=row[1],
            chat_id=row[2],
            parent_id=row[3],
            root_id=row[4],
            origin=row[5],
            role=row[6],
            tool_name=row[7],
            status=row[8],
            provider=row[9],
            model=row[10],
            params=params,
            content=row[12],
            tokens_in=row[13],
            tokens_out=row[14],
            cost=row[15],
            latency_ms=row[16],
            scope=row[17],
            payload=payload,
            created_at=row[19],
        )
