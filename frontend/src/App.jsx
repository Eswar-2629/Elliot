import { useEffect, useRef, useState } from "react";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [articles, setArticles] = useState([]);
  const [memories, setMemories] = useState([]);
  const [memoryLearning, setMemoryLearning] = useState(true);
  const [showMemory, setShowMemory] = useState(false);
  const [conversationId, setConversationId] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [profile, setProfile] = useState(null);
  const [activeJobPanel, setActiveJobPanel] = useState(null);
  const endRef = useRef(null);

  async function loadStatus() {
    try {
      const response = await fetch(`${API_URL}/api/health`);
      if (!response.ok) throw new Error("Backend is unavailable");
      setStatus(await response.json());
    } catch {
      setStatus({ status: "offline" });
    }
  }

  async function loadLatestNews() {
    try {
      const response = await fetch(`${API_URL}/api/news/latest?limit=20`);
      if (response.ok) {
        const data = await response.json();
        setArticles(data.articles || []);
      }
    } catch {
      // The WebSocket and next startup load will retry availability.
    }
  }

  async function loadMemories() {
    try {
      const [memoryResponse, settingResponse] = await Promise.all([
        fetch(`${API_URL}/memories`),
        fetch(`${API_URL}/memory-settings`),
      ]);
      if (memoryResponse.ok) setMemories((await memoryResponse.json()).memories || []);
      if (settingResponse.ok) setMemoryLearning((await settingResponse.json()).memory_learning);
    } catch {
      // Memory controls remain available when the backend is restarted.
    }
  }

  async function loadJobs() {
    try {
      const response = await fetch(`${API_URL}/jobs?limit=100`);
      if (response.ok) setJobs((await response.json()).jobs || []);
    } catch {}
  }

  async function loadProfile() {
    try {
      const response = await fetch(`${API_URL}/profile`);
      if (response.ok) setProfile(await response.json());
    } catch {}
  }

  async function updateJobStatus(jobId, status) {
    await fetch(`${API_URL}/jobs/${jobId}/status`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, note: "" }) });
    loadJobs();
  }

  useEffect(() => {
    loadStatus();
    loadLatestNews();
    loadMemories();
    loadJobs();
    loadProfile();

    const wsUrl = `${API_URL.replace(/^http/, "ws")}/ws/news`;
    const socket = new WebSocket(wsUrl);
    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === "news") {
        setArticles((current) => [data.article, ...current.filter((item) => item.id !== data.article.id)].slice(0, 20));
      }
    };

    return () => socket.close();
  }, []);

  async function pollDueReminders() {
    try {
      const response = await fetch(`${API_URL}/api/reminders/due`);
      if (!response.ok) return;
      const data = await response.json();
      if (data.reminders?.length) {
        const notice = data.reminders
          .map((reminder) => `${reminder.text} (${reminder.due_at})`)
          .join("\n");
        setMessages((current) => [...current, { role: "assistant", content: `Reminder:\n${notice}` }]);
      }
    } catch {
      // Reminder polling will retry on the next interval.
    }
  }

  useEffect(() => {
    pollDueReminders();
    const timer = window.setInterval(pollDueReminders, 30000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function sendMessage(event) {
    event.preventDefault();
    const content = input.trim();
    if (!content || loading) return;

    const nextMessages = [...messages, { role: "user", content }];
    setMessages(nextMessages);
    setInput("");
    setLoading(true);

    try {
      const response = await fetch(`${API_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: content, conversation_id: conversationId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Request failed");
      setConversationId(data.conversation_id || conversationId);
      setMessages([...nextMessages, { role: "assistant", content: data.reply }]);
      loadMemories();
    } catch (error) {
      setMessages([...nextMessages, { role: "assistant", content: `Elliot error: ${error.message}` }]);
    } finally {
      setLoading(false);
      loadStatus();
    }
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">PERSONAL AI SYSTEM</p>
          <h1>Elliot</h1>
        </div>
        <div className="header-actions">
          <button className="memory-toggle" onClick={() => setActiveJobPanel((current) => current === "jobs" ? null : "jobs")}>Jobs</button>
          <button className="memory-toggle" onClick={() => setActiveJobPanel((current) => current === "tracker" ? null : "tracker")}>Tracker</button>
          <button className="memory-toggle" onClick={() => setActiveJobPanel((current) => current === "profile" ? null : "profile")}>Profile</button>
          <button className="memory-toggle" onClick={() => setShowMemory((visible) => !visible)}>Memory</button>
          <div className={`system-status ${status?.status === "ok" ? "online" : "offline"}`}>
            <span className="status-dot" />
            {status?.status === "ok" ? `${status.provider} / ${status.model}` : "OFFLINE"}
          </div>
        </div>
      </header>

      <div className="news-ticker" aria-label="Latest news ticker">
        {articles.length ? articles.slice(0, 8).map((article) => (
          <a key={article.id} href={article.url} target="_blank" rel="noreferrer">
            <strong>{article.source}</strong> {article.title}
          </a>
        )) : <span>Waiting for RSS news articles...</span>}
      </div>

      <div className="stage-layout">
      <section className="chat-panel" aria-label="Chat with Elliot">
        <div className="messages">
          {messages.length === 0 && (
            <div className="welcome">
              <span className="signal">J</span>
              <p>How can I help you today?</p>
            </div>
          )}
          {messages.map((message, index) => (
            <article key={index} className={`message ${message.role}`}>
              <span className="message-label">{message.role === "user" ? "YOU" : "ELLIOT"}</span>
              <p>{message.content}</p>
            </article>
          ))}
          {loading && <div className="loading">Elliot is thinking<span>.</span><span>.</span><span>.</span></div>}
          <div ref={endRef} />
        </div>

        <form className="command-bar" onSubmit={sendMessage}>
          <label htmlFor="command" className="sr-only">Command</label>
          <input
            id="command"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Ask Elliot..."
            maxLength={10000}
            autoComplete="off"
          />
          <button type="submit" disabled={loading || !input.trim()} aria-label="Send message">Send</button>
        </form>
      </section>
      <aside className="news-panel" aria-label="Latest news">
        <div className="panel-heading"><span>LATEST NEWS</span><small>IST</small></div>
        <div className="news-list">
          {articles.map((article) => {
            const time = article.published_at ? new Date(article.published_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" }) : "--:--";
            return (
              <article className={`news-item ${article.alerted ? "alerted" : ""}`} key={article.id}>
                <div className="news-meta"><strong>{article.source}</strong><span>{time}</span></div>
                <a href={article.url} target="_blank" rel="noreferrer">{article.title}</a>
                {article.summary && <p>{article.summary}</p>}
              </article>
            );
          })}
          {!articles.length && <p className="news-empty">No stored articles yet.</p>}
        </div>
      </aside>
      </div>
      {activeJobPanel && (
        <section className="jobs-panel" aria-label={`${activeJobPanel} panel`}>
          <div className="memory-heading"><span>{activeJobPanel.toUpperCase()}</span><button className="panel-close" onClick={() => setActiveJobPanel(null)}>Close</button></div>
          {activeJobPanel === "profile" && profile && (
            <div className="job-panel-list">
              <label>Name<input defaultValue={profile.full_name} /></label>
              <label>Email<input defaultValue={profile.email} /></label>
              <label>Location<input defaultValue={profile.location} /></label>
              <label>Target roles<input defaultValue={(profile.target_roles || []).join(", ")} /></label>
              <label>Skills<input defaultValue={(profile.skills || []).join(", ")} /></label>
              <button onClick={async (event) => {
                const fields = event.currentTarget.parentElement.querySelectorAll("input");
                await fetch(`${API_URL}/profile`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ full_name: fields[0].value, email: fields[1].value, location: fields[2].value, target_roles: fields[3].value.split(',').map((value) => value.trim()).filter(Boolean), skills: fields[4].value.split(',').map((value) => value.trim()).filter(Boolean) }) });
                loadProfile();
              }}>Save Profile</button>
              <button onClick={async () => { await fetch(`${API_URL}/jobs/fetch`, { method: "POST" }); loadJobs(); }}>Fetch Jobs Now</button>
            </div>
          )}
          {activeJobPanel !== "profile" && (
            <div className="job-panel-list">
              {jobs.filter((job) => activeJobPanel !== "tracker" || !["new", "ignored"].includes(job.status)).map((job) => (
                <article className={`job-item ${job.scam_risk ? "scam" : ""}`} key={job.id}>
                  <div className="job-meta"><strong>{job.company}</strong><span>{job.match_score}%</span></div>
                  <a href={job.url} target="_blank" rel="noreferrer">{job.title}</a>
                  <p>{job.match_reason || "Not scored yet."}</p>
                  {!!job.gaps?.length && <small>Gaps: {job.gaps.join(", ")}</small>}
                  <div className="job-actions">
                    <button onClick={() => updateJobStatus(job.id, "saved")}>Save</button>
                    <button onClick={() => updateJobStatus(job.id, "ignored")}>Ignore</button>
                    <button onClick={async () => { await fetch(`${API_URL}/jobs/${job.id}/tailor-resume`, { method: "POST" }); }}>Tailor Resume</button>
                    <button onClick={async () => { await fetch(`${API_URL}/jobs/${job.id}/cover-letter?tone=formal`, { method: "POST" }); }}>Cover Letter</button>
                  </div>
                  {activeJobPanel === "tracker" && <select value={job.status} onChange={(event) => updateJobStatus(job.id, event.target.value)}>
                    {["new", "saved", "applied", "interview", "offer", "rejected", "ignored"].map((status) => <option key={status}>{status}</option>)}
                  </select>}
                </article>
              ))}
              {!jobs.length && <p className="news-empty">No jobs found yet.</p>}
            </div>
          )}
        </section>
      )}
      {showMemory && (
        <section className="memory-panel" aria-label="Memory controls">
          <div className="memory-heading">
            <span>MEMORIES</span>
            <label><input type="checkbox" checked={memoryLearning} onChange={async (event) => {
              const enabled = event.target.checked;
              await fetch(`${API_URL}/memory-settings?enabled=${enabled}`, { method: "PATCH" });
              setMemoryLearning(enabled);
            }} /> Learning</label>
          </div>
          <div className="memory-list">
            {memories.map((memory) => (
              <article className="memory-item" key={memory.id}>
                <select value={memory.category} onChange={async (event) => {
                  const category = event.target.value;
                  await fetch(`${API_URL}/memories/${memory.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category }) });
                  loadMemories();
                }}>
                  {["preference", "skill", "goal", "personal", "work", "other"].map((category) => <option key={category}>{category}</option>)}
                </select>
                <input defaultValue={memory.content} onBlur={async (event) => {
                  await fetch(`${API_URL}/memories/${memory.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: event.target.value }) });
                  loadMemories();
                }} />
                <button onClick={async () => {
                  await fetch(`${API_URL}/memories/${memory.id}`, { method: "DELETE" });
                  loadMemories();
                }}>Delete</button>
              </article>
            ))}
            {!memories.length && <p className="news-empty">No memories stored yet.</p>}
          </div>
        </section>
      )}
    </main>
  );
}
