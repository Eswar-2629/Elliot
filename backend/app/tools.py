import json
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .db import get_connection, ist_now_text
from .memory_store import delete_item, index_item, recall

logger = logging.getLogger("elliot.tools")
IST = ZoneInfo("Asia/Kolkata")


def _due_at_error() -> str:
    return "due_at must use the IST format YYYY-MM-DD HH:MM"


async def read_webpage(url: str) -> dict:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = await client.get(url, headers={"User-Agent": "ElliotAssistant/1.0"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not read webpage: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.stripped_strings)
    return {"url": url, "text": text[:6000]}


def web_search(query: str, max_results: int = 5) -> dict:
    max_results = max(1, min(int(max_results or 5), 10))
    results = []

    try:
        from ddgs import DDGS
        with DDGS() as search:
            for item in search.text(query, max_results=max_results):
                results.append({"title": item.get("title", ""), "url": item.get("href", ""), "snippet": item.get("body", "")})
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as search:
                for item in search.text(query, max_results=max_results):
                    results.append({"title": item.get("title", ""), "url": item.get("href", ""), "snippet": item.get("body", "")})
        except ImportError as exc:
            raise RuntimeError("Install ddgs to enable web search") from exc

    return {"query": query, "results": results}


def add_reminder(text: str, due_at: str) -> dict:
    try:
        parsed = datetime.strptime(due_at, "%Y-%m-%d %H:%M")
        parsed = parsed.replace(tzinfo=IST)
    except ValueError as exc:
        raise ValueError(_due_at_error()) from exc

    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO reminders (text, due_at, done, notified) VALUES (?, ?, 0, 0)",
            (text.strip(), due_at),
        )
        reminder_id = cursor.lastrowid
    return {"id": reminder_id, "text": text.strip(), "due_at": due_at, "done": False}


def list_reminders(include_done: bool = False) -> dict:
    query = "SELECT id, text, due_at, done, notified FROM reminders"
    parameters = []
    if not include_done:
        query += " WHERE done = 0"
    query += " ORDER BY due_at ASC, id ASC"

    with get_connection() as connection:
        rows = connection.execute(query, parameters).fetchall()
    reminders = [dict(row) | {"done": bool(row["done"]), "notified": bool(row["notified"])} for row in rows]
    return {"reminders": reminders}


def complete_reminder(reminder_id: int) -> dict:
    with get_connection() as connection:
        cursor = connection.execute("UPDATE reminders SET done = 1 WHERE id = ?", (int(reminder_id),))
        if cursor.rowcount == 0:
            raise ValueError(f"Reminder {reminder_id} does not exist")
    return {"id": int(reminder_id), "done": True}


def add_note(content: str, title: str = "") -> dict:
    content = content.strip()
    if not content:
        raise ValueError("Note content cannot be empty")
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO notes (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title.strip(), content, ist_now_text(), ist_now_text()),
        )
        note_id = cursor.lastrowid
    index_item("notes", f"note-{note_id}", f"{title.strip()}\n{content}".strip(), f"note:{note_id}", ist_now_text())
    return {"id": note_id, "title": title.strip(), "content": content}


def search_notes(query: str) -> dict:
    pattern = f"%{query.strip()}%"
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, title, content, created_at FROM notes WHERE title LIKE ? OR content LIKE ? ORDER BY id DESC",
            (pattern, pattern),
        ).fetchall()
    return {"notes": [dict(row) for row in rows]}


def delete_note(note_id: int) -> dict:
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
        if cursor.rowcount == 0:
            raise ValueError(f"Note {note_id} does not exist")
    delete_item("notes", f"note-{int(note_id)}")
    return {"id": int(note_id), "deleted": True}


def get_latest_news(topic: str = "", hours: int = 24, limit: int = 10) -> dict:
    hours = max(1, min(int(hours or 24), 168))
    limit = max(1, min(int(limit or 10), 50))
    cutoff = datetime.now(IST) - timedelta(hours=hours)
    cutoff_text = cutoff.isoformat(timespec="seconds")
    pattern = f"%{topic.strip()}%" if topic.strip() else "%"
    with get_connection() as connection:
        rows = connection.execute("""
            SELECT source, category, title, url, published_at, summary, topics, importance, alerted
            FROM articles
            WHERE published_at >= ? AND (title LIKE ? OR summary LIKE ? OR topics LIKE ?)
            ORDER BY published_at DESC LIMIT ?
        """, (cutoff_text, pattern, pattern, pattern, limit)).fetchall()
    return {"topic": topic, "hours": hours, "articles": [dict(row) for row in rows]}


def summarize_news(topic: str = "", hours: int = 2) -> dict:
    return get_latest_news(topic=topic, hours=hours, limit=30)


