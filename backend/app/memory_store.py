import logging
import os
import re
from pathlib import Path

from .db import DATA_DIR, get_connection, ist_now_text

logger = logging.getLogger("elliot.memory")
CHROMA_DIR = Path(os.getenv("ELLIOT_CHROMA_DIR", str(DATA_DIR / "chroma"))).resolve()
RECALL_THRESHOLD = float(os.getenv("MEMORY_RECALL_THRESHOLD", "0.25"))
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        import chromadb
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _CLIENT


def collection(type_name: str):
    return _client().get_or_create_collection(
        name=f"elliot_{type_name}",
        metadata={"hnsw:space": "cosine"},
    )


def index_item(type_name: str, item_id: str, text: str, source: str = "", date: str = "") -> None:
    if not text.strip():
        return
    try:
        collection(type_name).upsert(
            ids=[item_id], documents=[text],
            metadatas=[{"type": type_name, "source": source, "date": date}],
        )
        logger.info("Indexed memory type=%s id=%s", type_name, item_id)
    except Exception as exc:
        logger.warning("Memory index skipped type=%s id=%s error=%s", type_name, item_id, exc)


def delete_item(type_name: str, item_id: str) -> None:
    try:
        collection(type_name).delete(ids=[item_id])
    except Exception as exc:
        logger.warning("Memory delete failed type=%s id=%s error=%s", type_name, item_id, exc)


def recall(query: str, types: list[str] | None = None, top_k: int = 8) -> list[dict]:
    if not query.strip():
        return []
    types = types or ["notes", "memories", "document_chunks", "conversation_summaries"]
    matches = []
    for type_name in types:
        try:
            result = collection(type_name).query(query_texts=[query], n_results=max(1, min(top_k, 20)))
            documents = result.get("documents", [[]])[0]
            metadatas = result.get("metadatas", [[]])[0]
            distances = result.get("distances", [[]])[0]
            ids = result.get("ids", [[]])[0]
            for text, metadata, distance, item_id in zip(documents, metadatas, distances, ids):
                similarity = 1.0 - float(distance)
                if similarity >= RECALL_THRESHOLD:
                    metadata = metadata or {}
                    matches.append({"text": text, "type": type_name, "source": metadata.get("source", ""),
                                    "date": metadata.get("date", ""), "similarity": round(similarity, 4), "id": item_id})
        except Exception as exc:
            logger.warning("Memory retrieval failed type=%s error=%s", type_name, exc)
    matches.sort(key=lambda item: item["similarity"], reverse=True)
    return matches[:top_k]


def sync_existing_notes() -> int:
    count = 0
    with get_connection() as connection:
        rows = connection.execute("SELECT id, title, content, created_at, updated_at FROM notes").fetchall()
    for row in rows:
        text = f"{row['title']}\n{row['content']}".strip()
        index_item("notes", f"note-{row['id']}", text, f"note:{row['id']}", row["updated_at"] or row["created_at"])
        count += 1
    logger.info("One-time note memory indexing complete count=%s", count)
    return count


SENSITIVE_PATTERNS = {
    "password": re.compile(r"(?i)\b(?:password|passwd|passcode)\b"),
    "api_key": re.compile(r"(?i)(api[_ -]?key|secret[_ -]?key|access[_ -]?token|bearer\s+[A-Za-z0-9])"),
    "card": re.compile(r"(?<!\d)(?:\d[ -]*){13,19}(?!\d)"),
    "bank": re.compile(r"(?i)(bank account|account number|ifsc|aadhar|aadhaar|pan\s*number|social security|ssn)"),
}


def contains_sensitive_data(text: str) -> str | None:
    for name, pattern in SENSITIVE_PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def extract_text_chunks(text: str, max_chars: int = 2000, overlap: int = 200) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + max_chars])
        if start + max_chars >= len(text):
            break
        start += max_chars - overlap
    return chunks
