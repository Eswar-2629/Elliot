import json
import logging
import re
from difflib import SequenceMatcher

from anthropic import AsyncAnthropic

from .config import ANTHROPIC_API_KEY, MEMORY_LEARNING, MEMORY_MODEL
from .db import get_connection, ist_now_text
from .memory_store import contains_sensitive_data, delete_item, index_item

logger = logging.getLogger("elliot.memory")
VALID_CATEGORIES = {"preference", "skill", "goal", "personal", "work", "other"}


async def extract_memories(user_text: str, assistant_text: str) -> list[dict]:
    if not MEMORY_LEARNING:
        logger.info("Memory learning paused; extraction skipped")
        return []
    if contains_sensitive_data(user_text):
        logger.info("Memory extraction skipped: sensitive pattern in user text")
        return []
    if not ANTHROPIC_API_KEY:
        logger.warning("Memory extraction skipped: ANTHROPIC_API_KEY missing")
        return []

    prompt = """Extract only durable facts explicitly stated by the user in this turn.
Return strict JSON: {"facts": [{"content": "...", "category": "preference|skill|goal|personal|work|other", "confidence": 0.0-1.0}]}
Do not infer. Do not include passwords, API keys, bank/card details, government IDs, or health details.
If there is no durable fact, return {"facts": []}."""
    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=MEMORY_MODEL,
            max_tokens=600,
            system=prompt,
            messages=[{"role": "user", "content": json.dumps({"user": user_text, "assistant": assistant_text}, ensure_ascii=False)}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        payload = json.loads(text)
        facts = payload.get("facts", []) if isinstance(payload, dict) else []
    except Exception as exc:
        logger.exception("Memory fact extraction failed")
        return []

    saved = []
    now = ist_now_text()
    with get_connection() as connection:
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            content = str(fact.get("content", "")).strip()[:1000]
            category = str(fact.get("category", "other")).strip().lower()
            if category not in VALID_CATEGORIES or not content:
                continue
            if contains_sensitive_data(content):
                logger.info("Memory fact skipped: sensitive pattern")
                continue
            confidence = fact.get("confidence", 0.8)
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                confidence = 0.8
            confidence = max(0.0, min(1.0, float(confidence)))

            existing = connection.execute("SELECT id, content FROM memories WHERE category=?", (category,)).fetchall()
            duplicate = None
            for row in existing:
                ratio = SequenceMatcher(None, content.lower(), row["content"].lower()).ratio()
                if ratio >= 0.72:
                    duplicate = row
                    break

            if duplicate:
                if duplicate["content"].strip() != content:
                    connection.execute("UPDATE memories SET content=?, confidence=?, updated_at=? WHERE id=?",
                                       (content, confidence, now, duplicate["id"]))
                    logger.info("Updated memory id=%s category=%s", duplicate["id"], category)
                    saved.append({"id": duplicate["id"], "content": content, "updated": True})
                else:
                    saved.append({"id": duplicate["id"], "content": content, "updated": False})
            else:
                cursor = connection.execute("""
                    INSERT INTO memories (content, category, source, confidence, created_at, updated_at)
                    VALUES (?, ?, 'user_said', ?, ?, ?)
                """, (content, category, confidence, now, now))
                memory_id = cursor.lastrowid
                logger.info("Saved memory id=%s category=%s", memory_id, category)
                saved.append({"id": memory_id, "content": content, "updated": False})
        connection.commit()

    for item in saved:
        index_item("memories", f"memory-{item['id']}", item["content"], "memory", now)
    return saved


async def auto_title(conversation_id: int, first_user: str) -> str:
    if not ANTHROPIC_API_KEY:
        return first_user[:50] or "New chat"
    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=MEMORY_MODEL, max_tokens=30,
            system="Create a short title of at most 6 words from the user's text. Return only the title.",
            messages=[{"role": "user", "content": first_user[:1000]}],
        )
        title = "".join(block.text for block in response.content if block.type == "text").strip()
        title = title.replace("\n", " ")[:80] or first_user[:50]
    except Exception:
        title = first_user[:50] or "New chat"
    with get_connection() as connection:
        connection.execute("UPDATE conversations SET title=? WHERE id=?", (title, conversation_id))
    return title


async def summarize_conversation(conversation_id: int) -> str:
    with get_connection() as connection:
        conversation = connection.execute("SELECT summary FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        rows = connection.execute("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id ASC", (conversation_id,)).fetchall()
    if len(rows) <= 20:
        return conversation["summary"] if conversation else ""
    old_rows = rows[:-10]
    transcript = "\n".join(f"{row['role']}: {row['content']}" for row in old_rows)
    summary = ""
    if ANTHROPIC_API_KEY:
        try:
            client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            response = await client.messages.create(
                model=MEMORY_MODEL, max_tokens=500,
                system="Summarize the older conversation turns. Preserve user goals, preferences, decisions, and unresolved questions. Do not add facts.",
                messages=[{"role": "user", "content": transcript[:12000]}],
            )
            summary = "".join(block.text for block in response.content if block.type == "text").strip()
        except Exception as exc:
            logger.exception("Conversation summary failed")
    if summary:
        with get_connection() as connection:
            connection.execute("UPDATE conversations SET summary=? WHERE id=?", (summary, conversation_id))
            connection.execute("DELETE FROM messages WHERE conversation_id=? AND id IN (SELECT id FROM messages WHERE conversation_id=? ORDER BY id ASC LIMIT ?)",
                               (conversation_id, conversation_id, len(old_rows)))
        index_item("conversation_summaries", f"conversation-{conversation_id}", summary, "conversation", ist_now_text())
    return summary
