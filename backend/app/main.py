import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse, Response
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
from .db import DATA_DIR, get_connection, init_db, ist_now_text
from .news_engine import clients, start_news_engine, stop_news_engine
from .memory_store import delete_item, index_item, sync_existing_notes
from .memory import auto_title, extract_memories, summarize_conversation
from .config import MEMORY_LEARNING
from .profile import get_profile, update_profile, profile_prefill_memories
from .job_hunter import application_stats, fetch_jobs_manual, start_job_scheduler, stop_job_scheduler, tracker_csv
from .matcher import scam_flags, score_job
from .resume import application_answers, export_text_draft, generate_tailored_resume, write_cover_letter, extract_resume_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("elliot")
IST = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Elliot Stage 5", version="0.5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str = Field(min_length=1, max_length=10000)


class ChatRequest(BaseModel):
    message: str | None = Field(default=None, min_length=1, max_length=10000)
    messages: list[ChatMessage] | None = None
    conversation_id: int | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: int | None = None


@app.on_event("startup")
async def startup() -> None:
    init_db()
    sync_existing_notes()
    await start_news_engine(app)
    await start_job_scheduler(app)


@app.on_event("shutdown")
async def shutdown() -> None:
    await stop_news_engine()
    await stop_job_scheduler()


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

    now = ist_now_text()
    with get_connection() as connection:
        if request.conversation_id:
            conversation = connection.execute("SELECT id, summary FROM conversations WHERE id=?", (request.conversation_id,)).fetchone()
            if not conversation:
                raise HTTPException(status_code=404, detail="Conversation not found")
            conversation_id = request.conversation_id
        else:
            cursor = connection.execute("INSERT INTO conversations (title, created_at, updated_at, summary) VALUES (?, ?, ?, '')",
                                       ("New chat", now, now))
            conversation_id = cursor.lastrowid
            conversation = {"summary": ""}
        connection.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                           (conversation_id, message, now))
        connection.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        rows = connection.execute("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 10", (conversation_id,)).fetchall()
        history_rows = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    history = list(history_rows)
    if conversation["summary"]:
        history = [{"role": "assistant", "content": "Rolling conversation summary: " + conversation["summary"]}] + history

    try:
        if AI_PROVIDER == "claude":
            reply = await run_claude_agent(history, message)
        elif AI_PROVIDER == "ollama":
            reply = await chat_with_ollama(history)
        else:
            raise HTTPException(status_code=500, detail=f"Unsupported AI_PROVIDER: '{AI_PROVIDER}'. Use 'claude' or 'ollama'.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Chat request failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    now = ist_now_text()
    with get_connection() as connection:
        connection.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                           (conversation_id, reply, now))
        connection.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        user_count = connection.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'", (conversation_id,)).fetchone()[0]
    if user_count == 1:
        asyncio.create_task(auto_title(conversation_id, message))
    asyncio.create_task(summarize_conversation(conversation_id))
    if MEMORY_LEARNING:
        asyncio.create_task(extract_memories(message, reply))
    return ChatResponse(reply=reply, conversation_id=conversation_id)


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


@app.get("/conversations")
async def list_conversations() -> dict:
    with get_connection() as connection:
        rows = connection.execute("SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC").fetchall()
    return {"conversations": [dict(row) for row in rows]}


@app.post("/conversations")
async def new_conversation() -> dict:
    now = ist_now_text()
    with get_connection() as connection:
        cursor = connection.execute("INSERT INTO conversations (title, created_at, updated_at, summary) VALUES ('New chat', ?, ?, '')", (now, now))
        conversation_id = cursor.lastrowid
    return {"id": conversation_id, "title": "New chat", "created_at": now, "updated_at": now}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: int) -> dict:
    with get_connection() as connection:
        conversation = connection.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        rows = connection.execute("SELECT id, role, content, created_at FROM messages WHERE conversation_id=? ORDER BY id ASC", (conversation_id,)).fetchall()
    return {"conversation": dict(conversation), "messages": [dict(row) for row in rows]}


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int) -> dict:
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Conversation not found")
    delete_item("conversation_summaries", f"conversation-{conversation_id}")
    return {"deleted": True, "id": conversation_id}


class MemoryPatch(BaseModel):
    content: str | None = None
    category: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


