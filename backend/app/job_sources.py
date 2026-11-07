import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import yaml

logger = logging.getLogger("elliot.jobs.sources")
IST = ZoneInfo("Asia/Kolkata")


class JobSource:
    name = "base"

    async def fetch(self, profile: dict) -> list[dict]:
        raise NotImplementedError


def normalize_job(source: str, external_id: str, title: str, company: str, location: str,
                  remote: bool, description: str, url: str, posted_at: str = "", salary: str = "") -> dict:
    return {
        "external_id": str(external_id or ""), "source": source, "title": title.strip(), "company": company.strip(),
        "location": location.strip(), "remote": bool(remote), "description": description.strip(), "url": url.strip(),
        "posted_at": posted_at.strip(), "salary": salary.strip(),
    }


class RemotiveSource(JobSource):
    name = "Remotive"

    async def fetch(self, profile: dict) -> list[dict]:
        query = profile.get("target_roles", [])
        search = str(query[0]) if query else "python developer"
        params = {"search": search, "limit": 50}
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": "ElliotJobHunter/1.0"}) as client:
            response = await client.get("https://remotive.com/api/remote-jobs", params=params)
            response.raise_for_status()
            payload = response.json()
        jobs = []
        for item in payload.get("jobs", []):
            jobs.append(normalize_job(self.name, item.get("id", ""), item.get("title", ""), item.get("company_name", ""),
                                      item.get("candidate_required_location", "Worldwide"), True,
                                      item.get("description", ""), item.get("url", ""), item.get("publication_date", ""),
                                      item.get("salary", "")))
        return jobs


class RemoteOKSource(JobSource):
    name = "RemoteOK"

    async def fetch(self, profile: dict) -> list[dict]:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": "ElliotJobHunter/1.0"}) as client:
            response = await client.get("https://remoteok.com/api")
            response.raise_for_status()
            payload = response.json()
        jobs = []
        target = " ".join(str(role).lower() for role in profile.get("target_roles", []))
        for item in payload:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            text = " ".join([str(item.get("position", "")), " ".join(item.get("tags", []) if isinstance(item.get("tags", []), list) else [])]).lower()
            if target and not any(role.lower().strip() in text for role in profile.get("target_roles", []) if role.strip()):
                continue
            jobs.append(normalize_job(self.name, item.get("id", ""), item.get("position", ""), item.get("company", ""),
                                      item.get("location", "Worldwide"), True, item.get("description", ""), item.get("url", ""),
                                      str(item.get("date", "")), item.get("salary", "")))
        return jobs


class AdzunaSource(JobSource):
    name = "Adzuna"

    async def fetch(self, profile: dict) -> list[dict]:
        app_id = os.getenv("ADZUNA_APP_ID", "")
        app_key = os.getenv("ADZUNA_APP_KEY", "")
        if not app_id or not app_key:
            return []
        roles = profile.get("target_roles", []) or ["software developer"]
        location = profile.get("preferred_locations", [])
        location_text = str(location[0]) if location else "India"
        jobs = []
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": "ElliotJobHunter/1.0"}) as client:
            for role in roles[:3]:
                params = {"app_id": app_id, "app_key": app_key, "what": role, "where": location_text, "results_per_page": 50}
                response = await client.get("https://api.adzuna.com/v1/api/jobs/in/search/1", params=params)
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("results", []):
                    company = item.get("company", {}).get("display_name", "") if isinstance(item.get("company", {}), dict) else ""
                    jobs.append(normalize_job(self.name, item.get("id", ""), item.get("title", ""), company,
                                              item.get("location", {}).get("display_name", "India") if isinstance(item.get("location", {}), dict) else "India",
                                              False, item.get("description", ""), item.get("redirect_url", ""),
                                              item.get("created", ""), str(item.get("salary_min", ""))))
        return jobs


class BoardSource(JobSource):
    def __init__(self):
        self.companies_path = Path(__file__).resolve().parents[1] / "companies.yaml"

    async def fetch(self, profile: dict) -> list[dict]:
        if not self.companies_path.exists():
            return []
        with self.companies_path.open("r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        jobs = []
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": "ElliotJobHunter/1.0"}) as client:
            for company in config.get("companies", []):
                if not company.get("enabled", False):
                    continue
                board = str(company.get("board", "")).lower()
                token = company.get("token", "").strip()
                name = company.get("name", token)
                if not token or board not in {"greenhouse", "lever"}:
                    continue
                if board == "greenhouse":
                    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
                else:
                    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    payload = response.json()
                    items = payload.get("jobs", []) if board == "greenhouse" else payload if isinstance(payload, list) else []
                    for item in items:
                        if board == "greenhouse":
                            item_url = item.get("absolute_url", "")
                            title = item.get("title", "")
                            location = item.get("location", {}).get("name", "") if isinstance(item.get("location", {}), dict) else ""
                            description = item.get("content", "")
                            external_id = str(item.get("id", ""))
                            posted_at = item.get("updated_at", "")
                        else:
                            item_url = item.get("hostedUrl", "")
                            title = item.get("text", "")
                            location = item.get("categories", {}).get("location", "") if isinstance(item.get("categories", {}), dict) else ""
                            description = item.get("descriptionPlain", "")
                            external_id = str(item.get("id", ""))
                            posted_at = item.get("createdAt", "")
                        jobs.append(normalize_job(board.title(), external_id, title, name, location, False, description, item_url, posted_at, ""))
                except Exception as exc:
                    logger.warning("Job board source failed company=%s board=%s error=%s", name, board, exc)
        return jobs


SOURCE_PLUGINS = [RemotiveSource(), RemoteOKSource(), AdzunaSource(), BoardSource()]
