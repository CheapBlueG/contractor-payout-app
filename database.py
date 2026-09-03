import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz

EST = pytz.timezone("America/New_York")
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    """Establishes connection to the Render PostgreSQL database."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    """Initializes tables using PostgreSQL syntax on app startup."""
    if not DATABASE_URL:
        print("DATABASE_URL environment variable is missing!")
        return

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

    conn.commit()
    cursor.close()
    conn.close()

def get_est_now():
    """Returns the current timestamp explicitly formatted in Eastern Standard Time."""
    return datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")
