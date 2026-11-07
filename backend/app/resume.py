import difflib
import json
import logging
from pathlib import Path

from anthropic import AsyncAnthropic
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_LEFT

from .config import ANTHROPIC_API_KEY, MATCH_MODEL
from .db import DATA_DIR, get_connection, ist_now_text
from .profile import get_profile

logger = logging.getLogger("elliot.jobs.resume")
RESUMES_DIR = DATA_DIR / "resumes"


def extract_resume_text(path: Path, suffix: str) -> str:
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix == ".docx":
        document = Document(path)
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    raise ValueError("Resume must be PDF or DOCX")


async def generate_tailored_resume(job_id: int) -> dict:
    profile = get_profile()
    with get_connection() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("Job not found")
    master = profile.get("master_resume", "").strip() or json.dumps(profile, ensure_ascii=False, indent=2)
    prompt = """Tailor only from the supplied profile/master resume and job description.
Never add facts, degrees, dates, employers, skills, numbers, or experience not present in the master resume.
Return strict JSON: {"resume_text":"plain ATS-friendly text","gaps":["missing skill or requirement"]}.
If a skill is missing, put it under gaps only and never add it to resume_text.
"""
    if not ANTHROPIC_API_KEY:
        return save_draft(job_id, "resume", "AI resume tailoring requires ANTHROPIC_API_KEY. Review the master resume manually.", "No change: API key unavailable.", "text")
    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(model=MATCH_MODEL, max_tokens=1800, system=prompt,
            messages=[{"role": "user", "content": json.dumps({"master": master, "job": dict(job)}, ensure_ascii=False)}])
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].strip().removeprefix("json").strip()
        payload = json.loads(text)
        resume_text = str(payload.get("resume_text", "")).strip()
        gaps = payload.get("gaps", []) if isinstance(payload.get("gaps", []), list) else []
    except Exception as exc:
        logger.exception("Resume tailoring failed job_id=%s", job_id)
        return save_draft(job_id, "resume", "Could not tailor resume. Review the master resume manually.", "Generation failed.", "text")

    diff = "\n".join(difflib.unified_diff(master.splitlines(), resume_text.splitlines(), fromfile="master", tofile="tailored", lineterm=""))
    return save_draft(job_id, "resume", resume_text, diff or "No textual changes detected.", "text")


def save_draft(job_id: int, draft_type: str, content: str, diff: str = "", format_name: str = "text") -> dict:
    now = ist_now_text()
    with get_connection() as connection:
        cursor = connection.execute("""
            INSERT INTO job_drafts (job_id, draft_type, content, content_diff, format, path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '', ?, ?)
        """, (job_id, draft_type, content, diff, format_name, now, now))
        draft_id = cursor.lastrowid
    return {"id": draft_id, "job_id": job_id, "draft_type": draft_type, "content": content, "diff": diff}


async def write_cover_letter(job_id: int, tone: str = "formal") -> dict:
    profile = get_profile()
    with get_connection() as connection:
        job_row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job_row:
        raise ValueError("Job not found")
    job = dict(job_row)
    tone = tone if tone in {"formal", "friendly", "concise"} else "formal"
    prompt = f"""Write a {tone} cover letter of 200-250 words for this real candidate and posting.
Use only facts in the profile and job. Never invent achievements, employers, dates, degrees, skills, or numbers.
Do not use clichés or generic filler. Return only the letter.
"""
    if not ANTHROPIC_API_KEY:
        return save_draft(job_id, "cover_letter", "AI cover letter generation requires ANTHROPIC_API_KEY. I did not invent any application details.")
    try:
        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(model=MATCH_MODEL, max_tokens=900, system=prompt,
            messages=[{"role": "user", "content": json.dumps({"profile": profile, "job": job}, ensure_ascii=False)}])
        letter = "".join(block.text for block in response.content if block.type == "text").strip()
    except Exception as exc:
        logger.exception("Cover letter generation failed job_id=%s", job_id)
        letter = "Could not generate a cover letter. Review the profile and job manually."
    return save_draft(job_id, "cover_letter", letter, format_name="text")


async def application_answers(job_id: int) -> dict:
    profile = get_profile()
    with get_connection() as connection:
        job_row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    job = dict(job_row) if job_row else {}
    answers = {
        "why_company": "Use only the company's publicly stated mission and the verified job description; review before submitting.",
        "why_role": "Use only my profile and the real requirements listed in the job.",
        "notice_period": profile.get("notice_period", "Not specified in profile"),
        "expected_salary": f"INR {profile.get('salary_min', 0):,} to INR {profile.get('salary_max', 0):,}" if profile.get("salary_max") else "Not specified",
    }
    return save_draft(job_id, "answers", json.dumps(answers, ensure_ascii=False), format_name="json")


def export_text_draft(draft_id: int, file_format: str = "docx") -> Path:
    with get_connection() as connection:
        draft = connection.execute("SELECT * FROM job_drafts WHERE id=?", (draft_id,)).fetchone()
    if not draft:
        raise ValueError("Draft not found")
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    base = RESUMES_DIR / f"draft-{draft_id}-{draft['draft_type']}"
    if file_format == "docx":
        document = Document()
        for line in str(draft["content"]).splitlines():
            document.add_paragraph(line)
        output = base.with_suffix(".docx")
        document.save(output)
    elif file_format == "pdf":
        output = base.with_suffix(".pdf")
        styles = getSampleStyleSheet()
        styles["Normal"].alignment = TA_LEFT
        pdf = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=inch * 0.65, leftMargin=inch * 0.65,
                                topMargin=inch * 0.65, bottomMargin=inch * 0.65)
        story = [Spacer(1, 0.1 * inch)] + [Paragraph(line.replace("&", "&amp;"), styles["Normal"])
                                          for line in str(draft["content"]).splitlines() or [""]]
        pdf.build(story)
    else:
        raise ValueError("Format must be docx or pdf")
    with get_connection() as connection:
        connection.execute("UPDATE job_drafts SET path=? WHERE id=?", (str(output), draft_id))
    return output
