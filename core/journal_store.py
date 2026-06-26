from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import time
from typing import Any, Iterable

from core.event_model import Event
from core.id_generator import ulid


def _try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec  # noqa: F811
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except (ImportError, OSError):
        return False


class Store:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._has_vec = _try_load_sqlite_vec(self.conn)
        self._vec_dims: int | None = None
        self._configure()
        self._create_schema()
        if self._has_vec:
            self._init_vec_index()

    def _configure(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")

    def _create_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT NOT NULL PRIMARY KEY,
                event_type TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                parent_id TEXT NULL,
                root_id TEXT NOT NULL,
                origin TEXT NOT NULL,
                role TEXT NULL,
                tool_name TEXT NULL,
                status TEXT NULL,
                provider TEXT NULL,
                model TEXT NULL,
                params JSON NULL,
                content TEXT NULL,
                tokens_in INTEGER NULL,
                tokens_out INTEGER NULL,
                cost REAL NULL,
                latency_ms INTEGER NULL,
                scope TEXT NOT NULL,
                payload JSON NULL,
                created_at TEXT NOT NULL,
                provenance TEXT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS events_no_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events table is immutable: UPDATE not allowed');
            END
            """
        )
        self.conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS events_no_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events table is immutable: DELETE not allowed');
            END
            """
        )
        try:
            self.conn.execute("ALTER TABLE events ADD COLUMN provenance TEXT NULL")
        except sqlite3.OperationalError:
            pass
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vectors (
                event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                text     TEXT NOT NULL,
                embedding BLOB NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_root_id_event_type "
            "ON events (root_id, event_type)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_chat_id_event_type "
            "ON events (chat_id, event_type)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_chat_id_origin "
            "ON events (chat_id, origin)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_event_type "
            "ON events (event_type)"
        )
        self.conn.commit()

    def _append_no_commit(self, event: Event) -> None:
        self.conn.execute(
            """
            INSERT INTO events (
                event_id,
                event_type,
                chat_id,
                parent_id,
                root_id,
                origin,
                role,
                tool_name,
                status,
                provider,
                model,
                params,
                content,
                tokens_in,
                tokens_out,
                cost,
                latency_ms,
                scope,
                payload,
                created_at,
                provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            event.to_row(),
        )

    def append(self, event: Event) -> None:
        self._append_no_commit(event)
        self.conn.commit()

    def get(self, event_id: str) -> Event:
        cursor = self.conn.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(f"Event not found: {event_id}")
        return Event.from_row(tuple(row))

    def query(self, filter: dict[str, Any]) -> Iterable[Event]:
        conditions = []
        values: list[Any] = []
        for key, value in filter.items():
            conditions.append(f"{key} = ?")
            values.append(value)
        sql = "SELECT * FROM events"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        cursor = self.conn.execute(sql, tuple(values))
        for row in cursor:
            yield Event.from_row(tuple(row))

    def _init_vec_index(self) -> None:
        row = self.conn.execute(
            "SELECT embedding FROM vectors LIMIT 1"
        ).fetchone()
        if row is None:
            return
        dims = len(json.loads(row[0]))
        self._vec_dims = dims
        self._ensure_vec_table(dims)
        vec_count = self.conn.execute(
            "SELECT COUNT(*) FROM vec_events"
        ).fetchone()[0]
        if vec_count == 0:
            self._populate_vec_index()

    def _ensure_vec_table(self, dims: int) -> None:
        self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_events USING vec0("
            f"event_id TEXT PRIMARY KEY, "
            f"embedding float[{dims}] distance_metric=cosine"
            f")"
        )

    def _populate_vec_index(self) -> None:
        rows = self.conn.execute(
            "SELECT event_id, embedding FROM vectors"
        ).fetchall()
        for row in rows:
            event_id = row[0]
            floats = json.loads(row[1])
            blob = struct.pack(f"<{len(floats)}f", *floats)
            self.conn.execute(
                "INSERT OR REPLACE INTO vec_events (event_id, embedding) VALUES (?, ?)",
                (event_id, blob),
            )
        self.conn.commit()

    def rebuild_vec_index(self) -> None:
        if not self._has_vec:
            raise RuntimeError("sqlite-vec extension is not loaded")
        row = self.conn.execute(
            "SELECT embedding FROM vectors LIMIT 1"
        ).fetchone()
        if row is None:
            return
        dims = len(json.loads(row[0]))
        self._vec_dims = dims
        try:
            self.conn.execute("DROP TABLE IF EXISTS vec_events")
        except sqlite3.OperationalError:
            pass
        self._ensure_vec_table(dims)
        self._populate_vec_index()

    def rebuild_vectors_from_journal(self) -> None:
        """Rebuild vectors table from journaled embedding events."""
        emb_events = list(self.query({"event_type": "embedding"}))
        if not emb_events:
            return

        from core.vector_embedder import Embedder

        latest: dict[str, Event] = {}
        for event in emb_events:
            source_id = event.parent_id
            if source_id not in latest or event.created_at > latest[source_id].created_at:
                latest[source_id] = event

        by_provider: dict[str, list[tuple[str, Event]]] = {}
        for source_id, event in latest.items():
            by_provider.setdefault(event.provider, []).append((source_id, event))

        for provider, entries in by_provider.items():
            embedder = Embedder(provider=provider)
            for source_id, event in entries:
                text = event.content
                embedding = embedder.embed(text)
                blob = json.dumps(embedding).encode("utf-8")
                self.conn.execute(
                    "INSERT OR REPLACE INTO vectors (event_id, text, embedding) "
                    "VALUES (?, ?, ?)",
                    (source_id, text, blob),
                )

        self.conn.commit()

        if self._has_vec:
            self.rebuild_vec_index()

    def upsert_vector(
        self,
        event_id: str,
        text: str,
        embedding: list[float],
        embedding_provider: str,
        embedding_model: str,
    ) -> None:
        if text.startswith("[{") or text.startswith('{"role'):
            raise ValueError(f"Refusing to upsert raw JSON into vectors: {text[:80]}")

        source = self.get(event_id)

        emb_event = Event(
            event_id=ulid(),
            event_type="embedding",
            chat_id=source.chat_id,
            parent_id=event_id,
            root_id=source.root_id,
            origin="system:embedding",
            role=None,
            tool_name=None,
            status="ok",
            provider=embedding_provider,
            model=embedding_model,
            params=None,
            content=text,
            tokens_in=None,
            tokens_out=None,
            cost=None,
            latency_ms=None,
            scope=source.scope,
            payload={"dims": len(embedding)},
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._append_no_commit(emb_event)

        blob = json.dumps(embedding).encode("utf-8")
        self.conn.execute(
            "INSERT OR REPLACE INTO vectors (event_id, text, embedding) VALUES (?, ?, ?)",
            (event_id, text, blob),
        )
        if self._has_vec:
            dims = len(embedding)
            if self._vec_dims is None:
                self._vec_dims = dims
                self._ensure_vec_table(dims)
            vec_blob = struct.pack(f"<{dims}f", *embedding)
            self.conn.execute(
                "INSERT OR REPLACE INTO vec_events (event_id, embedding) VALUES (?, ?)",
                (event_id, vec_blob),
            )
        self.conn.commit()

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        chat_id: str | None = None,
        scope: str | None = None,
    ) -> list[tuple[str, float]]:
        if self._has_vec and self._vec_dims is not None:
            return self._search_vec(query_embedding, top_k, chat_id, scope)
        return self._search_python(query_embedding, top_k, chat_id, scope)

    def _search_vec(
        self,
        query_embedding: list[float],
        top_k: int,
        chat_id: str | None,
        scope: str | None,
    ) -> list[tuple[str, float]]:
        q_blob = struct.pack(f"<{len(query_embedding)}f", *query_embedding)
        params: list[Any] = [q_blob, top_k]
        if chat_id is None and scope is None:
            sql = (
                "SELECT v.event_id, v.distance "
                "FROM vec_events v "
                "WHERE v.embedding MATCH ? AND k = ?"
            )
        else:
            conditions: list[str] = []
            if chat_id is not None:
                conditions.append("e.chat_id = ?")
                params.append(chat_id)
            if scope is not None:
                conditions.append("e.scope = ?")
                params.append(scope)
            where = " AND ".join(conditions)
            sql = (
                "SELECT v.event_id, v.distance "
                "FROM vec_events v "
                "WHERE v.embedding MATCH ? AND k = ? "
                "AND v.event_id IN ("
                "SELECT ve.event_id FROM vectors ve "
                "JOIN events e ON ve.event_id = e.event_id "
                f"WHERE {where})"
            )
        rows = self.conn.execute(sql, params).fetchall()
        return [(row[0], 1.0 - row[1]) for row in rows]

    def _search_python(
        self,
        query_embedding: list[float],
        top_k: int,
        chat_id: str | None,
        scope: str | None,
    ) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            """
            SELECT v.event_id, v.embedding
            FROM vectors v
            JOIN events e ON v.event_id = e.event_id
            WHERE (e.chat_id = ? OR ? IS NULL)
              AND (e.scope = ? OR ? IS NULL)
            """,
            (chat_id, chat_id, scope, scope),
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for row in rows:
            event_id = row[0]
            stored = json.loads(row[1])
            score = _cosine_similarity(query_embedding, stored)
            scored.append((event_id, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
