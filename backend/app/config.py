import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

AI_PROVIDER = os.getenv("AI_PROVIDER", "ollama").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
MODEL = os.getenv("MODEL", ANTHROPIC_MODEL)
NEWS_MODEL = os.getenv("NEWS_MODEL", "claude-3-5-haiku-latest")
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "claude-3-5-haiku-latest")
MEMORY_LEARNING = os.getenv("MEMORY_LEARNING", "on").strip().lower() in {"1", "true", "on", "yes"}
MEMORY_SUMMARY_TURNS = max(10, int(os.getenv("MEMORY_SUMMARY_TURNS", "20")))
DOCUMENT_MAX_SIZE_MB = max(1, int(os.getenv("DOCUMENT_MAX_SIZE_MB", "20")))
DOCUMENT_CHUNK_CHARS = max(500, int(os.getenv("DOCUMENT_CHUNK_CHARS", "2000")))
DOCUMENT_CHUNK_OVERLAP = max(0, int(os.getenv("DOCUMENT_CHUNK_OVERLAP", "200")))
MATCH_MODEL = os.getenv("MATCH_MODEL", "claude-3-5-haiku-latest")
JOB_AI_DAILY_LIMIT = max(0, int(os.getenv("JOB_AI_DAILY_LIMIT", "100")))
JOB_FETCH_INTERVAL_HOURS = max(1, int(os.getenv("JOB_FETCH_INTERVAL_HOURS", "6")))
MAX_JOB_AGE_DAYS = max(1, int(os.getenv("MAX_JOB_AGE_DAYS", "30")))
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
NEWS_AI_DAILY_LIMIT = int(os.getenv("NEWS_AI_DAILY_LIMIT", "100"))
NEWS_POLL_INTERVAL_SECONDS = int(os.getenv("NEWS_POLL_INTERVAL_SECONDS", "60"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if origin.strip()]

SYSTEM_PROMPT = """You are Elliot, a personal AI assistant.
Be concise, calm, technically accurate, and proactive.
Convert relative times into YYYY-MM-DD HH:MM in Asia/Kolkata (IST).
Cite source URLs when using web search or webpage results.
Never invent search results. Ask for confirmation before deleting anything.
Use plain language. Do not claim actions occurred unless a tool result confirms them.
Ask a short clarifying question only when the request genuinely needs more information.
"""
