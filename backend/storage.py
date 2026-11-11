"""SQLite-backed metadata store. Uploaded files live on disk under UPLOAD_DIR."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    column_count INTEGER NOT NULL,
    encoding TEXT NOT NULL,
    delimiter TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_insights (
    dataset_id TEXT PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def create_dataset(*, dataset_id, name, file_path, size_bytes, row_count,
                   column_count, encoding, delimiter) -> dict:
    with connect() as conn:
        conn.execute(
            "INSERT INTO datasets (id, name, file_path, size_bytes, row_count, "
            "column_count, encoding, delimiter, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (dataset_id, name, file_path, size_bytes, row_count, column_count,
             encoding, delimiter, _utc_now()),
        )
    return get_dataset(dataset_id)


def get_dataset(dataset_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    return dict(row) if row else None


def list_datasets() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_dataset(dataset_id: str) -> dict | None:
    meta = get_dataset(dataset_id)
    if meta is None:
        return None
    with connect() as conn:
        conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    Path(meta["file_path"]).unlink(missing_ok=True)
    return meta


def save_insight(dataset_id: str, payload: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ai_insights (dataset_id, payload, created_at) VALUES (?,?,?)",
            (dataset_id, json.dumps(payload), _utc_now()),
        )


def get_insight(dataset_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM ai_insights WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    return json.loads(row["payload"]) if row else None
