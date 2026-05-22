# -*- coding: utf-8 -*-
"""
Conquest Trading — Persistent Storage Layer
============================================
Uses PostgreSQL (Railway) when DATABASE_URL is set.
Falls back to local JSON files when running on localhost.

All app code should use the four public functions:
    kv_get(key)          → list or dict
    kv_set(key, value)   → None
    kv_delete(key)       → None
    db_available()       → bool

Keys used:
    "paper_trades"   → list of trade dicts
    "watchlist"      → list of watchlist entry dicts
    "settings"       → dict of app settings
    "morning_brief"  → dict (daily cache)
"""

import json
import os
import logging

log = logging.getLogger(__name__)

# ── File fallback paths (used on localhost / when DB is unavailable) ──────────
_APP_DIR = os.path.dirname(os.path.abspath(__file__))

_FILE_MAP = {
    "paper_trades":  os.path.join(_APP_DIR, "paper_trades.json"),
    "watchlist":     os.path.join(_APP_DIR, "watchlist.json"),
    "settings":      os.path.join(_APP_DIR, "alerts_settings.json"),
    "morning_brief": os.path.join(_APP_DIR, "morning_brief.json"),
}

_DEFAULT_VALUES = {
    "paper_trades":  [],
    "watchlist":     [],
    "settings":      {
        "webhook_url": "",
        "watchlist":   "AAPL NVDA MSFT GOOGL AMZN META TSLA JPM XOM WMT COP SPY QQQ",
        "auto_briefing": True,
    },
    "morning_brief": {},
}

# ── DB connection (lazy-initialised once) ─────────────────────────────────────
_conn = None


def _get_conn():
    """Return a live psycopg2 connection, creating it if needed."""
    global _conn
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return None
    try:
        import psycopg2
        import psycopg2.extras
        # Railway sometimes supplies postgres:// but psycopg2 needs postgresql://
        url = url.replace("postgres://", "postgresql://", 1)
        if _conn is None or _conn.closed:
            _conn = psycopg2.connect(url)
            _conn.autocommit = True
            _ensure_table(_conn)
        # Quick liveness ping
        try:
            _conn.cursor().execute("SELECT 1")
        except Exception:
            _conn = psycopg2.connect(url)
            _conn.autocommit = True
        return _conn
    except Exception as e:
        log.warning(f"[DB] Could not connect to PostgreSQL: {e} — using file fallback")
        return None


def _ensure_table(conn):
    """Create the kv_store table if it doesn't exist yet."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key        TEXT PRIMARY KEY,
                value      JSONB        NOT NULL,
                updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            );
        """)


# ── Public API ────────────────────────────────────────────────────────────────

def db_available() -> bool:
    """True if a PostgreSQL DATABASE_URL is configured and reachable."""
    return _get_conn() is not None


def kv_get(key: str):
    """
    Retrieve a value by key.
    Returns the stored list/dict, or the default for that key, or None.
    """
    conn = _get_conn()
    if conn:
        try:
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                row = cur.fetchone()
                if row:
                    return row["value"]
        except Exception as e:
            log.warning(f"[DB] kv_get({key}) DB error: {e} — trying file")

    # File fallback
    path = _FILE_MAP.get(key)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return _DEFAULT_VALUES.get(key)


def kv_set(key: str, value) -> None:
    """
    Store a value by key (upsert).
    Always writes to both DB (if available) and local file (for localhost dev).
    """
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO kv_store (key, value, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (key) DO UPDATE
                        SET value      = EXCLUDED.value,
                            updated_at = NOW();
                """, (key, json.dumps(value, default=str)))
        except Exception as e:
            log.warning(f"[DB] kv_set({key}) DB error: {e}")

    # Always mirror to local file (keeps localhost working)
    path = _FILE_MAP.get(key)
    if path:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(value, f, indent=2, default=str)
        except Exception as e:
            log.warning(f"[DB] kv_set({key}) file write error: {e}")


def kv_delete(key: str) -> None:
    """Delete a key from the store."""
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
        except Exception as e:
            log.warning(f"[DB] kv_delete({key}) error: {e}")

    path = _FILE_MAP.get(key)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


# ── Migration helper — call once to seed DB from existing local files ─────────

def migrate_files_to_db():
    """
    One-time migration: read each local JSON file and upsert into the DB.
    Safe to call multiple times — only overwrites if local file exists.
    """
    if not db_available():
        print("[DB] No DATABASE_URL — migration skipped.")
        return
    migrated = []
    for key, path in _FILE_MAP.items():
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                kv_set(key, data)
                migrated.append(key)
            except Exception as e:
                print(f"[DB] Migration failed for {key}: {e}")
    print(f"[DB] Migration complete. Keys migrated: {migrated or 'none (files not found)'}")


if __name__ == "__main__":
    print(f"DB available: {db_available()}")
    migrate_files_to_db()
