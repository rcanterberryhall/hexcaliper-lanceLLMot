"""
scrapers/base.py — Base web scraper for the technical document library.

Provides rate limiting, retry with exponential backoff, SHA-256 dedup,
and file storage layout.  Individual manufacturer scrapers subclass this.
"""
import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

import config
import db

log = logging.getLogger(__name__)

# ── Shared doc-type keyword vocabulary ───────────────────────────────────────
# Used by all scrapers to classify PDFs and to filter search results.
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "manual":         ["manual", "documentation", "operating instruction", "handbuch",
                       "user guide", "user manual", "instruction manual"],
    "datasheet":      ["datasheet", "data sheet", "technical data", "datenblatt",
                       "product information", "technical information", "specification"],
    "app_note":       ["application note", "app note", "application example",
                       "programming example", "sample", "application manual"],
    "firmware_notes": ["firmware", "release note", "changelog", "change log",
                       "revision history", "software note", "what's new"],
    "mounting":       ["mounting", "installation", "quick start", "quick guide",
                       "dimension drawing", "wiring", "getting started"],
}

# All doc-type keywords flattened — used when no specific type is requested.
_ALL_KEYWORDS = [kw for kws in _TYPE_KEYWORDS.values() for kw in kws]

# Seconds between consecutive requests to the same domain.
RATE_LIMIT_DELAY = 0.5   # 2 req/s max

# Maximum download size (30 MB — large firmware bundles can be big).
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024

# Retry configuration.
MAX_RETRIES    = 3
RETRY_BASE     = 2.0   # seconds — doubles each attempt

# Shared across all scrapers; keyed by domain → last request time.
_last_request: dict[str, float] = {}
_rate_lock = asyncio.Lock()


@dataclass
class ScrapeResult:
    url:      str
    filename: str
    filepath: str
    doc_type: str
    version:  Optional[str] = None
    checksum: Optional[str] = None
    success:  bool = True
    error:    Optional[str] = None


