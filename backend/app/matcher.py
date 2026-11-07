import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from .config import ANTHROPIC_API_KEY, MATCH_MODEL, JOB_AI_DAILY_LIMIT
from .db import get_connection, ist_now_text
from .memory_store import recall

logger = logging.getLogger("elliot.jobs.matcher")
IST = ZoneInfo("Asia/Kolkata")
daily_cost = {"date": datetime.now(IST).date().isoformat(), "count": 0}

SCAM_PATTERNS = [r"registration fee", r"pay.*(fee|deposit|security)", r"whatsapp.*payment", r"send.*(otp|password)", r"unpaid.*trial", r"processing fee", r"guaranteed income"]


def prefilter_score(profile: dict, job_description: str) -> float:
    profile_text = " ".join([
        " ".join(profile.get("target_roles", [])), " ".join(profile.get("skills", []) if isinstance(profile.get("skills", []), list) else []),
        " ".join(profile.get("location", "") .split()), job_description[:3000],
    ])
    matches = recall(profile_text[:1500], types=["notes", "memories"], top_k=1)
    return 1.0


def scam_flags(text: str) -> list[str]:
    found = []
    lowered = text.lower()
    for pattern in SCAM_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            found.append(pattern.replace(".*", " "))
    return sorted(set(found))


async def score_job(job_id: int) -> dict:
    global daily_cost
    today = datetime.now(IST).date().isoformat()
    if daily_cost["date"] != today:
        daily_cost = {"date": today, "count": 0}
    with get_connection() as connection:
        job_row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        profile_row = connection.execute("SELECT * FROM profile WHERE id=1").fetchone()
    if not job_row:
        raise ValueError("Job not found")

    job = dict(job_row)
    profile = dict(profile_row) if profile_row else {}
    for field in ("target_roles", "skills", "experience", "education", "certifications", "projects", "preferred_locations", "languages"):
        try:
            profile[field] = json.loads(profile.get(field, "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            profile[field] = []

    scam = scam_flags(job["title"] + " " + job["description"])
    if daily_cost["count"] >= JOB_AI_DAILY_LIMIT or not ANTHROPIC_API_KEY:
        result = {"score": 0, "matching_skills": [], "missing_skills": [], "seniority_fit": "not scored",
                  "location_fit": "not scored", "salary_fit": "not scored", "red_flags": scam,
                  "explanation": "AI matching is unavailable until the daily limit resets or an API key is configured.", "scam_risk": bool(scam)}
    else:
        prompt = """Score this job only from the supplied job text and profile. Never invent skills or requirements.
Return strict JSON with score (0-100), matching_skills array, missing_skills array, seniority_fit string,
location_fit string, salary_fit string, red_flags array, and explanation (2-3 sentences).
Flag vague role, unpaid trial, payment requests, unrealistic requirements, and other scam indicators.
Use only evidence in both inputs.
"""
        try:
            client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            response = await client.messages.create(
                model=MATCH_MODEL, max_tokens=700, system=prompt,
                messages=[{"role": "user", "content": json.dumps({"profile": profile, "job": job}, ensure_ascii=False)}],
            )
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            if text.startswith("```"):
                text = text.split("```", 2)[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            result = json.loads(text)
            if not isinstance(result, dict) or not isinstance(result.get("score"), (int, float)):
                raise ValueError("Invalid match JSON")
        except Exception as exc:
            logger.exception("Job match failed job_id=%s", job_id)
            result = {"score": 0, "matching_skills": [], "missing_skills": [], "seniority_fit": "unknown",
                      "location_fit": "unknown", "salary_fit": "unknown", "red_flags": scam,
                      "explanation": "The match could not be scored; review the job description manually.", "scam_risk": bool(scam)}
        daily_cost["count"] += 1

    score = max(0, min(100, int(result.get("score", 0))))
    gaps = result.get("missing_skills", []) if isinstance(result.get("missing_skills", []), list) else []
    red_flags = sorted(set((result.get("red_flags", []) if isinstance(result.get("red_flags", []), list) else []) + scam))
    explanation = str(result.get("explanation", "")).strip()
    with get_connection() as connection:
        connection.execute("""
            UPDATE jobs SET match_score=?, match_reason=?, gaps=?, red_flags=?, scam_risk=?, updated_at=? WHERE id=?
        """, (score, explanation, json.dumps(gaps[:20]), json.dumps(red_flags[:20]), 1 if red_flags else 0,
              ist_now_text(), job_id))
    logger.info("Scored job id=%s score=%s cost_calls=%s", job_id, score, daily_cost["count"])
    return {"job_id": job_id, "score": score, "matching_skills": result.get("matching_skills", []),
            "missing_skills": gaps, "seniority_fit": result.get("seniority_fit", "unknown"),
            "location_fit": result.get("location_fit", "unknown"), "salary_fit": result.get("salary_fit", "unknown"),
            "red_flags": red_flags, "explanation": explanation, "scam_risk": bool(red_flags)}
