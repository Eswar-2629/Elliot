# Elliot

A personal AI assistant built in stages.

## Stage 1: Project Structure and Basic Chat

Stage 1 provides a FastAPI chat API and a React chat interface. The AI provider is selected in `backend/.env`; Claude and Ollama are both supported.

### Folder Structure

```text
Elliot/
├── backend/
│   ├── .env.example
│   ├── requirements.txt
│   └── app/
│       ├── __init__.py
│       ├── config.py
│       └── main.py
├── frontend/
│   ├── index.html
│   ├── package.json
│   └── src/
│       ├── App.jsx
│       ├── main.jsx
│       └── styles.css
├── .gitignore
└── README.md
```

### Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- One AI provider:
  - **Claude:** an Anthropic API key
  - **Ollama (free):** Ollama installed locally, plus one downloaded chat model

No API key is needed for the Ollama route.

### 1. Configure the Backend

```bash
cd /workspaces/Elliot/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

For free local AI, keep:

```env
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:3b
```

Install the model in a separate terminal:

```bash
ollama serve
ollama pull qwen2.5:3b
```

For Claude, edit `backend/.env`:

```env
AI_PROVIDER=claude
ANTHROPIC_API_KEY=your_real_key_here
MODEL=claude-3-5-sonnet-latest
```

Create the key at Anthropic Console. Never commit `.env`.

### 2. Start the Backend

From `/workspaces/Elliot/backend`:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Check:

- API health: http://localhost:8000/api/health
- Interactive API docs: http://localhost:8000/docs

### 3. Start the Frontend

Open another terminal:

```bash
cd /workspaces/Elliot/frontend
npm install
npm run dev
```

Open http://localhost:5173.

The UI sends the latest user message to `POST /api/chat`. Stage 2 keeps the latest 20 plain-text turns in backend memory.

### Test Stage 1

1. Open http://localhost:5173.
2. Confirm the status indicator shows your selected provider and model.
3. Send `Hello, introduce yourself briefly.`
4. Send a short follow-up such as `Remember what I just said?`.
5. Switch providers by editing `AI_PROVIDER` in `backend/.env`, then restart Uvicorn.

Useful direct API test:

```bash
curl http://localhost:8000/api/health

curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello from the terminal"}'
```

### Stage 2: Tool Calling, Reminders, and Notes

The updated backend contains:

```text
backend/
├── .env.example
├── requirements.txt
├── jarvis.db
└── app/
    ├── agent.py
    ├── config.py
    ├── db.py
    ├── main.py
    ├── tools.py
    └── __init__.py
```

Install dependencies again and restart the backend:

```bash
cd /workspaces/Elliot/backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

With Ollama, AI_PROVIDER=ollama keeps Stage 1 chat running but does not enable Claude tool calling. Use AI_PROVIDER=claude with a valid key for Stage 2 tools.

Test these messages:

1. `Set a reminder to submit the report tomorrow at 5pm.`
2. `List my reminders.`
3. `Save a note titled Project idea: review API authentication before Friday.`
4. `Find my note about API authentication.`
5. `Search the web for the latest official PIB press releases about AI and summarize them with source URLs.`

Test direct tool storage with:

```bash
curl http://localhost:8000/reminders/due

curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"List my reminders"}'
```

The frontend polls `/api/reminders/due` every 30 seconds. Due reminders are marked notified after one delivery.

### Stage 2 Check

Confirm the chat works, web and page-reading tools return real URLs, reminders use IST `YYYY-MM-DD HH:MM`, notes can be found and only deleted after confirmation, and the health endpoint reports the configured model.

## Stage 3: Live News Engine

Stage 3 adds RSS polling, article storage, AI summarization, live WebSocket alerts, and news query tools.

Verified feeds in `backend/feeds.yaml`:

- The Hindu
- NDTV
- Times of India
- PIB
- BBC
- Al Jazeera

The original PIB RSS URL returned no entries, so it was replaced by the verified official feed variant `https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3`.

Install and run:

```bash
cd /workspaces/Elliot/backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Configure `backend/.env`:

```env
AI_PROVIDER=claude
ANTHROPIC_API_KEY=your_key_here
MODEL=claude-3-5-sonnet-latest
NEWS_MODEL=claude-3-5-haiku-latest
NEWS_AI_DAILY_LIMIT=100
NEWS_POLL_INTERVAL_SECONDS=60
```

The scheduler polls enabled feeds every 60 seconds. Start it alongside the frontend:

```bash
cd /workspaces/Elliot/frontend
npm run dev
```

Useful endpoints:

```text
GET  /api/news/latest?limit=20&category=national&topic=ISRO
GET  /api/news/settings
POST /api/news/settings
WS   /ws/news
```

Test with these chat messages:

1. `What's the latest news about ISRO?`
2. `Brief me on the last 2 hours of business news.`
3. `Alert me about ISRO.`