class BaseScraper:
    """
    Base scraper with shared HTTP behaviour.

    Subclasses must implement :meth:`scrape_product`.
    """

    USER_AGENT = "hexcaliper-library/1.0 (+https://hexcaliper.com)"

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _rate_limit(self, domain: str) -> None:
        """Enforce per-domain rate limiting before each request."""
        async with _rate_lock:
            last = _last_request.get(domain, 0.0)
            wait = RATE_LIMIT_DELAY - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request[domain] = time.monotonic()

    async def _get(
        self,
        url:     str,
        headers: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> Optional[httpx.Response]:
        """
        Fetch *url* with rate limiting and exponential-backoff retry.

        Returns the :class:`httpx.Response` on success, or ``None`` after all
        retries are exhausted.
        """
        domain = urlparse(url).netloc
        hdrs = {"User-Agent": self.USER_AGENT, **(headers or {})}

        for attempt in range(MAX_RETRIES):
            await self._rate_limit(domain)
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=timeout,
                ) as client:
                    resp = await client.get(url, headers=hdrs)
                    resp.raise_for_status()
                    return resp
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE ** attempt
                    log.warning("GET %s failed (attempt %d): %s — retry in %.1fs",
                                url, attempt + 1, exc, delay)
                    await asyncio.sleep(delay)
                else:
                    log.error("GET %s failed after %d attempts: %s", url, MAX_RETRIES, exc)
        return None

    # ── HTML parsing helpers ──────────────────────────────────────────────────

    def _soup(self, html: str, base_url: str = "") -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _find_pdf_links(
        self,
        soup: BeautifulSoup,
        base_url: str,
        keyword_hints: Optional[list[str]] = None,
    ) -> list[tuple[str, str]]:
        """
        Find all PDF links in *soup*, returning ``[(absolute_url, link_text)]``.

        When *keyword_hints* is provided, only links whose text (or href) contains
        at least one of the hints (case-insensitive) are returned.  Otherwise all
        PDF links are returned.
        """
        results = []
        hints = [h.lower() for h in (keyword_hints or [])]
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(strip=True)
            # Must be a PDF (by extension or content hint).
            is_pdf = href.lower().endswith(".pdf") or "pdf" in href.lower()
            if not is_pdf:
                continue
            # Optionally filter by keyword.
            if hints:
                combined = (href + " " + text).lower()
                if not any(h in combined for h in hints):
                    continue
            abs_url = urljoin(base_url, href)
            results.append((abs_url, text))
        return results

    def _find_links_by_text(
        self,
        soup: BeautifulSoup,
        base_url: str,
        patterns: list[str],
    ) -> list[tuple[str, str]]:
        """
        Find any links whose visible text matches one of the regex *patterns*.
        Returns ``[(absolute_url, text)]``.
        """
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        results = []
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if any(p.search(text) for p in compiled):
                abs_url = urljoin(base_url, a["href"].strip())
                results.append((abs_url, text))
        return results

    # ── Storage helpers ───────────────────────────────────────────────────────

    def _dest_dir(self, manufacturer: str, product_id: str) -> str:
        """
        Return (and create) the storage directory for a product's documents.

        Layout: ``{LIBRARY_PATH}/{manufacturer_slug}/{product_id_upper}/``
        """
        mfr_slug = manufacturer.strip().lower().replace(" ", "_")
        pid_upper = product_id.strip().upper()
        path = os.path.join(config.LIBRARY_PATH, mfr_slug, pid_upper)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _is_duplicate(self, checksum: str) -> bool:
        """Return True if this checksum is already in library_items."""
        with db.lock:
            items = db.list_library_items()
        return any(i.get("checksum") == checksum for i in items)

    async def _download_file(
        self,
        url:         str,
        dest_dir:    str,
        filename:    str,
        doc_type:    str,
        version:     Optional[str] = None,
    ) -> ScrapeResult:
        """
        Download *url* to ``dest_dir/filename``.

        Performs SHA-256 dedup: if the file content is already in the library
        (same checksum), the download is skipped and a success result is returned
        pointing to the existing file.
        """
        resp = await self._get(url, timeout=120.0)
        if resp is None:
            return ScrapeResult(
                url=url, filename=filename, filepath="", doc_type=doc_type,
                success=False, error="Download failed after retries",
            )

        if len(resp.content) > MAX_DOWNLOAD_BYTES:
            return ScrapeResult(
                url=url, filename=filename, filepath="", doc_type=doc_type,
                success=False, error=f"File too large ({len(resp.content) // 1024} KB)",
            )

        data     = resp.content
        checksum = self._sha256(data)

        if self._is_duplicate(checksum):
            log.info("Skipping duplicate: %s (checksum %s…)", filename, checksum[:8])
            # Find existing filepath.
            with db.lock:
                items = db.list_library_items()
            for item in items:
                if item.get("checksum") == checksum:
                    return ScrapeResult(
                        url=url, filename=item["filename"], filepath=item["filepath"],
                        doc_type=doc_type, version=version, checksum=checksum, success=True,
                    )

        filepath = os.path.join(dest_dir, filename)
        with open(filepath, "wb") as f:
            f.write(data)
        log.info("Downloaded: %s → %s (%d KB)", url, filepath, len(data) // 1024)

        return ScrapeResult(
            url=url, filename=filename, filepath=filepath,
            doc_type=doc_type, version=version, checksum=checksum, success=True,
        )

    # ── Subclass interface ────────────────────────────────────────────────────

    async def scrape_product(
        self,
        manufacturer: str,
        product_id:   str,
        doc_type:     Optional[str] = None,
        source_url:   Optional[str] = None,
    ) -> list[ScrapeResult]:
        """
        Discover and download documentation for one product.

        :param manufacturer: Manufacturer name (e.g. ``"Beckhoff"``).
        :param product_id:   Product identifier (e.g. ``"EL1008"``).
        :param doc_type:     Preferred doc type; scraper may return multiple types.
        :param source_url:   If the user provided a known URL, start there.
        :return:             List of :class:`ScrapeResult` (may be empty).
        """
        raise NotImplementedError
