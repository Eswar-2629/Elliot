import { useEffect, useRef, useState } from "react";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [articles, setArticles] = useState([]);
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

  useEffect(() => {
    loadStatus();
    loadLatestNews();

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
        body: JSON.stringify({ message: content }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Request failed");
      setMessages([...nextMessages, { role: "assistant", content: data.reply }]);
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
        <div className={`system-status ${status?.status === "ok" ? "online" : "offline"}`}>
          <span className="status-dot" />
          {status?.status === "ok" ? `${status.provider} / ${status.model}` : "OFFLINE"}
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
    </main>
  );
}
