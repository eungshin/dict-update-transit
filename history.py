"""
history.py — Persistent lookup history + favorites (SQLite).

Stores every successful lookup so the user can:
- Re-open words they've seen before (search palette, tray menu)
- Mark favorites
- Get results offline from the cache when network fails
- Seed did-you-mean suggestions from their own vocabulary

Location: %APPDATA%/dict-tool/history.db (same pattern as daemon.log).

Schema: see CREATE TABLE statements below. FTS5 is used when available;
a LIKE-based path is used as graceful fallback.

Public API
----------
    init_db(path=None) -> None
    set_enabled(flag: bool) -> None
    is_enabled() -> bool
    record_lookup(word, result, context, source) -> int | None
    get_cached(word) -> dict | None
    recent(limit=20) -> list[dict]
    favorites() -> list[dict]
    set_favorite(word, flag) -> None
    search(query, limit=50) -> list[dict]
    known_words() -> list[str]
    export_csv(path) -> int
    stats() -> dict
    clear() -> None
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_DB_PATH: Path | None = None
_FTS_AVAILABLE: bool = False
_ENABLED: bool = True
_LOCK = threading.Lock()
_INITIALIZED: bool = False


def _default_db_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "dict-tool" / "history.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    word        TEXT NOT NULL,
    word_norm   TEXT NOT NULL,
    phonetic    TEXT,
    result_json TEXT NOT NULL,
    context     TEXT,
    source      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    is_favorite INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_word_norm ON lookups(word_norm);
CREATE INDEX IF NOT EXISTS idx_created   ON lookups(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_favorite  ON lookups(is_favorite, created_at DESC);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS lookups_fts USING fts5(
    word,
    definitions,
    content='lookups',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS lookups_ai AFTER INSERT ON lookups BEGIN
    INSERT INTO lookups_fts(rowid, word, definitions)
    VALUES (new.id, new.word, new.result_json);
END;

CREATE TRIGGER IF NOT EXISTS lookups_ad AFTER DELETE ON lookups BEGIN
    INSERT INTO lookups_fts(lookups_fts, rowid, word, definitions)
    VALUES ('delete', old.id, old.word, old.result_json);
END;
"""


