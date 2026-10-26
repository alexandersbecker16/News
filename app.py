"""
Source Desk — search recent coverage from trusted news outlets.
Run: python app.py
Then open http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

MAX_HEADLINES = 12
ARTICLES_PER_HEADLINE = 12

HOST = "127.0.0.1"
PORT = 8765

TRUSTED_SOURCES = [
    {"id": "nytimes", "name": "The New York Times", "domain": "nytimes.com"},
    {"id": "wsj", "name": "The Wall Street Journal", "domain": "wsj.com"},
    {"id": "reuters", "name": "Reuters", "domain": "reuters.com"},
    {"id": "ap", "name": "Associated Press", "domain": "apnews.com"},
    {"id": "wapo", "name": "The Washington Post", "domain": "washingtonpost.com"},
    {"id": "bbc", "name": "BBC News", "domain": "bbc.com"},
    {"id": "npr", "name": "NPR", "domain": "npr.org"},
    {"id": "guardian", "name": "The Guardian", "domain": "theguardian.com"},
    {"id": "bloomberg", "name": "Bloomberg", "domain": "bloomberg.com"},
    {"id": "ft", "name": "Financial Times", "domain": "ft.com"},
    {"id": "economist", "name": "The Economist", "domain": "economist.com"},
    {"id": "pbs", "name": "PBS NewsHour", "domain": "pbs.org"},
]

DOMAIN_TO_SOURCE = {s["domain"]: s for s in TRUSTED_SOURCES}
# BBC also publishes under bbc.co.uk
DOMAIN_TO_SOURCE["bbc.co.uk"] = DOMAIN_TO_SOURCE["bbc.com"]

USER_AGENT = (
    "Mozilla/5.0 (compatible; SourceDesk/1.0; +http://127.0.0.1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def site_filter(domains: list[str]) -> str:
    return " OR ".join(f"site:{d}" for d in domains)


def build_rss_url(query: str, when: str, domains: list[str]) -> str:
    parts = [query.strip()]
    if domains:
        parts.append(f"({site_filter(domains)})")
    if when and when != "any":
        parts.append(f"when:{when}")
    q = " ".join(parts)
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
    )


def fetch_text(url: str, timeout: float = 20.0) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_pub_date(raw: str | None) -> tuple[str | None, float]:
    if not raw:
        return None, 0.0
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(), dt.timestamp()
    except (TypeError, ValueError, IndexError):
        return raw, 0.0


def clean_title(title: str) -> tuple[str, str | None]:
    """Google News titles often end with ' - Source Name'."""
    if " - " in title:
        main, source = title.rsplit(" - ", 1)
        if len(source) < 40:
            return main.strip(), source.strip()
    return title.strip(), None


def match_source(source_name: str | None, link: str) -> dict | None:
    host = urlparse(link).netloc.lower().removeprefix("www.")
    for domain, meta in DOMAIN_TO_SOURCE.items():
        if host == domain or host.endswith("." + domain):
            return meta
    if source_name:
        lowered = source_name.lower()
        for meta in TRUSTED_SOURCES:
            if meta["name"].lower() in lowered or meta["id"] in lowered:
                return meta
    return None


def extract_items(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    items: list[dict] = []
    for item in channel.findall("item"):
        title_raw = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = item.findtext("pubDate")
        source_el = item.find("source")
        source_name = (source_el.text or "").strip() if source_el is not None else None
        source_url = source_el.get("url") if source_el is not None else None

        title, title_source = clean_title(title_raw)
        if not source_name and title_source:
            source_name = title_source

        # Prefer publisher homepage from <source url>, else article link host
        probe = source_url or link
        matched = match_source(source_name, probe)
        iso, ts = parse_pub_date(pub_raw)

        items.append(
            {
                "title": title,
                "url": link,
                "published": iso,
                "published_ts": ts,
                "source_name": matched["name"] if matched else (source_name or "Unknown"),
                "source_id": matched["id"] if matched else "other",
                "source_domain": matched["domain"] if matched else None,
            }
        )
    return items


def parse_headlines(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        lines = [str(item) for item in raw]
    else:
        lines = str(raw).replace("\r\n", "\n").replace("\r", "\n").split("\n")

    headlines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = line.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        headlines.append(text)
    return headlines


def resolve_sources(source_ids: list[str] | None) -> list[dict]:
    if source_ids:
        selected = [s for s in TRUSTED_SOURCES if s["id"] in source_ids]
        return selected or TRUSTED_SOURCES
    return TRUSTED_SOURCES


def search_one(
    query: str,
    when: str,
    selected: list[dict],
    limit: int = ARTICLES_PER_HEADLINE,
) -> dict:
    domains = [s["domain"] for s in selected]
    if any(s["id"] == "bbc" for s in selected):
        domains.append("bbc.co.uk")

    rss_url = build_rss_url(query, when, domains)
    try:
        xml_text = fetch_text(rss_url)
        items = extract_items(xml_text)
    except HTTPError as e:
        return {
            "ok": False,
            "query": query,
            "error": f"News feed returned HTTP {e.code}. Try again in a moment.",
            "count": 0,
            "articles": [],
        }
    except URLError as e:
        return {
            "ok": False,
            "query": query,
            "error": f"Could not reach news feed: {e.reason}",
            "count": 0,
            "articles": [],
        }
    except ET.ParseError:
        return {
            "ok": False,
            "query": query,
            "error": "Could not parse news feed. Try a different query.",
            "count": 0,
            "articles": [],
        }

    trusted_ids = {s["id"] for s in selected}
    filtered = [i for i in items if i["source_id"] in trusted_ids] or items
    filtered.sort(key=lambda i: i["published_ts"], reverse=True)

    articles = filtered[:limit]
    for i in articles:
        i.pop("published_ts", None)

    return {
        "ok": True,
        "query": query,
        "count": len(articles),
        "articles": articles,
    }


def search_news(
    queries: str | list[str],
    when: str = "7d",
    source_ids: list[str] | None = None,
) -> dict:
    headlines = parse_headlines(queries)
    if not headlines:
        return {"ok": False, "error": "Enter one or more headlines (one per line)."}
    if len(headlines) > MAX_HEADLINES:
        return {
            "ok": False,
            "error": f"Too many headlines. Enter up to {MAX_HEADLINES} (one per line).",
        }
    if any(len(h) > 300 for h in headlines):
        return {"ok": False, "error": "One of the headlines is too long (max 300 characters)."}

    selected = resolve_sources(source_ids)
    limit = 40 if len(headlines) == 1 else ARTICLES_PER_HEADLINE
    results: list[dict | None] = [None] * len(headlines)

    with ThreadPoolExecutor(max_workers=min(6, len(headlines))) as pool:
        futures = {
            pool.submit(search_one, headline, when, selected, limit): idx
            for idx, headline in enumerate(headlines)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    groups = [r for r in results if r is not None]
    total = sum(g["count"] for g in groups)

    return {
        "ok": True,
        "when": when,
        "headline_count": len(groups),
        "count": total,
        "groups": groups,
        "sources_searched": [s["name"] for s in selected],
    }


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Source Desk</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet" />
  <style>
    :root {
      --ink: #15202b;
      --muted: #5a6a7a;
      --paper: #f7f8fa;
      --panel: #ffffff;
      --line: #d8dee6;
      --accent: #0d7377;
      --accent-deep: #095456;
      --warn: #9a3412;
      --shadow: 0 18px 50px rgba(21, 32, 43, 0.08);
      --radius: 14px;
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; }
    body {
      font-family: "IBM Plex Sans", system-ui, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at 10% -10%, rgba(13, 115, 119, 0.12), transparent 55%),
        radial-gradient(900px 500px at 100% 0%, rgba(21, 32, 43, 0.06), transparent 50%),
        linear-gradient(180deg, #eef2f5 0%, var(--paper) 40%, #e9eef3 100%);
    }

    .wrap {
      width: min(920px, calc(100% - 2rem));
      margin: 0 auto;
      padding: 2.5rem 0 4rem;
    }

    header {
      margin-bottom: 1.75rem;
      animation: rise 0.55s ease-out both;
    }

    .brand {
      font-family: Outfit, sans-serif;
      font-weight: 700;
      font-size: clamp(2rem, 5vw, 2.75rem);
      letter-spacing: -0.03em;
      line-height: 1.05;
      margin: 0 0 0.55rem;
    }

    .brand span { color: var(--accent); }

    .lede {
      margin: 0;
      max-width: 38rem;
      color: var(--muted);
      font-size: 1.05rem;
      line-height: 1.5;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 1.25rem;
      animation: rise 0.65s ease-out 0.05s both;
    }

    form { display: grid; gap: 1rem; }

    label {
      display: block;
      font-family: Outfit, sans-serif;
      font-size: 0.78rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 0.45rem;
    }

    .headline-input {
      width: 100%;
      border: 1.5px solid var(--line);
      border-radius: 10px;
      padding: 0.95rem 1rem;
      font: 500 1.05rem/1.45 Outfit, sans-serif;
      color: var(--ink);
      background: #fbfcfd;
      outline: none;
      resize: vertical;
      min-height: 7.5rem;
      transition: border-color 0.15s, box-shadow 0.15s;
    }

    .headline-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(13, 115, 119, 0.15);
      background: #fff;
    }

    .hint {
      margin: 0.45rem 0 0;
      color: var(--muted);
      font-size: 0.86rem;
    }

    .row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 0.75rem;
      align-items: end;
    }

    select {
      width: 100%;
      border: 1.5px solid var(--line);
      border-radius: 10px;
      padding: 0.7rem 0.85rem;
      font: 500 0.95rem IBM Plex Sans, sans-serif;
      background: #fbfcfd;
      color: var(--ink);
    }

    button[type="submit"] {
      border: 0;
      border-radius: 10px;
      padding: 0.85rem 1.35rem;
      font: 600 0.95rem Outfit, sans-serif;
      color: #fff;
      background: linear-gradient(180deg, #12949a, var(--accent));
      cursor: pointer;
      white-space: nowrap;
      transition: transform 0.12s ease, background 0.15s;
    }

    button[type="submit"]:hover { background: linear-gradient(180deg, #0f858a, var(--accent-deep)); }
    button[type="submit"]:active { transform: translateY(1px); }
    button[type="submit"]:disabled { opacity: 0.65; cursor: wait; }

    details.sources {
      border-top: 1px solid var(--line);
      padding-top: 0.85rem;
    }

    details.sources summary {
      cursor: pointer;
      font-family: Outfit, sans-serif;
      font-weight: 600;
      font-size: 0.92rem;
      color: var(--ink);
      list-style: none;
    }

    details.sources summary::-webkit-details-marker { display: none; }
    details.sources summary::after {
      content: "Show";
      float: right;
      color: var(--accent);
      font-weight: 500;
      font-size: 0.85rem;
    }
    details.sources[open] summary::after { content: "Hide"; }

    .source-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 0.45rem 0.75rem;
      margin-top: 0.85rem;
    }

    .source-grid label {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      margin: 0;
      text-transform: none;
      letter-spacing: 0;
      font: 500 0.88rem IBM Plex Sans, sans-serif;
      color: var(--ink);
      cursor: pointer;
    }

    .source-grid input { accent-color: var(--accent); }

    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem 1rem;
      align-items: center;
      margin: 1.35rem 0 0.75rem;
      color: var(--muted);
      font-size: 0.92rem;
      min-height: 1.25rem;
      animation: rise 0.5s ease-out both;
    }

    .meta strong { color: var(--ink); font-weight: 600; }

    .status-error {
      color: var(--warn);
      background: #fff7ed;
      border: 1px solid #fed7aa;
      border-radius: 10px;
      padding: 0.85rem 1rem;
      margin-top: 1rem;
    }

    .results {
      display: grid;
      gap: 1.5rem;
      margin-top: 0.5rem;
    }

    .headline-group {
      display: grid;
      gap: 0.75rem;
      animation: rise 0.45s ease-out both;
    }

    .headline-group-header {
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem 0.75rem;
      align-items: baseline;
      padding-bottom: 0.35rem;
      border-bottom: 1px solid var(--line);
    }

    .headline-group-header h2 {
      margin: 0;
      font-family: Outfit, sans-serif;
      font-size: 1.2rem;
      font-weight: 650;
      letter-spacing: -0.02em;
      line-height: 1.3;
      flex: 1 1 16rem;
    }

    .headline-group-header .count {
      color: var(--muted);
      font-size: 0.88rem;
    }

    .group-error {
      color: var(--warn);
      background: #fff7ed;
      border: 1px solid #fed7aa;
      border-radius: 10px;
      padding: 0.75rem 0.9rem;
      font-size: 0.92rem;
    }

    .group-articles {
      display: grid;
      gap: 0.75rem;
    }

    article.result {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 1rem 1.1rem;
      transition: border-color 0.15s, transform 0.15s;
      animation: rise 0.45s ease-out both;
    }

    article.result:hover {
      border-color: #b9c7d4;
      transform: translateY(-1px);
    }

    .source-tag {
      display: inline-block;
      font-family: Outfit, sans-serif;
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      color: var(--accent-deep);
      background: rgba(13, 115, 119, 0.1);
      border-radius: 6px;
      padding: 0.2rem 0.45rem;
      margin-bottom: 0.45rem;
    }

    article.result h2 {
      margin: 0 0 0.4rem;
      font-family: Outfit, sans-serif;
      font-size: 1.08rem;
      font-weight: 600;
      letter-spacing: -0.015em;
      line-height: 1.35;
    }

    article.result h2 a {
      color: inherit;
      text-decoration: none;
    }

    article.result h2 a:hover { color: var(--accent-deep); text-decoration: underline; }

    .when {
      margin: 0;
      color: var(--muted);
      font-size: 0.86rem;
    }

    .empty {
      text-align: center;
      color: var(--muted);
      padding: 2.5rem 1rem;
      border: 1px dashed var(--line);
      border-radius: var(--radius);
      background: rgba(255,255,255,0.55);
    }

    footer {
      margin-top: 2rem;
      color: var(--muted);
      font-size: 0.8rem;
      line-height: 1.45;
    }

    @keyframes rise {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @media (max-width: 640px) {
      .row { grid-template-columns: 1fr; }
      button[type="submit"] { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1 class="brand">Source <span>Desk</span></h1>
      <p class="lede">Type one or more headlines. Get the latest matching coverage from NYT, WSJ, Reuters, AP, and other trusted outlets.</p>
    </header>

    <section class="panel">
      <form id="search-form">
        <div>
          <label for="q">Headlines</label>
          <textarea class="headline-input" id="q" name="q" rows="5" placeholder="Federal Reserve interest rates&#10;Ukraine peace talks&#10;Apple earnings" required autofocus></textarea>
          <p class="hint">One headline per line — up to 12 at a time.</p>
        </div>

        <div class="row">
          <div>
            <label for="when">Recency</label>
            <select id="when" name="when">
              <option value="1d">Past 24 hours</option>
              <option value="7d" selected>Past 7 days</option>
              <option value="30d">Past 30 days</option>
              <option value="any">Any time</option>
            </select>
          </div>
          <button type="submit" id="go">Find coverage</button>
        </div>

        <details class="sources">
          <summary>Trusted sources</summary>
          <div class="source-grid" id="source-grid"></div>
        </details>
      </form>
    </section>

    <div class="meta" id="meta" hidden></div>
    <div id="error" class="status-error" hidden></div>
    <div class="results" id="results">
      <div class="empty">Enter headlines above to see recent reporting from major outlets.</div>
    </div>

    <footer>
      Results are gathered via Google News RSS, limited to selected publishers. Links may open through Google News before the publisher page.
    </footer>
  </div>

  <script>
    const SOURCES = __SOURCES_JSON__;

    const form = document.getElementById("search-form");
    const qInput = document.getElementById("q");
    const whenSelect = document.getElementById("when");
    const goBtn = document.getElementById("go");
    const sourceGrid = document.getElementById("source-grid");
    const resultsEl = document.getElementById("results");
    const metaEl = document.getElementById("meta");
    const errorEl = document.getElementById("error");

    SOURCES.forEach((s) => {
      const label = document.createElement("label");
      label.innerHTML = `<input type="checkbox" name="source" value="${s.id}" checked /> ${s.name}`;
      sourceGrid.appendChild(label);
    });

    function selectedSources() {
      return [...form.querySelectorAll('input[name="source"]:checked')].map((el) => el.value);
    }

    function formatWhen(iso) {
      if (!iso) return "Date unknown";
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleString(undefined, {
        month: "short", day: "numeric", year: "numeric",
        hour: "numeric", minute: "2-digit"
      });
    }

    function showError(msg) {
      errorEl.hidden = !msg;
      errorEl.textContent = msg || "";
    }

    function renderArticle(a, i) {
      return `
        <article class="result" style="animation-delay:${Math.min(i, 12) * 0.04}s">
          <div class="source-tag">${escapeHtml(a.source_name)}</div>
          <h2><a href="${escapeAttr(a.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(a.title)}</a></h2>
          <p class="when">${escapeHtml(formatWhen(a.published))}</p>
        </article>
      `;
    }

    function renderGroup(group, groupIndex) {
      const body = !group.ok
        ? `<div class="group-error">${escapeHtml(group.error || "Search failed for this headline.")}</div>`
        : group.articles.length
          ? `<div class="group-articles">${group.articles.map(renderArticle).join("")}</div>`
          : `<div class="empty">No recent matches for this headline.</div>`;

      return `
        <section class="headline-group" style="animation-delay:${groupIndex * 0.05}s">
          <div class="headline-group-header">
            <h2>${escapeHtml(group.query)}</h2>
            <span class="count">${group.ok ? `${group.count} article${group.count === 1 ? "" : "s"}` : "Error"}</span>
          </div>
          ${body}
        </section>
      `;
    }

    function render(data) {
      showError("");
      if (!data.ok) {
        metaEl.hidden = true;
        resultsEl.innerHTML = "";
        showError(data.error || "Something went wrong.");
        return;
      }

      const n = data.headline_count || (data.groups || []).length;
      metaEl.hidden = false;
      metaEl.innerHTML = `<strong>${data.count}</strong> article${data.count === 1 ? "" : "s"} across <strong>${n}</strong> headline${n === 1 ? "" : "s"}`;

      if (!data.groups || !data.groups.length) {
        resultsEl.innerHTML = `<div class="empty">No recent matches from the selected sources. Try broader wording or a longer time range.</div>`;
        return;
      }

      resultsEl.innerHTML = data.groups.map(renderGroup).join("");
    }

    function escapeHtml(str) {
      return String(str)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function escapeAttr(str) {
      return escapeHtml(str).replaceAll("'", "&#39;");
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const q = qInput.value.trim();
      if (!q) return;

      const sources = selectedSources();
      if (!sources.length) {
        showError("Select at least one trusted source.");
        return;
      }

      goBtn.disabled = true;
      goBtn.textContent = "Searching…";
      showError("");
      metaEl.hidden = false;
      metaEl.innerHTML = "Searching trusted outlets…";
      resultsEl.innerHTML = `<div class="empty">Pulling the latest coverage…</div>`;

      try {
        const res = await fetch("/api/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            q,
            when: whenSelect.value,
            sources: sources.join(","),
          }),
        });
        const data = await res.json();
        render(data);
      } catch (err) {
        showError("Could not reach the local server. Is app.py still running?");
        resultsEl.innerHTML = "";
        metaEl.hidden = true;
      } finally {
        goBtn.disabled = false;
        goBtn.textContent = "Find coverage";
      }
    });
  </script>
</body>
</html>
"""


