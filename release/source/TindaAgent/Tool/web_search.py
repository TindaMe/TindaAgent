from __future__ import annotations

import json
import os
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib import error, parse, request


DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS = 20
MAX_QUERY_CHARS = 500
MAX_SNIPPET_CHARS = 900
USER_AGENT = "TindaAgent/1.9 web-search"
INDEX_PRIORITY = {
    "duckduckgo": 0,
    "google": 1,
    "bing": 2,
    "brave": 3,
    "baidu": 4,
    "github": 10,
    "stackoverflow": 11,
    "wikipedia": 12,
    "reddit": 13,
}


COMMON_WEB_INDEX: tuple[dict[str, Any], ...] = (
    {
        "id": "duckduckgo",
        "name": "DuckDuckGo",
        "category": "search",
        "domain": "duckduckgo.com",
        "search_url": "https://duckduckgo.com/?q={query}",
        "description": "General privacy-oriented web search fallback.",
        "tags": ["search", "general", "web"],
    },
    {
        "id": "google",
        "name": "Google Search",
        "category": "search",
        "domain": "google.com",
        "search_url": "https://www.google.com/search?q={query}",
        "description": "Mainstream general web search entry.",
        "tags": ["search", "general", "web"],
    },
    {
        "id": "bing",
        "name": "Bing",
        "category": "search",
        "domain": "bing.com",
        "search_url": "https://www.bing.com/search?q={query}",
        "description": "Mainstream general web search entry.",
        "tags": ["search", "general", "web"],
    },
    {
        "id": "brave",
        "name": "Brave Search",
        "category": "search",
        "domain": "search.brave.com",
        "search_url": "https://search.brave.com/search?q={query}",
        "description": "Independent general web search entry.",
        "tags": ["search", "general", "web"],
    },
    {
        "id": "baidu",
        "name": "Baidu",
        "category": "search",
        "domain": "baidu.com",
        "search_url": "https://www.baidu.com/s?wd={query}",
        "description": "Chinese-language general search entry.",
        "tags": ["search", "general", "china", "zh"],
    },
    {
        "id": "wikipedia",
        "name": "Wikipedia",
        "category": "reference",
        "domain": "wikipedia.org",
        "search_url": "https://en.wikipedia.org/w/index.php?search={query}",
        "description": "Encyclopedia reference for broad background checks.",
        "tags": ["reference", "encyclopedia", "facts"],
    },
    {
        "id": "github",
        "name": "GitHub",
        "category": "developer",
        "domain": "github.com",
        "search_url": "https://github.com/search?q={query}&type=repositories",
        "description": "Code repositories, issues, releases, examples, and project docs.",
        "tags": ["code", "developer", "issues", "repository"],
    },
    {
        "id": "stackoverflow",
        "name": "Stack Overflow",
        "category": "developer",
        "domain": "stackoverflow.com",
        "search_url": "https://stackoverflow.com/search?q={query}",
        "description": "Developer Q&A and implementation troubleshooting.",
        "tags": ["code", "developer", "qa", "debug"],
    },
    {
        "id": "stackexchange",
        "name": "Stack Exchange",
        "category": "reference",
        "domain": "stackexchange.com",
        "search_url": "https://stackexchange.com/search?q={query}",
        "description": "Cross-site Q&A for technical and knowledge topics.",
        "tags": ["qa", "reference", "technical"],
    },
    {
        "id": "reddit",
        "name": "Reddit",
        "category": "community",
        "domain": "reddit.com",
        "search_url": "https://www.reddit.com/search/?q={query}",
        "description": "Community discussions, recent user reports, and experiential signals.",
        "tags": ["community", "discussion", "recent"],
    },
    {
        "id": "hackernews",
        "name": "Hacker News",
        "category": "community",
        "domain": "news.ycombinator.com",
        "search_url": "https://hn.algolia.com/?q={query}",
        "description": "Technology discussion archive via Algolia HN Search.",
        "tags": ["community", "startup", "developer", "discussion"],
    },
    {
        "id": "youtube",
        "name": "YouTube",
        "category": "media",
        "domain": "youtube.com",
        "search_url": "https://www.youtube.com/results?search_query={query}",
        "description": "Video tutorials, demos, conference talks, and product walkthroughs.",
        "tags": ["video", "tutorial", "media"],
    },
    {
        "id": "mdn",
        "name": "MDN Web Docs",
        "category": "developer_docs",
        "domain": "developer.mozilla.org",
        "search_url": "https://developer.mozilla.org/en-US/search?q={query}",
        "description": "Official-style reference for Web APIs, HTML, CSS, and JavaScript.",
        "tags": ["docs", "web", "javascript", "css", "html"],
    },
    {
        "id": "python_docs",
        "name": "Python Docs",
        "category": "developer_docs",
        "domain": "docs.python.org",
        "search_url": "https://docs.python.org/3/search.html?q={query}",
        "description": "Official Python language and standard-library documentation.",
        "tags": ["docs", "python", "standard-library"],
    },
    {
        "id": "pypi",
        "name": "PyPI",
        "category": "packages",
        "domain": "pypi.org",
        "search_url": "https://pypi.org/search/?q={query}",
        "description": "Python package discovery and release metadata.",
        "tags": ["python", "packages", "release"],
    },
    {
        "id": "npm",
        "name": "npm",
        "category": "packages",
        "domain": "npmjs.com",
        "search_url": "https://www.npmjs.com/search?q={query}",
        "description": "JavaScript package discovery and release metadata.",
        "tags": ["javascript", "packages", "node"],
    },
    {
        "id": "microsoft_learn",
        "name": "Microsoft Learn",
        "category": "developer_docs",
        "domain": "learn.microsoft.com",
        "search_url": "https://learn.microsoft.com/en-us/search/?terms={query}",
        "description": "Official Microsoft product, Windows, Azure, and .NET documentation.",
        "tags": ["docs", "microsoft", "windows", "azure", "dotnet"],
    },
    {
        "id": "apple_developer",
        "name": "Apple Developer",
        "category": "developer_docs",
        "domain": "developer.apple.com",
        "search_url": "https://developer.apple.com/search/?q={query}",
        "description": "Official Apple platform documentation.",
        "tags": ["docs", "apple", "ios", "macos"],
    },
    {
        "id": "android_developers",
        "name": "Android Developers",
        "category": "developer_docs",
        "domain": "developer.android.com",
        "search_url": "https://developer.android.com/s/results?q={query}",
        "description": "Official Android documentation and guides.",
        "tags": ["docs", "android", "mobile"],
    },
    {
        "id": "docker_docs",
        "name": "Docker Docs",
        "category": "developer_docs",
        "domain": "docs.docker.com",
        "search_url": "https://docs.docker.com/search/?q={query}",
        "description": "Official Docker documentation and operational guides.",
        "tags": ["docs", "docker", "container"],
    },
    {
        "id": "kubernetes_docs",
        "name": "Kubernetes Docs",
        "category": "developer_docs",
        "domain": "kubernetes.io",
        "search_url": "https://www.google.com/search?q=site%3Akubernetes.io%2Fdocs+{query}",
        "description": "Kubernetes official documentation via site-scoped search.",
        "tags": ["docs", "kubernetes", "container"],
    },
    {
        "id": "fastapi_docs",
        "name": "FastAPI Docs",
        "category": "developer_docs",
        "domain": "fastapi.tiangolo.com",
        "search_url": "https://www.google.com/search?q=site%3Afastapi.tiangolo.com+{query}",
        "description": "FastAPI official documentation via site-scoped search.",
        "tags": ["docs", "python", "fastapi", "api"],
    },
    {
        "id": "react_docs",
        "name": "React Docs",
        "category": "developer_docs",
        "domain": "react.dev",
        "search_url": "https://www.google.com/search?q=site%3Areact.dev+{query}",
        "description": "React official documentation via site-scoped search.",
        "tags": ["docs", "react", "javascript", "frontend"],
    },
    {
        "id": "nextjs_docs",
        "name": "Next.js Docs",
        "category": "developer_docs",
        "domain": "nextjs.org",
        "search_url": "https://www.google.com/search?q=site%3Anextjs.org%2Fdocs+{query}",
        "description": "Next.js official documentation via site-scoped search.",
        "tags": ["docs", "nextjs", "react", "frontend"],
    },
    {
        "id": "tailwind_docs",
        "name": "Tailwind CSS Docs",
        "category": "developer_docs",
        "domain": "tailwindcss.com",
        "search_url": "https://www.google.com/search?q=site%3Atailwindcss.com%2Fdocs+{query}",
        "description": "Tailwind CSS official documentation via site-scoped search.",
        "tags": ["docs", "tailwind", "css", "frontend"],
    },
    {
        "id": "openai_docs",
        "name": "OpenAI Docs",
        "category": "ai_docs",
        "domain": "platform.openai.com",
        "search_url": "https://www.google.com/search?q=site%3Aplatform.openai.com%2Fdocs+{query}",
        "description": "OpenAI platform documentation via site-scoped search.",
        "tags": ["docs", "ai", "llm", "openai"],
    },
    {
        "id": "deepseek_docs",
        "name": "DeepSeek Docs",
        "category": "ai_docs",
        "domain": "api-docs.deepseek.com",
        "search_url": "https://www.google.com/search?q=site%3Aapi-docs.deepseek.com+{query}",
        "description": "DeepSeek API documentation via site-scoped search.",
        "tags": ["docs", "ai", "llm", "deepseek"],
    },
    {
        "id": "tavily_docs",
        "name": "Tavily Docs",
        "category": "ai_docs",
        "domain": "docs.tavily.com",
        "search_url": "https://www.google.com/search?q=site%3Adocs.tavily.com+{query}",
        "description": "Tavily API documentation via site-scoped search.",
        "tags": ["docs", "ai", "search", "tavily"],
    },
    {
        "id": "arxiv",
        "name": "arXiv",
        "category": "research",
        "domain": "arxiv.org",
        "search_url": "https://arxiv.org/search/?query={query}&searchtype=all",
        "description": "Research papers and preprints.",
        "tags": ["research", "paper", "academic"],
    },
)


