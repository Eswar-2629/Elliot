import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("elliot.db")
IST = ZoneInfo("Asia/Kolkata")
BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("ELLIOT_DATA_DIR", str(BACKEND_DIR / "data"))).resolve()
DB_PATH = DATA_DIR / os.getenv("ELLIOT_DB_FILENAME", "jarvis.db")
LEGACY_DB_PATH = BACKEND_DIR / "jarvis.db"


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate_legacy_database() -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists() or not LEGACY_DB_PATH.exists():
        return False
    try:
        shutil.copy2(LEGACY_DB_PATH, DB_PATH)
        logger.info("Migrated legacy SQLite database to %s", DB_PATH)
        return True
    except OSError as exc:
        logger.warning("Could not migrate legacy database: %s", exc)
        return False


def init_db() -> None:
    migrate_legacy_database()
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

        notes_columns = {row[1] for row in connection.execute("PRAGMA table_info(notes)").fetchall()}
        if "updated_at" not in notes_columns:
            connection.execute("ALTER TABLE notes ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT 'New chat',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, summary TEXT NOT NULL DEFAULT ''
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL,
                created_at TEXT NOT NULL, FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('preference','skill','goal','personal','work','other')),
                source TEXT NOT NULL CHECK(source IN ('user_said','inferred')), confidence REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, stored_path TEXT NOT NULL, created_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL, content TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_document_chunks_document ON document_chunks(document_id, chunk_index)")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS profile (
                id INTEGER PRIMARY KEY CHECK (id = 1), full_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', target_roles TEXT NOT NULL DEFAULT '[]',
                skills TEXT NOT NULL DEFAULT '[]', experience TEXT NOT NULL DEFAULT '[]', education TEXT NOT NULL DEFAULT '[]',
                certifications TEXT NOT NULL DEFAULT '[]', projects TEXT NOT NULL DEFAULT '[]', preferred_locations TEXT NOT NULL DEFAULT '[]',
                remote_preference TEXT NOT NULL DEFAULT 'any', salary_min INTEGER NOT NULL DEFAULT 0, salary_max INTEGER NOT NULL DEFAULT 0,
                notice_period TEXT NOT NULL DEFAULT '', languages TEXT NOT NULL DEFAULT '[]', master_resume TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
            )
        """)
        connection.execute("INSERT OR IGNORE INTO profile (id, updated_at) VALUES (1, '')")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
                title TEXT NOT NULL, company TEXT NOT NULL, location TEXT NOT NULL DEFAULT '', remote INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '', url TEXT NOT NULL UNIQUE, posted_at TEXT NOT NULL DEFAULT '', salary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new', match_score INTEGER NOT NULL DEFAULT 0, match_reason TEXT NOT NULL DEFAULT '',
                gaps TEXT NOT NULL DEFAULT '[]', red_flags TEXT NOT NULL DEFAULT '[]', scam_risk INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '', contact_name TEXT NOT NULL DEFAULT '', interview_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(match_score DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS job_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
                draft_type TEXT NOT NULL CHECK(draft_type IN ('resume','cover_letter','answers')),
                content TEXT NOT NULL DEFAULT '', content_diff TEXT NOT NULL DEFAULT '', format TEXT NOT NULL DEFAULT 'text',
                path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
        """)
        connection.commit()


def ist_now_text() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")
