import json
import logging
from pathlib import Path

from .db import get_connection, ist_now_text
from .memory_store import contains_sensitive_data

logger = logging.getLogger("elliot.profile")

PROFILE_DEFAULTS = {
    "full_name": "", "email": "", "phone": "", "location": "", "target_roles": [], "skills": [],
    "experience": [], "education": [], "certifications": [], "projects": [], "preferred_locations": [],
    "remote_preference": "any", "salary_min": 0, "salary_max": 0, "notice_period": "", "languages": [],
    "master_resume": "",
}

JSON_FIELDS = {"target_roles", "skills", "experience", "education", "certifications", "projects", "preferred_locations", "languages"}


def get_profile() -> dict:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM profile WHERE id=1").fetchone()
    if not row:
        return dict(PROFILE_DEFAULTS)
    result = dict(row)
    for field in JSON_FIELDS:
        try:
            result[field] = json.loads(result[field] or "[]")
        except json.JSONDecodeError:
            result[field] = []
    result.pop("id", None)
    return result


def update_profile(payload: dict) -> dict:
    current = get_profile()
    clean = dict(current)
    for field, value in payload.items():
        if field == "id":
            continue
        if field in JSON_FIELDS:
            if not isinstance(value, list):
                raise ValueError(f"{field} must be a JSON list")
            clean[field] = value
        elif field in {"salary_min", "salary_max"}:
            clean[field] = max(0, int(value or 0))
        else:
            clean[field] = str(value or "")[:10000]

    if clean["remote_preference"] not in {"any", "remote", "hybrid", "onsite"}:
        clean["remote_preference"] = "any"
    sensitive = contains_sensitive_data(clean["full_name"] + " " + clean.get("email", "") + " " + clean.get("phone", "") + " " + clean["master_resume"])
    if sensitive:
        logger.warning("Profile rejected because it contains sensitive field type=%s", sensitive)
        raise ValueError("Passwords, API keys, bank/card details, government IDs, and health details are not allowed in profiles")

    now = ist_now_text()
    clean["updated_at"] = now
    with get_connection() as connection:
        connection.execute("""
            UPDATE profile SET full_name=?, email=?, phone=?, location=?, target_roles=?, skills=?, experience=?,
            education=?, certifications=?, projects=?, preferred_locations=?, remote_preference=?, salary_min=?, salary_max=?,
            notice_period=?, languages=?, master_resume=?, updated_at=? WHERE id=1
        """, (
            clean["full_name"], clean["email"], clean["phone"], clean["location"],
            json.dumps(clean["target_roles"]), json.dumps(clean["skills"]), json.dumps(clean["experience"]),
            json.dumps(clean["education"]), json.dumps(clean["certifications"]), json.dumps(clean["projects"]),
            json.dumps(clean["preferred_locations"]), clean["remote_preference"], clean["salary_min"], clean["salary_max"],
            clean["notice_period"], json.dumps(clean["languages"]), clean["master_resume"][:50000], now,
        ))
    logger.info("Updated job profile")
    return get_profile()


def profile_prefill_memories() -> dict:
    with get_connection() as connection:
        rows = connection.execute("SELECT content, category FROM memories ORDER BY updated_at DESC LIMIT 30").fetchall()
    return {"memories": [dict(row) for row in rows]}
