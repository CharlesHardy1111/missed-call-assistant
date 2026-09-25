import sqlite3
from datetime import datetime

DB_NAME = "missed_calls.db"


def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

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

    conn.commit()
    conn.close()


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
    external_call_id=None
):
    follow_up_message = (
        "Hi! Sorry we missed your call. "
        "We received your message and someone will get back to you shortly. "
        "How can we help?"
    )

    time_received = datetime.now().strftime(
        "%b %d, %Y %I:%M %p"
    )

    conn = get_connection()
    cursor = conn.cursor()

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

    call_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return call_id


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