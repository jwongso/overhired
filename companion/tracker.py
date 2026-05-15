"""
grapply — application tracker

Persistent SQLite store at ~/.grapply/applications.db.
Tracks every job application with status lifecycle and notes.

Statuses: applied → interviewing → offered → accepted | rejected | ghosted | withdrawn
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

DB_PATH = Path("~/.grapply/applications.db").expanduser()

VALID_STATUSES = {
    "applied", "interviewing", "offered",
    "accepted", "rejected", "ghosted", "withdrawn",
}

# ── Schema ────────────────────────────────────────────────────────────────────

# Bump this whenever the schema changes. Migration code below handles upgrades.
_SCHEMA_VERSION = 3

_DDL = """
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    company      TEXT    NOT NULL,
    date_applied TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'applied',
    notes        TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status       ON applications(status);
CREATE INDEX IF NOT EXISTS idx_company      ON applications(company);
CREATE INDEX IF NOT EXISTS idx_date_applied ON applications(date_applied);

CREATE TABLE IF NOT EXISTS html_strategy_stats (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    domain    TEXT    NOT NULL,
    strategy  TEXT    NOT NULL,
    score     REAL    NOT NULL,
    length    INTEGER NOT NULL,
    time_ms   REAL    NOT NULL,
    selected  INTEGER NOT NULL DEFAULT 0,
    ran_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hss_domain ON html_strategy_stats(domain);

CREATE TABLE IF NOT EXISTS token_usage (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    model            TEXT    NOT NULL,
    endpoint         TEXT    NOT NULL,
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL  NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_tu_ts       ON token_usage(ts);
CREATE INDEX IF NOT EXISTS idx_tu_provider ON token_usage(provider);
CREATE INDEX IF NOT EXISTS idx_tu_endpoint ON token_usage(endpoint);
"""

_MIGRATIONS: dict[int, str] = {
    2: """
CREATE TABLE IF NOT EXISTS token_usage (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    model            TEXT    NOT NULL,
    endpoint         TEXT    NOT NULL,
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL  NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_tu_ts       ON token_usage(ts);
CREATE INDEX IF NOT EXISTS idx_tu_provider ON token_usage(provider);
CREATE INDEX IF NOT EXISTS idx_tu_endpoint ON token_usage(endpoint);
""",
    1: """
CREATE TABLE IF NOT EXISTS html_strategy_stats (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    domain    TEXT    NOT NULL,
    strategy  TEXT    NOT NULL,
    score     REAL    NOT NULL,
    length    INTEGER NOT NULL,
    time_ms   REAL    NOT NULL,
    selected  INTEGER NOT NULL DEFAULT 0,
    ran_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hss_domain ON html_strategy_stats(domain);
""",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations based on PRAGMA user_version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == _SCHEMA_VERSION:
        return
    if current > _SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema v{current} is newer than this code (v{_SCHEMA_VERSION}). "
            "Please update grapply."
        )
    for v in range(current, _SCHEMA_VERSION):
        sql = _MIGRATIONS.get(v)
        if sql:
            conn.executescript(sql)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.commit()


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_DDL)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ── Public tools ──────────────────────────────────────────────────────────────

def log_application(
    domain: str,
    title: str,
    company: str,
    date_applied: str = "",
    notes: str = "",
) -> dict:
    """Record a new job application.

    Args:
        domain:       Company or job board domain, e.g. 'seek.co.nz'.
        title:        Job title.
        company:      Company name.
        date_applied: ISO date YYYY-MM-DD. Defaults to today.
        notes:        Optional notes about this application.
    """
    if not date_applied:
        date_applied = date.today().isoformat()
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO applications (domain,title,company,date_applied,status,notes,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (domain, title, company, date_applied, "applied", notes, now),
        )
        return {
            "id":      cur.lastrowid,
            "title":   title,
            "company": company,
            "status":  "applied",
            "date_applied": date_applied,
        }


def list_applications(
    status: str = "",
    days: int = 0,
    limit: int = 50,
) -> dict:
    """List applications, newest first.

    Args:
        status: Filter by status (applied, interviewing, offered, rejected, ghosted, ...).
                Empty string returns all statuses.
        days:   Only show applications from the last N days. 0 = no limit.
        limit:  Maximum number of results to return.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if days:
        cutoff = date.fromordinal(date.today().toordinal() - days).isoformat()
        clauses.append("date_applied >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM applications {where} ORDER BY date_applied DESC LIMIT ?",
            params,
        ).fetchall()
    return {"applications": [_row_to_dict(r) for r in rows], "count": len(rows)}


def update_application(id: int, status: str = "", notes: str = "") -> dict:
    """Update the status or notes of an existing application.

    Args:
        id:     Application ID (from log_application or list_applications).
        status: New status. One of: applied, interviewing, offered, accepted, rejected, ghosted, withdrawn.
        notes:  Append text to existing notes (leave empty to keep current notes).
    """
    if status and status not in VALID_STATUSES:
        return {"error": f"Invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}"}

    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")

    with _db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (id,)).fetchone()
        if not row:
            return {"error": f"No application with id={id}"}

        new_status = status or row["status"]
        new_notes  = (row["notes"] + "\n" + notes).strip() if notes else row["notes"]

        conn.execute(
            "UPDATE applications SET status=?, notes=?, updated_at=? WHERE id=?",
            (new_status, new_notes, now, id),
        )
    return {"id": id, "status": new_status, "updated_at": now}


def get_stats() -> dict:
    """Return aggregate statistics across all tracked applications."""
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        by_status = {
            row["status"]: row["cnt"]
            for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM applications GROUP BY status"
            ).fetchall()
        }
        # Response rate = (interviewing + offered + accepted) / total
        responded = sum(by_status.get(s, 0) for s in ("interviewing", "offered", "accepted"))
        response_rate = round(responded / total * 100, 1) if total else 0.0

        # Average days from applied to first status update beyond 'applied'
        avg_days_row = conn.execute("""
            SELECT AVG(
                julianday(updated_at) - julianday(date_applied)
            ) as avg_days
            FROM applications
            WHERE status != 'applied'
        """).fetchone()
        avg_days = round(avg_days_row["avg_days"] or 0, 1)

    return {
        "total":          total,
        "by_status":      by_status,
        "response_rate":  f"{response_rate}%",
        "avg_days_to_reply": avg_days,
    }


def delete_application(id: int) -> dict:
    """Permanently delete an application record.

    Args:
        id: Application ID to delete.
    """
    with _db() as conn:
        row = conn.execute("SELECT company, title FROM applications WHERE id=?", (id,)).fetchone()
        if not row:
            return {"error": f"No application with id={id}"}
        conn.execute("DELETE FROM applications WHERE id=?", (id,))
    return {"deleted": id, "company": row["company"], "title": row["title"]}


# ── Token usage tracking ──────────────────────────────────────────────────────

# Known pricing per 1M tokens (prompt, completion). Update as prices change.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1":            (2.00,  8.00),
    "gpt-4.1-mini":       (0.40,  1.60),
    "gpt-4.1-nano":       (0.10,  0.40),
    "gpt-4o":             (2.50, 10.00),
    "gpt-4o-mini":        (0.15,  0.60),
    "claude-opus-4-7":    (15.0, 75.00),
    "claude-sonnet-4-6":  (3.00, 15.00),
    "claude-haiku-4-5":   (0.80,  4.00),
}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    for name, (p_price, c_price) in _PRICING.items():
        if name in model:
            return round(
                (prompt_tokens * p_price + completion_tokens * c_price) / 1_000_000, 6
            )
    return 0.0


def log_token_usage(
    provider: str,
    model: str,
    endpoint: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Record token usage for one AI call.

    Args:
        provider:          'ollama', 'openai', or 'claude'.
        model:             Model name, e.g. 'gpt-4.1-mini'.
        endpoint:          Which API endpoint triggered the call, e.g. 'generate', 'extract'.
        prompt_tokens:     Input tokens consumed.
        completion_tokens: Output tokens generated.
    """
    total = prompt_tokens + completion_tokens
    cost  = _estimate_cost(model, prompt_tokens, completion_tokens)
    now   = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    with _db() as conn:
        conn.execute(
            "INSERT INTO token_usage "
            "(ts, provider, model, endpoint, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, provider, model, endpoint, prompt_tokens, completion_tokens, total, cost),
        )


def get_token_daily(
    days: int = 30,
    provider: str = "",
    endpoint: str = "",
) -> list[dict]:
    """Return daily token usage aggregates, ordered by date ascending.

    Args:
        days:     Number of past days to include. 0 = all time.
        provider: Filter by provider. Empty = all.
        endpoint: Filter by endpoint. Empty = all.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if days:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
        clauses.append("ts >= ?")
        params.append(cutoff)
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if endpoint:
        clauses.append("endpoint = ?")
        params.append(endpoint)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _db() as conn:
        rows = conn.execute(
            f"""
            SELECT DATE(ts) AS date,
                   COUNT(*) AS calls,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   ROUND(SUM(estimated_cost_usd), 6) AS cost_usd
            FROM token_usage {where}
            GROUP BY DATE(ts)
            ORDER BY date ASC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_token_stats(
    provider: str = "",
    model: str = "",
    endpoint: str = "",
    days: int = 0,
) -> dict:
    """Return aggregate token usage and estimated cost.

    Args:
        provider: Filter by provider. Empty = all.
        model:    Filter by model. Empty = all.
        endpoint: Filter by endpoint. Empty = all.
        days:     Only include records from the last N days. 0 = all time.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if model:
        clauses.append("model = ?")
        params.append(model)
    if endpoint:
        clauses.append("endpoint = ?")
        params.append(endpoint)
    if days:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
        clauses.append("ts >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with _db() as conn:
        totals = conn.execute(
            f"""
            SELECT
                COUNT(*)                        AS calls,
                SUM(prompt_tokens)              AS prompt_tokens,
                SUM(completion_tokens)          AS completion_tokens,
                SUM(total_tokens)               AS total_tokens,
                ROUND(SUM(estimated_cost_usd), 6) AS total_cost_usd
            FROM token_usage {where}
            """,
            params,
        ).fetchone()

        by_model = conn.execute(
            f"""
            SELECT provider, model, endpoint,
                COUNT(*)                           AS calls,
                SUM(prompt_tokens)                 AS prompt_tokens,
                SUM(completion_tokens)             AS completion_tokens,
                ROUND(SUM(estimated_cost_usd), 6)  AS cost_usd
            FROM token_usage {where}
            GROUP BY provider, model, endpoint
            ORDER BY cost_usd DESC
            """,
            params,
        ).fetchall()

    return {
        "summary":  dict(totals),
        "by_model": [dict(r) for r in by_model],
    }


# ── HTML strategy catalog ─────────────────────────────────────────────────────

def log_strategy_run(domain: str, results: list[dict], selected: str) -> None:
    """Persist one benchmark run (all strategies) for a domain."""
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    with _db() as conn:
        conn.executemany(
            "INSERT INTO html_strategy_stats "
            "(domain, strategy, score, length, time_ms, selected, ran_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    domain,
                    r["strategy"],
                    r["score"],
                    r["length"],
                    r["time_ms"],
                    1 if r["strategy"] == selected else 0,
                    now,
                )
                for r in results
            ],
        )


def get_best_strategy(domain: str, min_runs: int = 1) -> str | None:
    """Return the historically best strategy for a domain, or None if not enough data.

    Ranks by average score. Only considers strategies with avg_score > 0
    so zero-scoring stale entries never override a working strategy.
    """
    with _db() as conn:
        row = conn.execute(
            """
            SELECT strategy, AVG(score) AS avg_score, COUNT(*) AS runs
            FROM html_strategy_stats
            WHERE domain = ?
            GROUP BY strategy
            HAVING runs >= ? AND avg_score > 0
            ORDER BY avg_score DESC
            LIMIT 1
            """,
            (domain, min_runs),
        ).fetchone()
    return row["strategy"] if row else None


def get_strategy_catalog(domain: str | None = None) -> list[dict]:
    """Return the strategy catalog, optionally filtered by domain.

    Each row: domain, strategy, avg_score, runs, last_run.
    Ordered by domain then avg_score desc.
    """
    with _db() as conn:
        if domain:
            rows = conn.execute(
                """
                SELECT domain, strategy,
                       ROUND(AVG(score), 4) AS avg_score,
                       ROUND(AVG(time_ms), 2) AS avg_time_ms,
                       COUNT(*) AS runs,
                       MAX(ran_at) AS last_run
                FROM html_strategy_stats
                WHERE domain = ?
                GROUP BY domain, strategy
                ORDER BY avg_score DESC
                """,
                (domain,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT domain, strategy,
                       ROUND(AVG(score), 4) AS avg_score,
                       ROUND(AVG(time_ms), 2) AS avg_time_ms,
                       COUNT(*) AS runs,
                       MAX(ran_at) AS last_run
                FROM html_strategy_stats
                GROUP BY domain, strategy
                ORDER BY domain, avg_score DESC
                """,
            ).fetchall()
    return [dict(r) for r in rows]