def set_news_alert(topic: str) -> dict:
    topic = topic.strip()
    if not topic:
        raise ValueError("Topic cannot be empty")
    with get_connection() as connection:
        row = connection.execute("SELECT topics FROM news_settings WHERE id=1").fetchone()
        topics = json.loads(row["topics"] if row else "[]")
        if topic.lower() not in {item.lower() for item in topics}:
            topics.append(topic)
        connection.execute("UPDATE news_settings SET topics=? WHERE id=1", (json.dumps(topics),))
    return {"topics": topics}


def remove_news_alert(topic: str) -> dict:
    topic = topic.strip().lower()
    with get_connection() as connection:
        row = connection.execute("SELECT topics FROM news_settings WHERE id=1").fetchone()
        topics = json.loads(row["topics"] if row else "[]")
        topics = [item for item in topics if item.lower() != topic]
        connection.execute("UPDATE news_settings SET topics=? WHERE id=1", (json.dumps(topics),))
    return {"topics": topics}


def remember(content: str, category: str = "other") -> dict:
    content = content.strip()
    category = category.lower()
    if not content:
        raise ValueError("Memory content cannot be empty")
    if category not in {"preference", "skill", "goal", "personal", "work", "other"}:
        category = "other"
    from .memory_store import contains_sensitive_data
    if contains_sensitive_data(content):
        return {"saved": False, "reason": "Sensitive data was blocked. I will not store passwords, keys, card, bank, ID, or health details."}
    now = ist_now_text()
    with get_connection() as connection:
        cursor = connection.execute("""
            INSERT INTO memories (content, category, source, confidence, created_at, updated_at)
            VALUES (?, ?, 'user_said', 1.0, ?, ?)
        """, (content, category, now, now))
        memory_id = cursor.lastrowid
    index_item("memories", f"memory-{memory_id}", content, "memory", now)
    return {"id": memory_id, "content": content, "category": category, "saved": True}


def recall_memory(query: str) -> dict:
    return {"matches": recall(query, types=["memories", "notes", "document_chunks", "conversation_summaries"], top_k=8)}


def forget(memory_id: int) -> dict:
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM memories WHERE id=?", (int(memory_id),))
        if cursor.rowcount == 0:
            raise ValueError(f"Memory {memory_id} does not exist")
    delete_item("memories", f"memory-{int(memory_id)}")
    return {"id": int(memory_id), "deleted": True}


