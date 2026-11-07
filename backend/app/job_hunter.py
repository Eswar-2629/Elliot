import asyncio
import csv
import json
import logging
import re
from datetime import datetime, timedelta
from io import StringIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import JOB_FETCH_INTERVAL_HOURS, MAX_JOB_AGE_DAYS
from .db import get_connection, ist_now_text
from .job_sources import SOURCE_PLUGINS
from .profile import get_profile
from .memory_store import collection, index_item
from .matcher import score_job

logger = logging.getLogger("elliot.jobs")
IST = ZoneInfo("Asia/Kolkata")
scheduler: AsyncIOScheduler | None = None


def normalized_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return (parts.scheme.lower() + "://" + parts.netloc.lower() + parts.path.rstrip("/")).strip()


async def fetch_jobs_manual() -> dict:
    profile = get_profile()
    stats = {"sources_checked": len(SOURCE_PLUGINS), "jobs_found": 0, "new": 0, "duplicates": 0, "errors": 0, "scoring_cost": 0}
    cutoff = datetime.now(IST) - timedelta(days=MAX_JOB_AGE_DAYS)

    results = await asyncio.gather(*(source.fetch(profile) for source in SOURCE_PLUGINS), return_exceptions=True)
    for source, result in zip(SOURCE_PLUGINS, results):
        if isinstance(result, Exception):
            stats["errors"] += 1
            logger.warning("Job source failed source=%s error=%s", source.name, result)
            continue
        stats["jobs_found"] += len(result)
        for job in result:
            url = normalized_url(job["url"])
            if not url:
                continue
            if job["company"] and profile.get("location"):
                roles = profile.get("target_roles", [])
                text = f"{job['title']} {job['description']}".lower()
                if roles and not any(str(role).lower().strip() in text for role in roles):
                    continue
            posted = job.get("posted_at", "")
            if posted:
                try:
                    parsed = datetime.fromisoformat(posted.replace("Z", "+00:00"))
                    if parsed.tzinfo:
                        parsed = parsed.astimezone(IST).replace(tzinfo=None)
                    if parsed < cutoff.replace(tzinfo=None):
                        continue
                except ValueError:
                    pass

            now = ist_now_text()
            with get_connection() as connection:
                existing_url = connection.execute("SELECT id FROM jobs WHERE url=?", (url,)).fetchone()
                duplicate = connection.execute("""
                    SELECT id FROM jobs WHERE lower(company)=lower(?) AND lower(title)=lower(?) AND lower(location)=lower(?)
                """, (job["company"], job["title"], job["location"])).fetchone()
                if existing_url or duplicate:
                    stats["duplicates"] += 1
                    continue
                cursor = connection.execute("""
                    INSERT INTO jobs (external_id, source, title, company, location, remote, description, url, posted_at, salary,
                    status, match_score, match_reason, gaps, red_flags, scam_risk, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 0, '', '[]', '[]', 0, ?, ?)
                """, (job["external_id"], job["source"], job["title"], job["company"], job["location"], 1 if job["remote"] else 0,
                      job["description"][:50000], url, job["posted_at"], job["salary"], now, now))
                job_id = cursor.lastrowid
            stats["new"] += 1
            index_item("jobs", f"job-{job_id}", f"{job['title']} {job['company']} {job['description'][:4000]}", job["source"], job["posted_at"] or now)

    # Rank candidates with the local Stage 4 embedding model, then score only the top matches.
    profile_text = " ".join([
        " ".join(profile.get("target_roles", [])), " ".join(profile.get("skills", []) if isinstance(profile.get("skills", []), list) else []),
        profile.get("location", ""), "remote" if profile.get("remote_preference") == "remote" else "",
    ])
    candidate_ids = []
    try:
        result = collection("jobs").query(query_texts=[profile_text], n_results=10)
        for item_id in result.get("ids", [[]])[0]:
            if item_id.startswith("job-"):
                candidate_ids.append(int(item_id.split("-", 1)[1]))
    except Exception as exc:
        logger.warning("Embedding job prefilter failed; ranking by newest: %s", exc)
    if not candidate_ids:
        with get_connection() as connection:
            rows = connection.execute("SELECT id FROM jobs WHERE status='new' ORDER BY created_at DESC LIMIT 10").fetchall()
        candidate_ids = [row["id"] for row in rows]

    for job_id in candidate_ids:
        try:
            await score_job(job_id)
            stats["scoring_cost"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("Job scoring failed job_id=%s error=%s", row["id"], exc)

    logger.info("Job fetch sources_checked=%s jobs_found=%s new=%s duplicates=%s errors=%s scoring_cost=%s",
                stats["sources_checked"], stats["jobs_found"], stats["new"], stats["duplicates"], stats["errors"], stats["scoring_cost"])
    return stats


async def start_job_scheduler(app=None) -> None:
    global scheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(fetch_jobs_manual, "interval", hours=JOB_FETCH_INTERVAL_HOURS, id="job-fetcher", max_instances=1)
    scheduler.start()


async def stop_job_scheduler() -> None:
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None


def application_stats() -> dict:
    with get_connection() as connection:
        rows = connection.execute("SELECT status, interview_at FROM jobs").fetchall()
        applied = sum(1 for row in rows if row["status"] in {"applied", "interview", "offer", "rejected"})
        interviews = sum(1 for row in rows if row["status"] in {"interview", "offer", "rejected"})
        offer = sum(1 for row in rows if row["status"] == "offer")
        response_rate = round((interviews / applied * 100), 1) if applied else 0
    return {"applications": applied, "interviews": interviews, "offers": offer, "response_rate_percent": response_rate}


def tracker_csv() -> str:
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["id", "title", "company", "location", "url", "status", "match_score", "notes", "contact_name", "interview_at"])
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row[field] if row[field] is not None else "" for field in writer.fieldnames})
    return output.getvalue()
