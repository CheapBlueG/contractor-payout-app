import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz

EST = pytz.timezone("America/New_York")
DATABASE_URL = os.getenv("DATABASE_URL")


def get_db():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set on Render.")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    if not DATABASE_URL:
        print("CRITICAL: DATABASE_URL environment variable is missing!")
        return

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS contractors (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS team_members (
            team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
            contractor_id INTEGER REFERENCES contractors(id) ON DELETE CASCADE,
            PRIMARY KEY (team_id, contractor_id)
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_ledgers (
            id SERIAL PRIMARY KEY,
            week_date VARCHAR(50) NOT NULL,
            contractor_id INTEGER REFERENCES contractors(id) ON DELETE CASCADE,
            amount_owed_usd NUMERIC(10, 2) NOT NULL,
            status VARCHAR(50) DEFAULT 'UNPAID',
            paid_at_est VARCHAR(100),
            crypto_tx_id TEXT,
            crypto_symbol VARCHAR(20),
            notes TEXT
        );
        """)

        # Indexes to keep the dashboard, filtering, and the duplicate-tx
        # check fast as the table grows. Not UNIQUE on crypto_tx_id on
        # purpose: a single transaction is allowed to pay several
        # contractors' rows at once within the same reconciliation call.
        # Reuse of a tx hash across a *different* reconciliation is instead
        # blocked in application code (see /api/reconcile-crypto).
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_weekly_ledgers_week_date
            ON weekly_ledgers (week_date);
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_weekly_ledgers_contractor
            ON weekly_ledgers (contractor_id);
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_weekly_ledgers_tx
            ON weekly_ledgers (crypto_tx_id) WHERE crypto_tx_id IS NOT NULL;
        """)

        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"DATABASE INIT ERROR: {e}")
        if conn:
            conn.close()


def get_est_now():
    return datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")


def unix_to_est(unix_timestamp: int) -> str:
    """Converts a blockchain block timestamp (unix seconds, UTC) into the
    same EST display format used everywhere else in the ledger. Used so
    'Paid At' reflects the moment the sender actually broadcast the
    transaction, not the moment someone clicked 'Verify'."""
    dt_utc = datetime.fromtimestamp(unix_timestamp, tz=pytz.UTC)
    return dt_utc.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S EST")
