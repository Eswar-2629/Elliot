import json
import logging
from collections import deque
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .agent import run_claude_agent
from .config import (
    AI_PROVIDER,
    CORS_ORIGINS,
    MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    SYSTEM_PROMPT,
)
from .db import get_connection, init_db
from .news_engine import clients, start_news_engine, stop_news_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("elliot")
IST = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Elliot Stage 3", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

chat_history: deque[dict] = deque(maxlen=20)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str = Field(min_length=1, max_length=10000)


class ChatRequest(BaseModel):
    message: str | None = Field(default=None, min_length=1, max_length=10000)
    messages: list[ChatMessage] | None = None


class ChatResponse(BaseModel):
    reply: str


@app.on_event("startup")
async def startup() -> None:
    init_db()
    await start_news_engine(app)


@app.on_event("shutdown")
async def shutdown() -> None:
    await stop_news_engine()


async def chat_with_ollama(messages: list[dict]) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("message", {}).get("content", "I could not generate a text response.")
    except httpx.HTTPError as exc:
        logger.exception("Ollama request failed")
        raise HTTPException(status_code=502, detail="Ollama request failed. Is Ollama running and is the configured model installed?") from exc


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "provider": AI_PROVIDER, "model": MODEL if AI_PROVIDER == "claude" else OLLAMA_MODEL}


async def process_chat(request: ChatRequest) -> ChatResponse:
    if request.message:
        message = request.message.strip()
    elif request.messages:
        user_messages = [item.content.strip() for item in request.messages if item.role == "user"]
        message = user_messages[-1] if user_messages else ""
    else:
        raise HTTPException(status_code=422, detail="Provide message or messages")

    chat_history.append({"role": "user", "content": message})
    history = list(chat_history)

    try:
        if AI_PROVIDER == "claude":
            reply = await run_claude_agent(history)
        elif AI_PROVIDER == "ollama":
            reply = await chat_with_ollama(history)
        else:
            raise HTTPException(status_code=500, detail=f"Unsupported AI_PROVIDER: '{AI_PROVIDER}'. Use 'claude' or 'ollama'.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Chat request failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    chat_history.append({"role": "assistant", "content": reply})
    return ChatResponse(reply=reply)


@app.post("/chat", response_model=ChatResponse)
@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await process_chat(request)


@app.get("/reminders/due")
@app.get("/api/reminders/due")
async def due_reminders() -> dict:
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, text, due_at FROM reminders WHERE done = 0 AND notified = 0 AND due_at <= ? ORDER BY due_at ASC, id ASC",
            (now,),
        ).fetchall()
        reminders = [dict(row) for row in rows]
        if rows:
            connection.execute(
                "UPDATE reminders SET notified = 1 WHERE id IN (%s)" % ",".join("?" for _ in rows),
                [row["id"] for row in rows],
            )
    return {"reminders": reminders}


class NewsSettings(BaseModel):
    topics: list[str] = Field(default_factory=list)
    min_importance: int = Field(default=4, ge=1, le=5)


@app.websocket("/ws/news")
@app.websocket("/api/ws/news")
async def news_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)


@app.get("/news/latest")
@app.get("/api/news/latest")
async def latest_news(limit: int = 20, category: str | None = None, topic: str | None = None) -> dict:
    limit = max(1, min(limit, 100))
    query = "SELECT * FROM articles WHERE 1=1"
    parameters = []
    if category:
        query += " AND category = ?"
        parameters.append(category)
    if topic:
        pattern = f"%{topic}%"
        query += " AND (title LIKE ? OR summary LIKE ? OR topics LIKE ?)"
        parameters.extend([pattern, pattern, pattern])
    query += " ORDER BY published_at DESC LIMIT ?"
    parameters.append(limit)

    with get_connection() as connection:
        rows = connection.execute(query, parameters).fetchall()
    articles = []
    for row in rows:
        article = dict(row)
        try:
            article["topics"] = json.loads(article["topics"])
        except (json.JSONDecodeError, TypeError):
            article["topics"] = []
        articles.append(article)
    return {"articles": articles}


@app.get("/news/settings")
@app.get("/api/news/settings")
async def get_news_settings() -> dict:
    with get_connection() as connection:
        row = connection.execute("SELECT topics, min_importance FROM news_settings WHERE id=1").fetchone()
    topics = json.loads(row["topics"] if row else "[]")
    return {"topics": topics, "min_importance": row["min_importance"] if row else 4}


@app.post("/news/settings")
@app.post("/api/news/settings")
async def set_news_settings(settings: NewsSettings) -> dict:
    clean_topics = []
    for topic in settings.topics:
        topic = topic.strip()
        if topic and topic.lower() not in {item.lower() for item in clean_topics}:
            clean_topics.append(topic)
    with get_connection() as connection:
        connection.execute(
            "UPDATE news_settings SET topics=?, min_importance=? WHERE id=1",
            (json.dumps(clean_topics), settings.min_importance),
        )
    return {"topics": clean_topics, "min_importance": settings.min_importance}