# ---------------------------------------------------------------------------
# Connection / init
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    assert _DB_PATH is not None, "init_db() not called"
    conn = sqlite3.connect(str(_DB_PATH), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _check_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS __fts_probe USING fts5(x)")
        conn.execute("DROP TABLE __fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def init_db(path: Path | None = None) -> None:
    global _DB_PATH, _FTS_AVAILABLE, _INITIALIZED
    with _LOCK:
        _DB_PATH = Path(path) if path is not None else _default_db_path()
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            _FTS_AVAILABLE = _check_fts5(conn)
            if _FTS_AVAILABLE:
                try:
                    conn.executescript(_FTS_SCHEMA)
                except sqlite3.OperationalError as exc:
                    logger.warning("FTS5 init failed (%s) — falling back to LIKE", exc)
                    _FTS_AVAILABLE = False
            _INITIALIZED = True
            logger.info("history.db ready at %s (FTS5=%s)", _DB_PATH, _FTS_AVAILABLE)
        finally:
            conn.close()


def _ensure_init() -> None:
    if not _INITIALIZED:
        init_db()


# ---------------------------------------------------------------------------
# Enable / disable (driven by config.history_enabled)
# ---------------------------------------------------------------------------


def set_enabled(flag: bool) -> None:
    global _ENABLED
    _ENABLED = bool(flag)


def is_enabled() -> bool:
    return _ENABLED


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _normalize(word: str) -> str:
    return word.strip().lower()


def record_lookup(
    word: str,
    result: dict,
    context: str | None = None,
    source: str = "api",
) -> int | None:
    """Persist a lookup result. Returns the row id or None if disabled."""
    if not _ENABLED:
        return None
    if not isinstance(word, str) or not word.strip():
        return None
    if not isinstance(result, dict):
        return None

    _ensure_init()

    word_norm = _normalize(word)
    phonetic = result.get("phonetic")
    result_json = json.dumps(result, ensure_ascii=False)
    created_at = int(time.time())

    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO lookups (word, word_norm, phonetic, result_json,
                                     context, source, created_at, is_favorite)
                VALUES (?, ?, ?, ?, ?, ?, ?,
                        COALESCE((SELECT is_favorite FROM lookups
                                  WHERE word_norm=? ORDER BY created_at DESC LIMIT 1), 0))
                """,
                (word, word_norm, phonetic, result_json,
                 context, source, created_at, word_norm),
            )
            return cur.lastrowid
        except sqlite3.Error as exc:
            logger.warning("record_lookup failed: %s", exc)
            return None
        finally:
            conn.close()


def set_favorite(word: str, flag: bool) -> None:
    if not isinstance(word, str) or not word.strip():
        return
    _ensure_init()
    word_norm = _normalize(word)
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE lookups SET is_favorite=? WHERE word_norm=?",
                (1 if flag else 0, word_norm),
            )
        finally:
            conn.close()


def clear() -> None:
    _ensure_init()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM lookups")
            if _FTS_AVAILABLE:
                try:
                    conn.execute("INSERT INTO lookups_fts(lookups_fts) VALUES('rebuild')")
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict:
    try:
        result = json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        result = {}
    return {
        "id": row["id"],
        "word": row["word"],
        "word_norm": row["word_norm"],
        "phonetic": row["phonetic"],
        "context": row["context"],
        "source": row["source"],
        "created_at": row["created_at"],
        "is_favorite": bool(row["is_favorite"]),
        "result": result,
    }


def get_cached(word: str) -> dict | None:
    """Return the most recent stored lookup_word() result for *word*, or None."""
    if not isinstance(word, str) or not word.strip():
        return None
    _ensure_init()
    word_norm = _normalize(word)
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT result_json FROM lookups WHERE word_norm=? "
                "ORDER BY created_at DESC LIMIT 1",
                (word_norm,),
            ).fetchone()
            if row is None:
                return None
            try:
                return json.loads(row["result_json"])
            except (json.JSONDecodeError, TypeError):
                return None
        finally:
            conn.close()


def _dedupe_latest(rows: Iterable[sqlite3.Row]) -> list[dict]:
    """Keep only the latest row per word_norm, preserving order."""
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        if row["word_norm"] in seen:
            continue
        seen.add(row["word_norm"])
        out.append(_row_to_dict(row))
    return out


def recent(limit: int = 20) -> list[dict]:
    _ensure_init()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM lookups ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)) * 4,),  # over-fetch for dedupe
            ).fetchall()
            return _dedupe_latest(rows)[: max(1, int(limit))]
        finally:
            conn.close()


def favorites() -> list[dict]:
    _ensure_init()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM lookups WHERE is_favorite=1 ORDER BY created_at DESC"
            ).fetchall()
            return _dedupe_latest(rows)
        finally:
            conn.close()


def search(query: str, limit: int = 50) -> list[dict]:
    """Search by word or definition text. Uses FTS5 when available."""
    if not isinstance(query, str) or not query.strip():
        return recent(limit=limit)
    _ensure_init()
    q = query.strip()
    lim = max(1, int(limit))
    with _LOCK:
        conn = _connect()
        try:
            if _FTS_AVAILABLE:
                # Prefix match on every token so typing "hel" finds "hello"
                tokens = [t.replace('"', "") for t in q.split() if t.strip()]
                if not tokens:
                    return recent(limit=limit)
                fts_query = " ".join(f'"{t}"*' for t in tokens)
                try:
                    rows = conn.execute(
                        """
                        SELECT lookups.* FROM lookups
                        JOIN lookups_fts ON lookups.id = lookups_fts.rowid
                        WHERE lookups_fts MATCH ?
                        ORDER BY lookups.created_at DESC
                        LIMIT ?
                        """,
                        (fts_query, lim * 4),
                    ).fetchall()
                    return _dedupe_latest(rows)[:lim]
                except sqlite3.OperationalError as exc:
                    logger.debug("FTS query failed (%s) — falling back to LIKE", exc)
            like = f"%{q.lower()}%"
            rows = conn.execute(
                """
                SELECT * FROM lookups
                WHERE word_norm LIKE ? OR LOWER(result_json) LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (like, like, lim * 4),
            ).fetchall()
            return _dedupe_latest(rows)[:lim]
        finally:
            conn.close()


def known_words() -> list[str]:
    """Distinct normalized words ever recorded (used for did-you-mean)."""
    _ensure_init()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT word_norm FROM lookups"
            ).fetchall()
            return [r["word_norm"] for r in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Export / stats
# ---------------------------------------------------------------------------


def export_csv(path: Path | str) -> int:
    """Write all rows to CSV. Returns number of rows written."""
    _ensure_init()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT word, phonetic, context, source, created_at, is_favorite, result_json "
                "FROM lookups ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()

    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["word", "phonetic", "context", "source",
             "created_at_iso", "is_favorite", "result_json"]
        )
        for r in rows:
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r["created_at"]))
            writer.writerow([
                r["word"], r["phonetic"] or "", r["context"] or "",
                r["source"], iso, r["is_favorite"], r["result_json"],
            ])
    return len(rows)


def stats() -> dict[str, Any]:
    _ensure_init()
    with _LOCK:
        conn = _connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM lookups").fetchone()["n"]
            favs = conn.execute(
                "SELECT COUNT(DISTINCT word_norm) AS n FROM lookups WHERE is_favorite=1"
            ).fetchone()["n"]
            distinct = conn.execute(
                "SELECT COUNT(DISTINCT word_norm) AS n FROM lookups"
            ).fetchone()["n"]
            first_row = conn.execute(
                "SELECT MIN(created_at) AS t FROM lookups"
            ).fetchone()
            first_ts = first_row["t"] if first_row else None
        finally:
            conn.close()

    db_bytes = _DB_PATH.stat().st_size if _DB_PATH and _DB_PATH.exists() else 0
    days_active = 0
    if first_ts:
        days_active = max(1, (int(time.time()) - int(first_ts)) // 86400)
    return {
        "total": total,
        "distinct_words": distinct,
        "favorites": favs,
        "days_active": days_active,
        "db_bytes": db_bytes,
    }
