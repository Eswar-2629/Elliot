import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).resolve().parents[1] / "jarvis.db"
IST = ZoneInfo("Asia/Kolkata")


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                due_at TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                notified INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                published_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                topics TEXT NOT NULL DEFAULT '[]',
                importance INTEGER NOT NULL DEFAULT 1,
                alerted INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS news_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                topics TEXT NOT NULL DEFAULT '[]',
                min_importance INTEGER NOT NULL DEFAULT 4
            )
        """)
        connection.execute("INSERT OR IGNORE INTO news_settings (id, topics, min_importance) VALUES (1, '[]', 4)")


def ist_now_text() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")