def _bounded_text(value: Any, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", unescape(str(value or ""))).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _parse_bool(value: Any, default: bool = False) -> bool:
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "basic", "advanced"}


def _parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_query(query: str) -> str:
    normalized = re.sub(r"\s+", " ", str(query or "")).strip()
    if not normalized:
        raise ValueError("query is required")
    if len(normalized) > MAX_QUERY_CHARS:
        normalized = normalized[:MAX_QUERY_CHARS].rstrip()
    return normalized


def _split_csv(value: str) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for part in re.split(r"[,;\s]+", str(value or "")):
        item = part.strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        rows.append(item)
    return rows


def _format_search_url(template: str, query: str) -> str:
    return str(template or "").replace("{query}", parse.quote_plus(query))


def _index_rows() -> list[dict[str, Any]]:
    return [dict(row, tags=list(row.get("tags", []))) for row in COMMON_WEB_INDEX]


def _resolve_domains(site: str) -> tuple[list[str], list[str]]:
    ids = set(_split_csv(site))
    if not ids:
        return [], []

    domains: list[str] = []
    labels: list[str] = []
    for row in COMMON_WEB_INDEX:
        row_id = str(row.get("id", "")).lower()
        domain = str(row.get("domain", "")).lower()
        category = str(row.get("category", "")).lower()
        tags = {str(tag).lower() for tag in row.get("tags", [])}
        if row_id in ids or domain in ids or category in ids or ids.intersection(tags):
            if domain and domain not in domains:
                domains.append(domain)
            labels.append(row_id or domain)
    for item in ids:
        if "." in item and item not in domains:
            domains.append(item)
            labels.append(item)
    return domains, labels


