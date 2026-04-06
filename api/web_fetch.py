"""
web_fetch.py — URL extraction and page-content fetching.

Detects HTTP/HTTPS URLs in a user message, fetches each page, strips
boilerplate HTML (scripts, nav, footer), and returns trimmed plain text
so the LLM has useful inline context without being overwhelmed.
"""

import logging
import re

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Matches any http/https URL that is at least 4 characters long after the scheme.
_URL_RE = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]{4,}")

# Maximum characters returned per fetched page.
MAX_CHARS = 3000
# Maximum number of URLs to fetch from a single message.
MAX_URLS = 3

# System CA bundle used to verify TLS certificates inside the container.
_SSL_CA = "/etc/ssl/certs/ca-certificates.crt"


# ── Helpers ────────────────────────────────────────────────────

def extract_urls(text: str) -> list[str]:
    """
    Extract up to MAX_URLS HTTP/HTTPS URLs from a plain-text string.

    :param text: The text to scan for URLs.
    :type text: str
    :return: A list of matched URL strings, capped at MAX_URLS.
    :rtype: list[str]
    """
    return _URL_RE.findall(text)[:MAX_URLS]


async def fetch_url(url: str) -> str | None:
    """
    Fetch the content of a single URL and return it as plain text.

    For HTML pages, navigational chrome (``<script>``, ``<style>``, ``<nav>``,
    ``<footer>``, ``<aside>``) is stripped before extracting readable text.
    Non-HTML responses (JSON, plain text, etc.) are returned as-is.
    The result is truncated to MAX_CHARS to keep prompt sizes manageable.

    :param url: The URL to retrieve.
    :type url: str
    :return: Trimmed page text, or ``None`` if the request fails or returns
             a non-success HTTP status.
    :rtype: str | None
    """
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, verify=_SSL_CA
        ) as client:
            resp = await client.get(
                url, headers={"User-Agent": "Mozilla/5.0 Hexcaliper/1.0"}
            )
        if not resp.is_success:
            return None
        ct = resp.headers.get("content-type", "")
        if "html" in ct:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Remove non-content elements to reduce noise in the extracted text.
            for tag in soup(["script", "style", "nav", "footer", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
        else:
            text = resp.text
        return text[:MAX_CHARS]
    except Exception as exc:
        log.warning("fetch failed: %s", exc)
        return None


async def fetch_context(message: str) -> dict[str, str]:
    """
    Fetch page content for every URL found in a chat message.

    Extracts URLs from *message*, fetches each one concurrently (up to
    MAX_URLS), and returns a mapping of ``{url: page_text}`` for URLs that
    were successfully retrieved.

    :param message: The raw user message that may contain URLs.
    :type message: str
    :return: A dict mapping each successfully fetched URL to its trimmed
             page text.
    :rtype: dict[str, str]
    """
    results: dict[str, str] = {}
    for url in extract_urls(message):
        content = await fetch_url(url)
        if content:
            results[url] = content
    return results