@app.get("/memories")
async def list_all_memories(category: str | None = None) -> dict:
    with get_connection() as connection:
        if category:
            rows = connection.execute("SELECT * FROM memories WHERE category=? ORDER BY updated_at DESC", (category.lower(),)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()
    return {"memories": [dict(row) for row in rows]}


@app.patch("/memories/{memory_id}")
async def patch_memory(memory_id: int, patch: MemoryPatch) -> dict:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Memory not found")
        content = patch.content.strip() if patch.content is not None else row["content"]
        category = patch.category.lower() if patch.category else row["category"]
        if category not in {"preference", "skill", "goal", "personal", "work", "other"}:
            raise HTTPException(status_code=422, detail="Invalid memory category")
        if not content:
            raise HTTPException(status_code=422, detail="Memory content cannot be empty")
        from .memory_store import contains_sensitive_data
        if contains_sensitive_data(content):
            raise HTTPException(status_code=422, detail="Sensitive data cannot be stored as a memory")
        confidence = patch.confidence if patch.confidence is not None else row["confidence"]
        connection.execute("UPDATE memories SET content=?, category=?, confidence=?, updated_at=? WHERE id=?",
                           (content, category, confidence, ist_now_text(), memory_id))
    index_item("memories", f"memory-{memory_id}", content, "memory", ist_now_text())
    return {"id": memory_id, "content": content, "category": category, "confidence": confidence}


@app.delete("/memories/{memory_id}")
async def delete_memory(memory_id: int) -> dict:
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Memory not found")
    delete_item("memories", f"memory-{memory_id}")
    logger.info("Deleted memory id=%s", memory_id)
    return {"deleted": True, "id": memory_id}


@app.delete("/memories")
async def wipe_memories(confirm: bool = Query(False)) -> dict:
    if not confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to wipe all memories")
    with get_connection() as connection:
        rows = connection.execute("SELECT id FROM memories").fetchall()
        connection.execute("DELETE FROM memories")
    for row in rows:
        delete_item("memories", f"memory-{row['id']}")
    logger.info("Wiped all memories count=%s", len(rows))
    return {"deleted": True, "count": len(rows)}


@app.get("/memories/export")
async def export_memories() -> Response:
    with get_connection() as connection:
        export_data = {
            "memories": [dict(row) for row in connection.execute("SELECT * FROM memories ORDER BY id")],
            "notes": [dict(row) for row in connection.execute("SELECT * FROM notes ORDER BY id")],
            "conversations": [],
        }
        conversations = connection.execute("SELECT * FROM conversations ORDER BY id").fetchall()
        for conversation in conversations:
            messages = connection.execute("SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY id", (conversation["id"],)).fetchall()
            export_data["conversations"].append(dict(conversation) | {"messages": [dict(row) for row in messages]})
    payload = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(payload, media_type="application/json", headers={"Content-Disposition": "attachment; filename=elliot-export.json"})


@app.get("/memory-settings")
async def get_memory_settings() -> dict:
    return {"memory_learning": MEMORY_LEARNING}


@app.patch("/memory-settings")
async def patch_memory_settings(enabled: bool = Query(...)) -> dict:
    global MEMORY_LEARNING
    MEMORY_LEARNING = enabled
    return {"memory_learning": MEMORY_LEARNING}


from .config import DOCUMENT_CHUNK_CHARS, DOCUMENT_CHUNK_OVERLAP, DOCUMENT_MAX_SIZE_MB
from .memory_store import extract_text_chunks


@app.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> dict:
    filename = Path(file.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".txt", ".md", ".docx"}:
        raise HTTPException(status_code=415, detail="Unsupported file type. Use PDF, TXT, MD, or DOCX.")
    content = await file.read()
    max_bytes = DOCUMENT_MAX_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds the {DOCUMENT_MAX_SIZE_MB} MB limit.")

    documents_dir = DATA_DIR / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    text = ""
    stored_temp = documents_dir / ("incoming_" + filename)
    stored_temp.write_bytes(content)
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(stored_temp).pages)
        elif suffix == ".docx":
            from docx import Document as DocxDocument
            text = "\n".join(paragraph.text for paragraph in DocxDocument(stored_temp).paragraphs)
        else:
            text = content.decode("utf-8", errors="replace")
    except Exception as exc:
        stored_temp.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Could not extract text: {exc}") from exc

    now = ist_now_text()
    with get_connection() as connection:
        cursor = connection.execute("INSERT INTO documents (filename, content_type, size_bytes, stored_path, created_at) VALUES (?, ?, ?, '', ?)",
                                    (filename, file.content_type or "application/octet-stream", len(content), now))
        document_id = cursor.lastrowid
        final_path = documents_dir / f"{document_id}_{filename}"
        shutil.move(stored_temp, final_path)
        connection.execute("UPDATE documents SET stored_path=? WHERE id=?", (str(final_path), document_id))

        chunks = extract_text_chunks(text, DOCUMENT_CHUNK_CHARS, DOCUMENT_CHUNK_OVERLAP)
        for chunk_index, chunk in enumerate(chunks):
            cursor_chunk = connection.execute("INSERT INTO document_chunks (document_id, chunk_index, content) VALUES (?, ?, ?)",
                                              (document_id, chunk_index, chunk))
            chunk_id = cursor_chunk.lastrowid
            index_item("document_chunks", f"document-{document_id}-chunk-{chunk_index}", chunk,
                       filename, now)

    return {"id": document_id, "filename": filename, "size_bytes": len(content), "chunks": len(chunks)}


