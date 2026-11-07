import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import calendar
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx
import yaml
from anthropic import AsyncAnthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import ANTHROPIC_API_KEY, NEWS_AI_DAILY_LIMIT, NEWS_MODEL, NEWS_POLL_INTERVAL_SECONDS
from .db import get_connection

logger = logging.getLogger("elliot.news")
IST = ZoneInfo("Asia/Kolkata")
USER_AGENT = "ElliotNews/1.0 (+personal assistant; RSS only)"
FEEDS_PATH = Path(__file__).resolve().parents[1] / "feeds.yaml"

clients: set = set()
feed_cache: dict[str, dict[str, str]] = {}
feed_failures: dict[str, dict] = {}
news_queue: asyncio.Queue = asyncio.Queue()
worker_task: asyncio.Task | None = None
scheduler: AsyncIOScheduler | None = None
daily_processed = {"date": datetime.now(IST).date().isoformat(), "count": 0}


def load_feeds() -> list[dict]:
    with FEEDS_PATH.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    return [source for source in data.get("sources", []) if source.get("enabled", True)]


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), ""))


def normalized_title(title: str) -> str:
    return " ".join(title.lower().split())


def title_is_duplicate(title: str, connection) -> bool:
    cutoff = (datetime.now(IST) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = connection.execute(
        "SELECT title FROM articles WHERE published_at >= ?", (cutoff,)
    ).fetchall()
    candidate = normalized_title(title)
    return any(SequenceMatcher(None, candidate, normalized_title(row["title"])).ratio() >= 0.82 for row in rows)


def entry_description(entry) -> str:
    return getattr(entry, "summary", "") or getattr(entry, "description", "")


def entry_published(entry) -> str:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        timestamp = calendar.timegm(parsed)
        return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(IST).isoformat(timespec="seconds")
    return datetime.now(IST).isoformat(timespec="seconds")


async def process_news_batch(articles: list[dict]) -> None:
    global daily_processed
    today = datetime.now(IST).date().isoformat()
    if daily_processed["date"] != today:
        daily_processed = {"date": today, "count": 0}

    remaining = NEWS_AI_DAILY_LIMIT - daily_processed["count"]
    if remaining <= 0 or not ANTHROPIC_API_KEY:
        logger.info("News AI processing skipped: cap=%s remaining=%s key=%s", NEWS_AI_DAILY_LIMIT, remaining, bool(ANTHROPIC_API_KEY))
        return

    batch = articles[:remaining]
    payload = [{"id": article["id"], "source": article["source"], "title": article["title"], "description": article["description"][:1200]} for article in batch]
    prompt = """Analyze only these RSS title and description fields. Return strict JSON as an array with one object per article:
[{"id": <number>, "summary": "1-2 sentence summary", "topics": ["topic", "topic"], "importance": <1-5>}]
Do not invent details. Summary must be based only on supplied feed text. Use 1-3 short topic tags. Importance: 5 major breaking/critical, 4 high, 3 notable, 2 routine, 1 minor.
"""

    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=NEWS_MODEL,
            max_tokens=1500,
            system=prompt,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        results = json.loads(text)
        if not isinstance(results, list):
            raise ValueError("AI result is not a JSON array")
    except Exception as exc:
        logger.exception("News AI batch failed")
        return

    valid_ids = {article["id"] for article in batch}
    with get_connection() as connection:
        for result in results:
            article_id = result.get("id") if isinstance(result, dict) else None
            if article_id not in valid_ids:
                continue
            summary = str(result.get("summary", "")).strip()[:1000]
            topics = result.get("topics", [])
            importance = result.get("importance", 1)
            if not isinstance(topics, list):
                topics = []
            topics = [str(topic)[:50] for topic in topics[:3]]
            if isinstance(importance, bool) or not isinstance(importance, int):
                importance = 1
            importance = max(1, min(5, importance))
            connection.execute(
                "UPDATE articles SET summary=?, topics=?, importance=? WHERE id=?",
                (summary, json.dumps(topics), importance, article_id),
            )
            row = connection.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            processed = dict(row)

            settings = connection.execute("SELECT topics, min_importance FROM news_settings WHERE id=1").fetchone()
            alert_topics = json.loads(settings["topics"] if settings else "[]")
            alert_topics = [topic.lower() for topic in alert_topics]
            haystack = " ".join([processed["title"] or "", processed["summary"] or "", processed["topics"] or ""]).lower()
            is_alert = processed["importance"] >= (settings["min_importance"] if settings else 4) or any(topic in haystack for topic in alert_topics)
            if is_alert:
                connection.execute("UPDATE articles SET alerted=1 WHERE id=?", (article_id,))
                processed["alerted"] = 1
            connection.commit()

    daily_processed["count"] += len(batch)

    with get_connection() as connection:
        for article in batch:
            row = connection.execute("SELECT * FROM articles WHERE id=?", (article["id"],)).fetchone()
            if row:
                await broadcast_article(dict(row))


async def news_worker() -> None:
    batch = []
    while True:
        try:
            article = await asyncio.wait_for(news_queue.get(), timeout=2.0)
            batch.append(article)
            if len(batch) >= 5:
                await process_news_batch(batch)
                batch = []
        except asyncio.TimeoutError:
            if batch:
                await process_news_batch(batch)
                batch = []


async def broadcast_article(article: dict) -> None:
    if article.get("topics"):
        try:
            article["topics"] = json.loads(article["topics"])
        except (json.JSONDecodeError, TypeError):
            article["topics"] = []
    dead = []
    for websocket in clients.copy():
        try:
            await websocket.send_json({"type": "news", "article": article})
        except Exception:
            dead.append(websocket)
    for websocket in dead:
        clients.discard(websocket)


async def poll_feed(source: dict) -> tuple[int, int, str | None]:
    rss_url = source["rss"]
    name = source["name"]
    now = datetime.now(IST)
    backoff = feed_failures.get(rss_url)
    if backoff and now < backoff["retry_at"]:
        return 0, 0, "backoff"

    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5"}
    cached = feed_cache.get(rss_url, {})
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = await client.get(rss_url, headers=headers)
            if response.status_code == 304:
                feed_failures.pop(rss_url, None)
                return 0, 0, None
            response.raise_for_status()
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"Invalid RSS feed: {feed.get('bozo_exception', 'unknown parse error')}")
        feed_cache[rss_url] = {
            "etag": response.headers.get("ETag", ""),
            "last_modified": response.headers.get("Last-Modified", ""),
        }
        feed_failures.pop(rss_url, None)
    except Exception as exc:
        failures = feed_failures.get(rss_url, {"count": 0})
        count = failures["count"] + 1
        retry_at = now + timedelta(seconds=min(900, NEWS_POLL_INTERVAL_SECONDS * (2 ** count)))
        feed_failures[rss_url] = {"count": count, "retry_at": retry_at}
        logger.warning("Feed failed name=%s url=%s count=%s error=%s", name, rss_url, count, exc)
        return 0, 0, str(exc)

    new_count = 0
    fetched_at = datetime.now(IST).isoformat(timespec="seconds")
    with get_connection() as connection:
        for entry in feed.entries:
            title = str(getattr(entry, "title", "")).strip()
            url = normalize_url(str(getattr(entry, "link", "")).strip())
            if not title or not url:
                continue
            existing = connection.execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()
            if existing or title_is_duplicate(title, connection):
                continue
            cursor = connection.execute("""
                INSERT INTO articles (source, category, title, url, published_at, fetched_at, summary, topics, importance, alerted)
                VALUES (?, ?, ?, ?, ?, ?, '', '[]', 1, 0)
            """, (name, source.get("category", "national"), title, url,
                  entry_published(entry), fetched_at))
            article_id = cursor.lastrowid
            new_count += 1
            await news_queue.put({"id": article_id, "source": name, "title": title, "description": entry_description(entry)})
    return len(feed.entries), new_count, None


async def poll_all_feeds() -> None:
    sources = load_feeds()
    results = await asyncio.gather(*(poll_feed(source) for source in sources), return_exceptions=True)
    checked = len(sources)
    new_articles = sum(result[1] for result in results if isinstance(result, tuple))
    errors = sum(1 for result in results if not isinstance(result, tuple) or result[2] is not None)
    logger.info("News poll cycle feeds_checked=%s new_articles=%s errors=%s", checked, new_articles, errors)


async def start_news_engine(app=None) -> None:
    global worker_task, scheduler
    worker_task = asyncio.create_task(news_worker())
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(poll_all_feeds, "interval", seconds=max(30, NEWS_POLL_INTERVAL_SECONDS), id="news-poller", max_instances=1)
    scheduler.start()
    await poll_all_feeds()


async def stop_news_engine() -> None:
    global worker_task, scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        worker_task = None
