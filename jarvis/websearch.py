"""Web search answering: search -> fetch top pages in parallel -> extract main text -> LLM synthesis.

Search backends: duckduckgo (ddgs library, no API key) or a SearXNG instance (JSON API).
Results are cached for a few minutes. No internet -> graceful message.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Source:
    title: str
    url: str
    snippet: str = ""
    text: str = ""


@dataclass
class WebAnswer:
    answer: str
    sources: list[Source] = field(default_factory=list)
    ok: bool = True


SYNTH_SYSTEM = (
    "Ты голосовой ассистент. Тебе дали выдержки из веб-страниц. Ответь на вопрос пользователя своими словами, "
    "по-русски, разговорно и коротко (2-4 предложения), в своём обычном стиле. Не зачитывай тексты страниц, "
    "не перечисляй сайты и ссылки, без markdown. Если источники противоречат друг другу или данных нет — так и скажи."
)


class WebSearch:
    def __init__(self, cfg: dict, llm=None):
        self.cfg = cfg or {}
        self.llm = llm
        self.backend = self.cfg.get("backend", "duckduckgo")     # duckduckgo | searxng
        self.searxng_url = str(self.cfg.get("searxng_url", "")).rstrip("/")
        self.region = self.cfg.get("region", "ru-ru")
        self.max_results = int(self.cfg.get("max_results", 5))
        self.fetch_pages = int(self.cfg.get("fetch_pages", 4))
        self.timeout = float(self.cfg.get("timeout_s", 8))
        self.cache_ttl = float(self.cfg.get("cache_minutes", 15)) * 60
        self._cache: dict[str, tuple[float, WebAnswer]] = {}

    # ------------------------------------------------------------ search
    def search(self, query: str) -> list[Source]:
        if self.backend == "searxng" and self.searxng_url:
            import httpx
            r = httpx.get(f"{self.searxng_url}/search", params={"q": query, "format": "json", "language": "ru"},
                          timeout=self.timeout)
            r.raise_for_status()
            return [Source(x.get("title", ""), x.get("url", ""), x.get("content", ""))
                    for x in r.json().get("results", [])[: self.max_results]]
        try:
            from ddgs import DDGS
        except ImportError:  # older package name
            from duckduckgo_search import DDGS  # type: ignore[no-redef]
        res = DDGS().text(query, region=self.region, max_results=self.max_results) or []
        return [Source(x.get("title", ""), x.get("href") or x.get("url", ""), x.get("body", "")) for x in res]

    def _fetch(self, src: Source) -> Source:
        try:
            import httpx
            r = httpx.get(src.url, timeout=self.timeout, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36",
                                   "Accept-Language": "ru,en;q=0.8"})
            if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
                return src
            html = r.text
            text = ""
            try:
                import trafilatura
                text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
            except Exception as e:
                log.debug("trafilatura: %s", e)
            if not text:   # crude fallback
                text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
                text = re.sub(r"<[^>]+>", " ", text)
            src.text = re.sub(r"\s+", " ", text).strip()[:4000]
        except Exception as e:
            log.debug("fetch %s: %s", src.url, e)
        return src

    def gather(self, query: str) -> list[Source]:
        sources = [s for s in self.search(query) if s.url.startswith("http")]
        top = sources[: self.fetch_pages]
        with cf.ThreadPoolExecutor(max_workers=max(1, len(top))) as ex:
            list(ex.map(self._fetch, top, timeout=self.timeout * 2))
        return sources

    # ------------------------------------------------------------ answer
    def answer(self, question: str, context: str = "") -> WebAnswer:
        key = (context + "|" + question).strip().lower()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_ttl:
            return hit[1]
        try:
            sources = self.gather(question if not context else f"{context} {question}")
        except Exception as e:
            log.warning("Веб-поиск: %s", e)
            return WebAnswer("Не могу выйти в интернет, поиск сейчас недоступен.", [], ok=False)
        if not sources:
            return WebAnswer("Ничего толкового не нашёл.", [], ok=False)
        blocks = []
        for i, s_ in enumerate(sources[: self.fetch_pages + 1], 1):
            body = s_.text or s_.snippet
            if body:
                blocks.append(f"[{i}] {s_.title}\n{body[:2500]}")
        user = (f"Вопрос: {question}\n" + (f"Контекст: {context}\n" if context else "") +
                "\nВыдержки из найденных страниц:\n\n" + "\n\n".join(blocks))
        if self.llm is None:
            return WebAnswer(sources[0].snippet or sources[0].title, sources)
        try:
            text = self.llm.complete([{"role": "system", "content": f"{getattr(self.llm, 'system_prompt', '')}\n\n{SYNTH_SYSTEM}"},
                                      {"role": "user", "content": user}], num_ctx=8192, temperature=0.4)
        except Exception as e:
            log.warning("Синтез ответа: %s", e)
            text = sources[0].snippet or "Нашёл источники, но не смог их обработать."
        ans = WebAnswer(text, sources)
        self._cache[key] = (time.time(), ans)
        return ans
