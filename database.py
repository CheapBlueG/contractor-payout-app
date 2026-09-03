import sqlite3
from datetime import datetime
import pytz

EST = pytz.timezone("America/New_York")

def get_db():
    conn = sqlite3.connect("app.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS contractors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS team_members (
        team_id INTEGER,
        contractor_id INTEGER,
        PRIMARY KEY (team_id, contractor_id),
        FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE,
        FOREIGN KEY(contractor_id) REFERENCES contractors(id) ON DELETE CASCADE
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS weekly_ledgers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_date TEXT NOT NULL,
        contractor_id INTEGER,
        amount_owed_usd REAL NOT NULL,
        status TEXT DEFAULT 'UNPAID',
        paid_at_est TEXT,
        crypto_tx_id TEXT,
        crypto_symbol TEXT,
        notes TEXT,
        FOREIGN KEY(contractor_id) REFERENCES contractors(id)
    )""")
    conn.commit()
    conn.close()

def get_est_now():
    return datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")