def _search_index(query: str, max_results: int, site: str = "") -> dict[str, Any]:
    terms = set(re.findall(r"[a-zA-Z0-9_+#.-]+", query.lower()))
    domains, labels = _resolve_domains(site)
    rows: list[tuple[int, dict[str, Any]]] = []

    for row in COMMON_WEB_INDEX:
        domain = str(row.get("domain", "")).lower()
        if domains and domain not in domains:
            continue
        haystack = " ".join(
            [
                str(row.get("id", "")),
                str(row.get("name", "")),
                str(row.get("category", "")),
                str(row.get("domain", "")),
                str(row.get("description", "")),
                " ".join(str(tag) for tag in row.get("tags", [])),
            ]
        ).lower()
        score = sum(3 if term in domain else 1 for term in terms if term and term in haystack)
        if domains:
            score += 5
        if score > 0 or domains or not terms:
            rows.append((score, row))
            continue
        category = str(row.get("category", "")).lower()
        row_id = str(row.get("id", "")).lower()
        if category == "search":
            rows.append((0, row))
        elif row_id in {"github", "stackoverflow", "wikipedia", "reddit"}:
            rows.append((-1, row))

    rows.sort(key=lambda item: (-item[0], INDEX_PRIORITY.get(str(item[1].get("id", "")).lower(), 50), str(item[1].get("id", ""))))
    selected = [row for _, row in rows[:max_results]]
    results = [
        {
            "title": str(row.get("name", "")),
            "url": _format_search_url(str(row.get("search_url", "")), query),
            "content": str(row.get("description", "")),
            "source": "index",
            "domain": str(row.get("domain", "")),
            "category": str(row.get("category", "")),
        }
        for row in selected
    ]
    return {
        "ok": True,
        "source": "builtin:index",
        "query": query,
        "results": results,
        "index_table": selected,
        "site_filters": labels,
        "fallback_reason": "",
    }