@app.get("/documents")
async def list_documents() -> dict:
    with get_connection() as connection:
        rows = connection.execute("SELECT id, filename, content_type, size_bytes, created_at FROM documents ORDER BY id DESC").fetchall()
    return {"documents": [dict(row) for row in rows]}


@app.delete("/documents/{document_id}")
async def delete_document(document_id: int) -> dict:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        chunks = connection.execute("SELECT chunk_index FROM document_chunks WHERE document_id=?", (document_id,)).fetchall()
        connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
    Path(row["stored_path"]).unlink(missing_ok=True)
    for chunk in chunks:
        delete_item("document_chunks", f"document-{document_id}-chunk-{chunk['chunk_index']}")
    return {"deleted": True, "id": document_id, "chunks_deleted": len(chunks)}


@app.get("/profile")
@app.get("/api/profile")
async def profile_get() -> dict:
    profile = get_profile()
    profile["suggested_from_memories"] = profile_prefill_memories()
    return profile


@app.put("/profile")
@app.put("/api/profile")
async def profile_put(payload: dict) -> dict:
    try:
        return update_profile(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/profile/resume/parse")
async def parse_profile_resume(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(status_code=415, detail="Resume must be PDF or DOCX")
    content = await file.read()
    if len(content) > DOCUMENT_MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Resume exceeds the configured size limit")
    documents_dir = DATA_DIR / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    temp_path = documents_dir / ("resume_" + (file.filename or "resume"))
    temp_path.write_bytes(content)
    try:
        text = extract_resume_text(temp_path, suffix)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse resume: {exc}") from exc
    return {"filename": file.filename, "parsed_text": text[:50000], "saved": False, "message": "Review this text, then confirm it with PUT /profile using master_resume."}


@app.get("/jobs")
async def list_jobs(limit: int = 100, status: str | None = None, min_score: int = 0) -> dict:
    limit = max(1, min(limit, 500))
    query = "SELECT * FROM jobs WHERE match_score >= ?"
    params = [max(0, min(100, min_score))]
    if status:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY match_score DESC, created_at DESC LIMIT ?"
    params.append(limit)
    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()
    jobs = []
    for row in rows:
        item = dict(row)
        for field in ("gaps", "red_flags"):
            try:
                item[field] = json.loads(item[field])
            except (json.JSONDecodeError, TypeError):
                item[field] = []
        jobs.append(item)
    return {"jobs": jobs}


@app.get("/jobs/{job_id}")
async def get_job(job_id: int) -> dict:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    result = dict(row)
    for field in ("gaps", "red_flags"):
        try:
            result[field] = json.loads(result[field])
        except (json.JSONDecodeError, TypeError):
            result[field] = []
    return result


@app.post("/jobs/fetch")
async def fetch_jobs() -> dict:
    return await fetch_jobs_manual()


@app.post("/jobs/manual")
async def add_manual_job(payload: dict) -> dict:
    text = str(payload.get("text", "")).strip()
    url = str(payload.get("url", "")).strip()
    title = str(payload.get("title", "Untitled pasted job")).strip()
    company = str(payload.get("company", "Unknown")).strip()
    location = str(payload.get("location", "")).strip()
    if not text and not url:
        raise HTTPException(status_code=422, detail="Provide job text or a URL")
    if url:
        if "linkedin.com" in url.lower() or "naukri.com" in url.lower() or "indeed.com" in url.lower():
            raise HTTPException(status_code=422, detail="Automated access to this site is not supported. Paste the public job details instead.")
    now = ist_now_text()
    source_url = url or f"manual://{abs(hash(text))}"
    flags = scam_flags(title + " " + text)
    with get_connection() as connection:
        existing = connection.execute("SELECT id FROM jobs WHERE url=?", (source_url,)).fetchone()
        if existing:
            return {"job_id": existing["id"], "duplicate": True}
        cursor = connection.execute("""
            INSERT INTO jobs (external_id, source, title, company, location, remote, description, url, posted_at, salary,
            status, match_score, match_reason, gaps, red_flags, scam_risk, notes, created_at, updated_at)
            VALUES ('manual', 'Manual paste', ?, ?, ?, 0, ?, ?, ?, '', 'new', 0, '', '[]', ?, ?, '', ?, ?)
        """, (title, company, location, text, source_url, now, json.dumps(flags), 1 if flags else 0, now, now))
        job_id = cursor.lastrowid
    if text:
        await score_job(job_id)
    return {"job_id": job_id, "duplicate": False, "red_flags": flags}


@app.post("/jobs/{job_id}/score")
async def rescore_job(job_id: int) -> dict:
    try:
        return await score_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class JobStatusPatch(BaseModel):
    status: str
    note: str = ""
    contact_name: str = ""
    interview_at: str = ""


@app.patch("/jobs/{job_id}/status")
async def update_job_status(job_id: int, patch: JobStatusPatch) -> dict:
    allowed = {"new", "saved", "applied", "interview", "offer", "rejected", "ignored"}
    if patch.status not in allowed:
        raise HTTPException(status_code=422, detail="Invalid job status")
    now = ist_now_text()
    with get_connection() as connection:
        row = connection.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        connection.execute("""
            UPDATE jobs SET status=?, notes=?, contact_name=?, interview_at=?, updated_at=? WHERE id=?
        """, (patch.status, patch.note, patch.contact_name, patch.interview_at, now, job_id))
    reminders = []
    if patch.status == "applied":
        from datetime import timedelta
        due = datetime.now(IST) + timedelta(days=7)
        due_text = due.strftime("%Y-%m-%d %H:%M")
        with get_connection() as connection:
            connection.execute("INSERT INTO reminders (text, due_at, done, notified) VALUES (?, ?, 0, 0)",
                               (f"Follow up on job application #{job_id}", due_text))
        reminders.append(due_text)
    if patch.status == "interview" and patch.interview_at:
        with get_connection() as connection:
            connection.execute("INSERT INTO reminders (text, due_at, done, notified) VALUES (?, ?, 0, 0)",
                               (f"Interview for job #{job_id}", patch.interview_at))
        reminders.append(patch.interview_at)
    return {"job_id": job_id, "status": patch.status, "follow_up_reminders_created": reminders}


@app.post("/jobs/{job_id}/tailor-resume")
async def tailor_job_resume(job_id: int) -> dict:
    return await generate_tailored_resume(job_id)


@app.post("/jobs/{job_id}/cover-letter")
async def job_cover_letter(job_id: int, tone: str = Query("formal")) -> dict:
    return await write_cover_letter(job_id, tone)


@app.post("/jobs/{job_id}/answers")
async def job_answers(job_id: int) -> dict:
    return await application_answers(job_id)


@app.get("/job-drafts/{draft_id}/export/{file_format}")
async def export_job_draft(draft_id: int, file_format: str) -> FileResponse:
    if file_format not in {"docx", "pdf", "text"}:
        raise HTTPException(status_code=422, detail="Format must be docx, pdf, or text")
    if file_format == "text":
        with get_connection() as connection:
            row = connection.execute("SELECT content, draft_type FROM job_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found")
        return Response(row["content"], media_type="text/plain", headers={"Content-Disposition": f"attachment; filename=draft-{draft_id}-{row['draft_type']}.txt"})
    try:
        output = export_text_draft(draft_id, file_format)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(output, filename=output.name)


@app.get("/application-stats")
async def applications_stats() -> dict:
    return application_stats()


@app.get("/tracker/export")
async def export_tracker() -> Response:
    csv_text = tracker_csv()
    return Response(csv_text, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=elliot-job-tracker.csv"})