Stage 3 records each feed cycle, stored article count, failures, and retry backoff. RSS delivery speed depends on each publisher; the polling interval starts after a source publishes.

## Stage 4: Persistent Memory and Documents

Stage 4 adds persistent conversations, long-term memory, local Chroma embeddings, document upload and search, and memory controls.

```text
backend/
├── app/
│   ├── agent.py
│   ├── db.py
│   ├── main.py
│   ├── memory.py
│   ├── memory_store.py
│   ├── news_engine.py
│   └── tools.py
├── data/
│   ├── jarvis.db
│   ├── chroma/
│   └── documents/
├── .env.example
├── feeds.yaml
└── requirements.txt
```

Install dependencies and run:

```bash
cd /workspaces/Elliot/backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Stage 4 uses Chroma's default local embedding model, `all-MiniLM-L6-v2` (ONNX). It is approximately 80 MB on disk and typically uses roughly 100-300 MB RAM depending on cache and collection size. It does not send embeddings to an external API.

Key environment variables:

```env
MEMORY_MODEL=claude-3-5-haiku-latest
MEMORY_LEARNING=on
MEMORY_SUMMARY_TURNS=20
MEMORY_RECALL_THRESHOLD=0.25
ELLIOT_DATA_DIR=./data
ELLIOT_CHROMA_DIR=./data/chroma
DOCUMENT_MAX_SIZE_MB=20
DOCUMENT_CHUNK_CHARS=2000
DOCUMENT_CHUNK_OVERLAP=200
```

Useful endpoints:

```text
GET    /conversations
POST   /conversations
GET    /conversations/{id}
DELETE /conversations/{id}
GET    /memories
PATCH  /memories/{id}
DELETE /memories/{id}
DELETE /memories?confirm=true
GET    /memories/export
POST   /documents
GET    /documents
DELETE /documents/{id}
GET    /memory-settings
PATCH  /memory-settings?enabled=false
```

Test memory by saying:

1. `I am preparing for Python developer interviews.`
2. `What do you know about me?`
3. `Remember that I prefer practical interview preparation.`
4. `Change my target role to backend Python developer.`
5. Upload a PDF, then ask about its content.

Sensitive test text such as `My password is hunter2` is blocked and not stored.

Memory extraction is imperfect. Review edits through the Memory panel, memory API, or export file, and correct any fact manually.

## Stage 5: Job Hunter

Stage 5 adds profile storage, legal job-source plugins, job matching, resume/cover-letter drafts, application tracking, and CSV export.

Working public job sources verified in this workspace:

- Remotive API: working
- RemoteOK API: working
- Adzuna: not active until `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` are supplied
- Greenhouse/Lever: enabled per company entry in `backend/companies.yaml`

No LinkedIn, Naukri, or Indeed automation is included. Add unsupported listings by pasting public text or a URL through `/jobs/manual`.

```text
backend/
├── app/
│   ├── agent.py
│   ├── db.py
│   ├── job_hunter.py
│   ├── job_sources.py
│   ├── matcher.py
│   ├── profile.py
│   ├── resume.py
│   └── tools.py
├── data/
├── companies.yaml
├── feeds.yaml
├── requirements.txt
└── .env.example
```

Configure Stage 5 in `backend/.env`:

```env
MATCH_MODEL=claude-3-5-haiku-latest
JOB_AI_DAILY_LIMIT=100
JOB_FETCH_INTERVAL_HOURS=6
MAX_JOB_AGE_DAYS=30
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
```

Run:

```bash
cd /workspaces/Elliot/backend
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Useful endpoints:

```text
GET/PUT   /profile
POST      /profile/resume/parse
GET       /jobs
POST      /jobs/fetch
POST      /jobs/manual
POST      /jobs/{id}/score
PATCH     /jobs/{id}/status
POST      /jobs/{id}/tailor-resume
POST      /jobs/{id}/cover-letter?tone=formal
GET       /tracker/export
GET       /application-stats
```

Test:

1. Save your profile with `PUT /profile` or the Profile panel.
2. Run `POST /jobs/fetch` for public sources.
3. Confirm duplicate jobs are not added twice.
4. Add a pasted job containing `registration fee` and confirm `scam_risk=1`.
5. Tailor a resume only after confirming its master resume text.
6. Export a cover letter as DOCX/PDF and review it manually.
7. Move a job to Applied and confirm a 7-day reminder is created.
8. Confirm there is no submit, send, or form-fill action.

Free job APIs cover only part of the market. Match scores are prioritization estimates, not guarantees. JARVIS prepares drafts; you submit applications yourself.

Do not start Stage 6.