def render_index() -> bytes:
    sources_json = json.dumps(
        [{"id": s["id"], "name": s["name"]} for s in TRUSTED_SOURCES],
        ensure_ascii=True,
    )
    html = HTML_PAGE.replace("__SOURCES_JSON__", sources_json)
    return html.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {args[0]}")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_search(self, queries, when: str, raw_sources: str) -> None:
        if when not in {"1d", "7d", "30d", "any"}:
            when = "7d"
        source_ids = [s for s in raw_sources.split(",") if s] or None
        payload = search_news(queries, when=when, source_ids=source_ids)
        body = json.dumps(payload).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send(200, render_index(), "text/html; charset=utf-8")
            return

        if path == "/api/search":
            qs = parse_qs(parsed.query)
            # Support repeated q= params or a single multiline q
            queries = [unquote(v) for v in (qs.get("q") or [""])]
            when = (qs.get("when") or ["7d"])[0]
            raw_sources = (qs.get("sources") or [""])[0]
            self._handle_search(queries, when, raw_sources)
            return

        self._send(404, b'{"ok":false,"error":"Not found"}', "application/json; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/search":
            self._send(404, b'{"ok":false,"error":"Not found"}', "application/json; charset=utf-8")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 100_000:
            self._send(413, b'{"ok":false,"error":"Request too large"}', "application/json; charset=utf-8")
            return

        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"ok":false,"error":"Invalid JSON"}', "application/json; charset=utf-8")
            return

        queries = data.get("queries")
        if queries is None:
            queries = data.get("q", "")
        when = str(data.get("when") or "7d")
        raw_sources = data.get("sources") or ""
        if isinstance(raw_sources, list):
            raw_sources = ",".join(str(s) for s in raw_sources)
        else:
            raw_sources = str(raw_sources)

        self._handle_search(queries, when, raw_sources)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Source Desk running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
