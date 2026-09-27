import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from flask import current_app, has_app_context
from sms import DEFAULT_MESSAGE

DB_NAME = "missed_calls.db"


def get_connection():
    path = current_app.config["DATABASE_PATH"] if has_app_context() else DB_NAME
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_connection()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS missed_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                caller_name TEXT,
                time_received TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'missed',
                follow_up_status TEXT NOT NULL DEFAULT 'pending',
                follow_up_message TEXT NOT NULL
            )
        """)

        # Upgrade an existing Stage 1 database without deleting anything.
        cursor.execute("PRAGMA table_info(missed_calls)")
        existing_columns = {
            row["name"] for row in cursor.fetchall()
        }

        if "call_status" not in existing_columns:
            cursor.execute("""
                ALTER TABLE missed_calls
                ADD COLUMN call_status TEXT
            """)

        if "external_call_id" not in existing_columns:
            cursor.execute("""
                ALTER TABLE missed_calls
                ADD COLUMN external_call_id TEXT
            """)

        # Additive lifecycle migration; old pending rows must never become a send backlog.
        lifecycle_columns = {
            "follow_up_attempted_at": "TEXT",
            "follow_up_completed_at": "TEXT",
            "follow_up_error": "TEXT",
            "follow_up_error_code": "TEXT",
            "follow_up_message_sid": "TEXT",
            "follow_up_provider_status": "TEXT",
        }
        for name, sql_type in lifecycle_columns.items():
            if name not in existing_columns:
                cursor.execute(f"ALTER TABLE missed_calls ADD COLUMN {name} {sql_type}")
        if "follow_up_attempted_at" not in existing_columns:
            cursor.execute("UPDATE missed_calls SET follow_up_status = 'not_attempted' WHERE follow_up_status = 'pending'")
        cursor.execute("""
            UPDATE missed_calls SET follow_up_message = ?
            WHERE follow_up_status IN ('pending', 'not_attempted', 'disabled')
            AND follow_up_message = ?
        """, (DEFAULT_MESSAGE, "Hi! Sorry we missed your call. We received your message and someone will get back to you shortly. How can we help?"))



def call_already_exists(external_call_id):
    if not external_call_id:
        return False

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id
        FROM missed_calls
        WHERE external_call_id = ?
        LIMIT 1
    """, (external_call_id,))

    existing_call = cursor.fetchone()

    conn.close()

    return existing_call is not None


def add_missed_call(
    phone_number,
    caller_name="Unknown Caller",
    call_status="no-answer",
    external_call_id=None,
    deduplicate=False,
):
    """Insert a lead; return None when deduplication skips an existing call ID."""
    follow_up_message = DEFAULT_MESSAGE

    time_received = datetime.now().strftime(
        "%b %d, %Y %I:%M %p"
    )

    # Serialize check + insert for the two independently delivered voice callbacks.
    # BEGIN IMMEDIATE works across Gunicorn workers and needs no schema migration.
    with closing(get_connection()) as conn, conn:
        cursor = conn.cursor()
        if deduplicate and external_call_id:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT id FROM missed_calls WHERE external_call_id = ? LIMIT 1",
                (external_call_id,),
            )
            if cursor.fetchone() is not None:
                return None

        cursor.execute("""
            INSERT INTO missed_calls (
                phone_number,
                caller_name,
                time_received,
                status,
                follow_up_status,
                follow_up_message,
                call_status,
                external_call_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            phone_number,
            caller_name,
            time_received,
            "missed",
            "pending",
            follow_up_message,
            call_status,
            external_call_id
        ))

        return cursor.lastrowid


def get_calls():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM missed_calls
        ORDER BY id DESC
    """)

    calls = cursor.fetchall()

    conn.close()

    return calls


def get_call_by_external_id(external_call_id):
    with closing(get_connection()) as conn:
        return conn.execute(
            "SELECT * FROM missed_calls WHERE external_call_id = ? ORDER BY id LIMIT 1",
            (external_call_id,),
        ).fetchone()


def claim_follow_up(call_id, enabled, skip_reason=None):
    """Commit an exclusive claim BEFORE any network I/O; never reclaim attempts."""
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM missed_calls WHERE id = ?", (call_id,)).fetchone()
        if row is None or row["follow_up_status"] != "pending":
            return None
        if not enabled or skip_reason:
            conn.execute(
                "UPDATE missed_calls SET follow_up_status = ?, follow_up_error = ? WHERE id = ?",
                ("disabled" if not enabled else "not_attempted", skip_reason, call_id),
            )
            return None
        conn.execute(
            "UPDATE missed_calls SET follow_up_status = 'sending', follow_up_attempted_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), call_id),
        )
        return row


def finish_follow_up(call_id, result):
    with closing(get_connection()) as conn, conn:
        conn.execute("""
            UPDATE missed_calls SET follow_up_status = ?, follow_up_completed_at = ?,
                follow_up_message_sid = ?, follow_up_provider_status = ?,
                follow_up_error = ?, follow_up_error_code = ?
            WHERE id = ? AND follow_up_status = 'sending'
        """, (result.status, datetime.now(timezone.utc).isoformat(), result.message_sid,
              result.provider_status, result.error, result.error_code, call_id))