def _normalize_tavily_answer_mode(value: str) -> bool | str:
    text = str(value or "").strip().lower()
    if text in {"advanced", "basic"}:
        return text
    return _parse_bool(text, default=False)


def _normalize_tavily_raw_mode(value: str) -> bool | str:
    text = str(value or "").strip().lower()
    if text in {"markdown", "text"}:
        return text
    return _parse_bool(text, default=False)


def _tavily_endpoint() -> str:
    explicit = str(os.environ.get("TAVILY_SEARCH_URL") or "").strip()
    if explicit:
        return explicit
    base = str(os.environ.get("TAVILY_BASE_URL") or "https://api.tavily.com").strip().rstrip("/")
    if base.endswith("/search"):
        return base
    return f"{base}/search"


def _http_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ.get('TAVILY_API_KEY', '').strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _tavily_search(
    query: str,
    *,
    max_results: int,
    topic: str,
    search_depth: str,
    time_range: str,
    include_answer: str,
    include_raw_content: str,
    site: str,
    exclude_domains: str,
    timeout: float,
) -> dict[str, Any]:
    api_key = str(os.environ.get("TAVILY_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "source": "tavily", "error": "TAVILY_API_KEY is not set"}

    topic_value = str(topic or "general").strip().lower()
    if topic_value not in {"general", "news", "finance"}:
        topic_value = "general"
    depth_value = str(search_depth or "basic").strip().lower()
    if depth_value not in {"basic", "advanced"}:
        depth_value = "basic"

    include_domains, labels = _resolve_domains(site)
    payload: dict[str, Any] = {
        "query": query,
        "topic": topic_value,
        "search_depth": depth_value,
        "max_results": max_results,
        "include_answer": _normalize_tavily_answer_mode(include_answer),
        "include_raw_content": _normalize_tavily_raw_mode(include_raw_content),
    }

    tr = str(time_range or "").strip().lower()
    if tr in {"day", "week", "month", "year", "d", "w", "m", "y"}:
        payload["time_range"] = tr
    if include_domains:
        payload["include_domains"] = include_domains
    excluded = _split_csv(exclude_domains)
    if excluded:
        payload["exclude_domains"] = excluded

    try:
        raw = _http_json(_tavily_endpoint(), payload, timeout)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "source": "tavily", "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "source": "tavily", "error": str(exc)}

    rows = raw.get("results", [])
    if not isinstance(rows, list):
        rows = []
    results: list[dict[str, Any]] = []
    for item in rows[:max_results]:
        if not isinstance(item, dict):
            continue
        result = {
            "title": _bounded_text(item.get("title"), 220),
            "url": str(item.get("url") or "").strip(),
            "content": _bounded_text(item.get("content") or item.get("raw_content") or ""),
            "source": "tavily",
        }
        if item.get("score") is not None:
            result["score"] = item.get("score")
        if item.get("published_date") is not None:
            result["published_date"] = item.get("published_date")
        results.append(result)

    return {
        "ok": True,
        "source": "tavily",
        "query": str(raw.get("query") or query),
        "answer": _bounded_text(raw.get("answer"), 1800) if raw.get("answer") else "",
        "results": results,
        "response_time": raw.get("response_time"),
        "request_id": raw.get("request_id"),
        "site_filters": labels,
        "fallback_reason": "",
    }


def _decode_duckduckgo_url(url: str) -> str:
    href = str(url or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        query = parse.parse_qs(parsed.query)
        target = query.get("uddg", [""])[0]
        if target:
            return target
    return href


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture: str = ""
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {str(k): str(v or "") for k, v in attrs}
        classes = set(str(attr.get("class", "")).split())
        if tag == "a" and "result__a" in classes:
            self._flush_current()
            self._current = {"url": _decode_duckduckgo_url(attr.get("href", ""))}
            self._capture = "title"
            self._buffer = []
            return
        if self._current is not None and ("result__snippet" in classes or "result__snippet" in str(attr.get("class", ""))):
            self._capture = "content"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._capture or self._current is None:
            return
        if self._capture == "title" and tag == "a":
            self._current["title"] = _bounded_text(" ".join(self._buffer), 220)
            self._capture = ""
            self._buffer = []
            return
        if self._capture == "content" and tag in {"a", "div"}:
            self._current["content"] = _bounded_text(" ".join(self._buffer))
            self._capture = ""
            self._buffer = []
            self._flush_current()

    def close(self) -> None:
        super().close()
        self._flush_current()

    def _flush_current(self) -> None:
        if len(self.results) >= self.limit:
            self._current = None
            return
        if not isinstance(self._current, dict):
            return
        url = str(self._current.get("url", "")).strip()
        title = str(self._current.get("title", "")).strip()
        if url and title and not any(row.get("url") == url for row in self.results):
            self._current.setdefault("content", "")
            self.results.append(dict(self._current))
        self._current = None


def _duckduckgo_search(query: str, *, max_results: int, site: str, timeout: float) -> dict[str, Any]:
    domains, labels = _resolve_domains(site)
    scoped_query = query
    if domains:
        scoped_query = f"{query} " + " ".join(f"site:{domain}" for domain in domains[:5])
    url = "https://duckduckgo.com/html/?" + parse.urlencode({"q": scoped_query})
    req = request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "source": "builtin:duckduckgo", "error": str(exc)}

    parser = _DuckDuckGoHTMLParser(max_results)
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        return {"ok": False, "source": "builtin:duckduckgo", "error": str(exc)}

    results = [
        {
            "title": row.get("title", ""),
            "url": row.get("url", ""),
            "content": row.get("content", ""),
            "source": "duckduckgo",
        }
        for row in parser.results[:max_results]
    ]
    if not results:
        return {"ok": False, "source": "builtin:duckduckgo", "error": "no parsed results"}
    return {
        "ok": True,
        "source": "builtin:duckduckgo",
        "query": query,
        "results": results,
        "site_filters": labels,
        "fallback_reason": "",
    }


def search_web(
    query: str,
    *,
    max_results: str = "5",
    source: str = "auto",
    site: str = "",
    topic: str = "general",
    search_depth: str = "basic",
    time_range: str = "",
    include_answer: str = "true",
    include_raw_content: str = "false",
    exclude_domains: str = "",
    timeout: str = "",
) -> dict[str, Any]:
    q = _normalize_query(query)
    limit = _parse_int(max_results, DEFAULT_MAX_RESULTS, 1, MAX_RESULTS)
    mode = str(source or "auto").strip().lower()
    if mode not in {"auto", "tavily", "builtin", "index"}:
        mode = "auto"
    try:
        timeout_seconds = float(str(timeout).strip()) if str(timeout or "").strip() else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    timeout_seconds = max(1.0, min(30.0, timeout_seconds))

    if mode == "index":
        return _search_index(q, limit, site=site)

    fallback_reason = ""
    if mode in {"auto", "tavily"}:
        tavily = _tavily_search(
            q,
            max_results=limit,
            topic=topic,
            search_depth=search_depth,
            time_range=time_range,
            include_answer=include_answer,
            include_raw_content=include_raw_content,
            site=site,
            exclude_domains=exclude_domains,
            timeout=timeout_seconds,
        )
        has_tavily_data = bool(tavily.get("answer")) or bool(tavily.get("results"))
        if tavily.get("ok") and has_tavily_data:
            return tavily
        fallback_reason = str(tavily.get("error") or "tavily returned no results")
        if mode == "tavily":
            tavily["fallback_reason"] = fallback_reason
            return tavily

    if mode in {"auto", "builtin"}:
        builtin = _duckduckgo_search(q, max_results=limit, site=site, timeout=timeout_seconds)
        if builtin.get("ok") and builtin.get("results"):
            if fallback_reason:
                builtin["fallback_reason"] = fallback_reason
            return builtin
        fallback_reason = str(builtin.get("error") or fallback_reason or "builtin search returned no results")

    indexed = _search_index(q, limit, site=site)
    indexed["fallback_reason"] = fallback_reason
    return indexed


def web_index_table(query: str = "", max_results: str = "30") -> dict[str, Any]:
    q = str(query or "").strip()
    limit = _parse_int(max_results, 30, 1, len(COMMON_WEB_INDEX))
    if q:
        return _search_index(_normalize_query(q), limit, site="")
    rows = _index_rows()[:limit]
    return {
        "ok": True,
        "source": "builtin:index",
        "query": "",
        "results": [],
        "index_table": rows,
        "fallback_reason": "",
    }
