"""
overhired — application tracker

Persistent SQLite store at ~/.overhired/applications.db.
Tracks every job application with status lifecycle and notes.

Statuses: applied → interviewing → offered → accepted | rejected | ghosted | withdrawn
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

DB_PATH = Path("~/.overhired/applications.db").expanduser()

VALID_STATUSES = {
    "applied", "interviewing", "offered",
    "accepted", "rejected", "ghosted", "withdrawn",
}

# ── Schema ────────────────────────────────────────────────────────────────────

# Bump this whenever the schema changes. Migration code below handles upgrades.
_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    company      TEXT    NOT NULL,
    date_applied TEXT    NOT NULL,   -- ISO date YYYY-MM-DD
    status       TEXT    NOT NULL DEFAULT 'applied',
    notes        TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL    -- ISO datetime
);
CREATE INDEX IF NOT EXISTS idx_status       ON applications(status);
CREATE INDEX IF NOT EXISTS idx_company      ON applications(company);
CREATE INDEX IF NOT EXISTS idx_date_applied ON applications(date_applied);
"""

# Migration scripts keyed by the version they upgrade FROM.
# e.g. _MIGRATIONS[1] upgrades schema v1 → v2.
_MIGRATIONS: dict[int, str] = {
    # Example (uncomment and edit when v2 is needed):
    # 1: "ALTER TABLE applications ADD COLUMN url TEXT NOT NULL DEFAULT '';",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply any pending schema migrations based on PRAGMA user_version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == _SCHEMA_VERSION:
        return
    if current > _SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema v{current} is newer than this code (v{_SCHEMA_VERSION}). "
            "Please update overhired."
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