def list_memories(category: str = "") -> dict:
    with get_connection() as connection:
        if category:
            rows = connection.execute("SELECT * FROM memories WHERE category=? ORDER BY updated_at DESC", (category.lower(),)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()
    return {"memories": [dict(row) for row in rows]}


def search_documents(query: str) -> dict:
    matches = recall(query, types=["document_chunks"], top_k=8)
    return {"matches": matches}


def find_jobs(query: str = "", location: str = "", remote: bool = False, days: int = 30) -> dict:
    pattern = f"%{query}%" if query else "%"
    with get_connection() as connection:
        rows = connection.execute("""
            SELECT id, title, company, location, remote, url, posted_at, salary, status, match_score, match_reason, gaps, red_flags
            FROM jobs WHERE (title LIKE ? OR company LIKE ? OR description LIKE ?)
            AND location LIKE ? ORDER BY match_score DESC, created_at DESC LIMIT 50
        """, (pattern, pattern, pattern, f"%{location}%" if location else "%")).fetchall()
    return {"jobs": [dict(row) for row in rows]}


def show_job(job_id: int) -> dict:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    if not row:
        return {"error": "Job not found"}
    result = dict(row)
    for field in ("gaps", "red_flags"):
        try:
            result[field] = json.loads(result[field])
        except (json.JSONDecodeError, TypeError):
            result[field] = []
    return result


async def score_job_tool(job_id: int) -> dict:
    from .matcher import score_job
    return await score_job(job_id)


def update_job_status_tool(job_id: int, status: str, note: str = "") -> dict:
    allowed = {"new", "saved", "applied", "interview", "offer", "rejected", "ignored"}
    if status not in allowed:
        return {"error": "Invalid job status"}
    now = ist_now_text()
    with get_connection() as connection:
        cursor = connection.execute("UPDATE jobs SET status=?, notes=?, updated_at=? WHERE id=?", (status, note, now, int(job_id)))
        if cursor.rowcount == 0:
            return {"error": "Job not found"}
    if status == "applied":
        from datetime import datetime, timedelta
        due = (datetime.now(IST) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        with get_connection() as connection:
            connection.execute("INSERT INTO reminders (text, due_at, done, notified) VALUES (?, ?, 0, 0)", (f"Follow up on job application #{job_id}", due))
    return {"job_id": int(job_id), "status": status, "note": note}


def list_applications(status: str = "") -> dict:
    with get_connection() as connection:
        if status:
            rows = connection.execute("SELECT * FROM jobs WHERE status=? ORDER BY updated_at DESC", (status,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM jobs WHERE status NOT IN ('new','ignored') ORDER BY updated_at DESC").fetchall()
    return {"applications": [dict(row) for row in rows]}


async def add_job_from_text_tool(text: str = "", url: str = "") -> dict:
    if url and any(site in url.lower() for site in ("linkedin.com", "naukri.com", "indeed.com")):
        return {"error": "This site is not supported for automated access. Paste the public job details instead."}
    if not text and not url:
        return {"error": "Provide text or a URL"}
    now = ist_now_text()
    source_url = url or f"manual://{abs(hash(text))}"
    title = text.strip().splitlines()[0][:200] if text.strip() else "Pasted job"
    flags = []
    lowered = text.lower()
    for marker in ("registration fee", "pay", "processing fee", "send otp", "password"):
        if marker in lowered:
            flags.append(marker)
    with get_connection() as connection:
        existing = connection.execute("SELECT id FROM jobs WHERE url=?", (source_url,)).fetchone()
        if existing:
            return {"job_id": existing["id"], "duplicate": True}
        cursor = connection.execute("""
            INSERT INTO jobs (external_id, source, title, company, location, remote, description, url, posted_at, salary,
            status, match_score, match_reason, gaps, red_flags, scam_risk, created_at, updated_at)
            VALUES ('manual', 'Manual paste', ?, 'Unknown', '', 0, ?, ?, ?, '', 'new', 0, '', '[]', ?, ?, ?, ?)
        """, (title, text, source_url, now, json.dumps(flags), 1 if flags else 0, now, now))
    return {"job_id": cursor.lastrowid, "duplicate": False, "red_flags": flags}


async def generate_tailor_tool(job_id: int) -> dict:
    from .resume import generate_tailored_resume
    return await generate_tailored_resume(job_id)


async def generate_cover_tool(job_id: int, tone: str = "formal") -> dict:
    from .resume import write_cover_letter
    return await write_cover_letter(job_id, tone)


def set_job_preferences_tool(target_roles: list[str] | None = None, preferred_locations: list[str] | None = None,
                             remote_preference: str | None = None, salary_min: int | None = None,
                             salary_max: int | None = None) -> dict:
    from .profile import update_profile
    payload = {}
    if target_roles is not None: payload["target_roles"] = target_roles
    if preferred_locations is not None: payload["preferred_locations"] = preferred_locations
    if remote_preference is not None: payload["remote_preference"] = remote_preference
    if salary_min is not None: payload["salary_min"] = salary_min
    if salary_max is not None: payload["salary_max"] = salary_max
    return update_profile(payload)


TOOLS = [
    {"name": "web_search", "description": "Search the web. Never invent results.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}}, "required": ["query"]}},
    {"name": "read_webpage", "description": "Fetch and extract readable text from a public webpage URL.", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "add_reminder", "description": "Create an IST reminder due at an exact date and time.", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "due_at": {"type": "string", "description": "IST time in YYYY-MM-DD HH:MM format"}}, "required": ["text", "due_at"]}},
    {"name": "list_reminders", "description": "List reminders, optionally including completed reminders.", "input_schema": {"type": "object", "properties": {"include_done": {"type": "boolean", "default": False}}, "required": []}},
    {"name": "complete_reminder", "description": "Mark a reminder as completed by its numeric ID.", "input_schema": {"type": "object", "properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]}},
    {"name": "add_note", "description": "Save a note for later retrieval.", "input_schema": {"type": "object", "properties": {"content": {"type": "string"}, "title": {"type": "string", "default": ""}}, "required": ["content"]}},
    {"name": "search_notes", "description": "Search saved notes by title or content.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "delete_note", "description": "Permanently delete a note by numeric ID. Only call after the user confirms deletion.", "input_schema": {"type": "object", "properties": {"note_id": {"type": "integer"}}, "required": ["note_id"]}},
    {"name": "get_latest_news", "description": "Search articles already stored by Elliot within a recent number of hours.", "input_schema": {"type": "object", "properties": {"topic": {"type": "string", "default": ""}, "hours": {"type": "integer", "default": 24}, "limit": {"type": "integer", "default": 10}}, "required": []}},
    {"name": "summarize_news", "description": "Get stored articles for a topic and period so you can write a sourced briefing.", "input_schema": {"type": "object", "properties": {"topic": {"type": "string", "default": ""}, "hours": {"type": "integer", "default": 2}}, "required": []}},
    {"name": "set_news_alert", "description": "Add a topic to live news alerts.", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    {"name": "remove_news_alert", "description": "Remove a topic from live news alerts.", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    {"name": "remember", "description": "Store an explicitly requested durable fact about the user. Sensitive secrets are blocked.", "input_schema": {"type": "object", "properties": {"content": {"type": "string"}, "category": {"type": "string", "enum": ["preference", "skill", "goal", "personal", "work", "other"]}}, "required": ["content"]}},
    {"name": "recall_memory", "description": "Search long-term user memories, notes, documents, and conversation summaries.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "forget", "description": "Delete a memory by ID. Only use after confirmation.", "input_schema": {"type": "object", "properties": {"memory_id": {"type": "integer"}}, "required": ["memory_id"]}},
    {"name": "list_memories", "description": "List remembered facts, optionally filtered by category.", "input_schema": {"type": "object", "properties": {"category": {"type": "string", "enum": ["", "preference", "skill", "goal", "personal", "work", "other"]}}, "required": []}},
    {"name": "search_documents", "description": "Search uploaded document chunks by meaning.", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find_jobs", "description": "Find stored jobs with optional query, location, remote and age filters.", "input_schema": {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "location": {"type": "string", "default": ""}, "remote": {"type": "boolean", "default": False}, "days": {"type": "integer", "default": 30}}, "required": []}},
    {"name": "show_job", "description": "Show one stored job and its match details.", "input_schema": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]}},
    {"name": "score_job", "description": "Score a job against the user's profile with Claude.", "input_schema": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]}},
    {"name": "tailor_resume", "description": "Generate a truthful tailored resume draft for a job.", "input_schema": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]}},
    {"name": "write_cover_letter", "description": "Generate a factual cover letter draft for a job.", "input_schema": {"type": "object", "properties": {"job_id": {"type": "integer"}, "tone": {"type": "string", "enum": ["formal", "friendly", "concise"]}}, "required": ["job_id"]}},
    {"name": "update_job_status", "description": "Update a job application status and optional note.", "input_schema": {"type": "object", "properties": {"job_id": {"type": "integer"}, "status": {"type": "string", "enum": ["new", "saved", "applied", "interview", "offer", "rejected", "ignored"]}, "note": {"type": "string", "default": ""}}, "required": ["job_id", "status"]}},
    {"name": "list_applications", "description": "List jobs in the application tracker by status.", "input_schema": {"type": "object", "properties": {"status": {"type": "string", "default": ""}}, "required": []}},
    {"name": "add_job_from_text", "description": "Add a pasted job URL or description without bypassing job-site rules.", "input_schema": {"type": "object", "properties": {"text": {"type": "string", "default": ""}, "url": {"type": "string", "default": ""}}, "required": []}},
    {"name": "set_job_preferences", "description": "Update job search roles, locations, remote preference and salary range.", "input_schema": {"type": "object", "properties": {"target_roles": {"type": "array", "items": {"type": "string"}}, "preferred_locations": {"type": "array", "items": {"type": "string"}}, "remote_preference": {"type": "string", "enum": ["any", "remote", "hybrid", "onsite"]}, "salary_min": {"type": "integer"}, "salary_max": {"type": "integer"}}, "required": []}},
]

TOOL_REGISTRY = {
    "web_search": web_search,
    "read_webpage": read_webpage,
    "add_reminder": add_reminder,
    "list_reminders": list_reminders,
    "complete_reminder": complete_reminder,
    "add_note": add_note,
    "search_notes": search_notes,
    "delete_note": delete_note,
    "get_latest_news": get_latest_news,
    "summarize_news": summarize_news,
    "set_news_alert": set_news_alert,
    "remove_news_alert": remove_news_alert,
    "remember": remember,
    "recall_memory": recall_memory,
    "forget": forget,
    "list_memories": list_memories,
    "search_documents": search_documents,
    "find_jobs": find_jobs,
    "show_job": show_job,
    "score_job": score_job_tool,
    "tailor_resume": generate_tailor_tool,
    "write_cover_letter": generate_cover_tool,
    "update_job_status": update_job_status_tool,
    "list_applications": list_applications,
    "add_job_from_text": add_job_from_text_tool,
    "set_job_preferences": set_job_preferences_tool,
}


async def run_tool(name: str, arguments: dict) -> str:
    if name not in TOOL_REGISTRY:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        function = TOOL_REGISTRY[name]
        if name in {"read_webpage", "score_job", "tailor_resume", "write_cover_letter", "add_job_from_text"}:
            result = await function(**arguments)
        else:
            import asyncio
            result = await asyncio.to_thread(function, **arguments)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.exception("Tool failed: %s", name)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
