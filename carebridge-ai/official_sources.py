"""Fetch public source pages, not predefined recommendations or eligibility data."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

_cache = []
_cached_at = 0
_lock = threading.Lock()


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _fetch(source):
    try:
        url = source['url']
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.gov.sg'):
            return None
        request = Request(url, headers={'User-Agent': 'CareBridge/1.0 (public assistance information)', 'Accept': 'text/html'})
        with urlopen(request, timeout=15) as response:
            final = urlparse(response.geturl())
            if final.scheme != 'https' or not (final.hostname or '').endswith('.gov.sg'):
                return None
            if 'text/html' not in response.headers.get('Content-Type', ''):
                return None
            html = response.read(2_000_000).decode('utf-8', errors='replace')
        parser = PageText()
        parser.feed(html)
        content = '\n'.join(parser.parts)
        if len(content) < 300:
            return None
        return {'title': source['title'], 'url': response.geturl(), 'content': content[:14000]}
    except (OSError, ValueError):
        return None


def fetch_official_sources():
    global _cache, _cached_at
    with _lock:
        if _cache and time.monotonic() - _cached_at < 3600:
            return [dict(source) for source in _cache]
        catalog = json.loads(Path(__file__).with_name('official_sources.json').read_text(encoding='utf-8'))
        with ThreadPoolExecutor(max_workers=6) as pool:
            sources = [source for source in pool.map(_fetch, catalog) if source]
        if sources:
            _cache, _cached_at = sources, time.monotonic()
        return [dict(source) for source in sources]
