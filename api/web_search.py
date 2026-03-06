import httpx
from bs4 import BeautifulSoup

MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 400

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def search(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """Return a list of {title, url, snippet} dicts from DuckDuckGo HTML search."""
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
    except Exception:
        return []


def format_results(results: list[dict]) -> str:
    if not results:
        return "No results found."
    parts = []
    for i, r in enumerate(results, 1):
        parts.append(f"[{i}] {r['title']}\nURL: {r['url']}\n{r['snippet']}")
    return "\n\n".join(parts)
