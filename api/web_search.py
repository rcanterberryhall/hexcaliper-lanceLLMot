"""
web_search.py — DuckDuckGo web-search integration.

Performs a synchronous HTML scrape of DuckDuckGo and returns structured
search results (title, URL, snippet) that can be injected into the chat
prompt as grounding context.
"""

import logging

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Maximum number of search results to return per query.
MAX_RESULTS = 5
# Maximum characters kept per result snippet to limit prompt size.
MAX_SNIPPET_CHARS = 400

# Browser-like headers to avoid being blocked by DuckDuckGo.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Search ─────────────────────────────────────────────────────

def search(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """
    Query DuckDuckGo and return structured search results.

    Performs a synchronous GET against the DuckDuckGo HTML interface and
    parses the response with BeautifulSoup.  Each result is a dict with
    the keys ``title``, ``url``, and ``snippet``.

    :param query: The search query string.
    :type query: str
    :param max_results: Maximum number of results to return.
    :type max_results: int
    :return: A list of result dicts, or an empty list if the request fails.
    :rtype: list[dict]
    """
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, headers=_HEADERS) as client:
            resp = client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
        if not resp.is_success:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for result in soup.select(".result__body"):
            title_el = result.select_one(".result__title a")
            snippet_el = result.select_one(".result__snippet")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            url = title_el.get("href", "")
            snippet = snippet_el.get_text(strip=True)[:MAX_SNIPPET_CHARS] if snippet_el else ""
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= max_results:
                break
        return results
    except Exception as exc:
        log.warning("web search failed: %s", exc)
        return []


def format_results(results: list[dict]) -> str:
    """
    Format a list of search result dicts into a human-readable string.

    Each result is rendered as a numbered entry with its title, URL, and
    snippet, separated by blank lines.  Used to build the context block
    injected into the chat prompt.

    :param results: A list of result dicts as returned by :func:`search`.
    :type results: list[dict]
    :return: A formatted multi-line string, or ``"No results found."`` if
             *results* is empty.
    :rtype: str
    """
    if not results:
        return "No results found."
    parts = []
    for i, r in enumerate(results, 1):
        parts.append(f"[{i}] {r['title']}\nURL: {r['url']}\n{r['snippet']}")
    return "\n\n".join(parts)
