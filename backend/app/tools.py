import json
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .db import get_connection, ist_now_text

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
            "INSERT INTO notes (title, content, created_at) VALUES (?, ?, ?)",
            (title.strip(), content, ist_now_text()),
        )
        note_id = cursor.lastrowid
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
}


async def run_tool(name: str, arguments: dict) -> str:
    if name not in TOOL_REGISTRY:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        function = TOOL_REGISTRY[name]
        if name == "read_webpage":
            result = await function(**arguments)
        else:
            import asyncio
            result = await asyncio.to_thread(function, **arguments)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.exception("Tool failed: %s", name)
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
