"""
scrapers/danfoss.py — Danfoss documentation scraper.

Strategy (in order):
  1. If caller provided a source_url, harvest that directly.
  2. Search the Danfoss documentation site (files.danfoss.com) for the product ID.
  3. Fall back to the main Danfoss search:
     https://www.danfoss.com/en/search/?query={product_id}
     Follow the first product page and harvest PDFs.
"""
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlencode

from scrapers.base import BaseScraper, ScrapeResult, _TYPE_KEYWORDS, _ALL_KEYWORDS

log = logging.getLogger(__name__)

DANFOSS_FILES  = "https://files.danfoss.com"
DANFOSS_MAIN   = "https://www.danfoss.com"


def _infer_doc_type(text: str, href: str) -> str:
    combined = (text + " " + href).lower()
    for dtype, keywords in _TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return dtype
    # Danfoss document codes: MI=installation, MG=general, BC=app note
    href_lower = href.lower()
    if re.search(r"\b(mi|mg)\d", href_lower):
        return "mounting" if "mi" in href_lower else "manual"
    if re.search(r"\bbc\d", href_lower):
        return "app_note"
    return "manual"


def _safe_filename(text: str, url: str, pid: str) -> str:
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    if not name.lower().endswith(".pdf"):
        clean = re.sub(r"[^\w\-.]", "_", text or pid)
        name = f"{clean}.pdf"
    return name


class DanfossScraper(BaseScraper):

    async def scrape_product(
        self,
        manufacturer: str,
        product_id:   str,
        doc_type:     Optional[str] = None,
        source_url:   Optional[str] = None,
    ) -> list[ScrapeResult]:
        pid      = product_id.strip().upper()
        dest_dir = self._dest_dir(manufacturer, pid)
        results: list[ScrapeResult] = []

        # 1. Direct URL.
        if source_url:
            log.info("Danfoss: using provided URL for %s", pid)
            found = await self._harvest_url(source_url, pid, dest_dir, doc_type)
            if found:
                return found

        # 2. files.danfoss.com search.
        log.info("Danfoss: trying files.danfoss.com for %s", pid)
        found = await self._search_files(pid, dest_dir, doc_type)
        if found:
            results.extend(found)

        # 3. Main site search fallback.
        if not results:
            log.info("Danfoss: falling back to main site search for %s", pid)
            found = await self._search_main(pid, dest_dir, doc_type)
            results.extend(found)

        return results

    async def _search_files(
        self, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        """Search the Danfoss files server directly."""
        params = urlencode({"q": pid})
        url    = f"{DANFOSS_FILES}/search?{params}"
        resp   = await self._get(url)
        if resp is None:
            return []
        soup = self._soup(resp.text, url)
        return await self._harvest_pdfs(soup, url, pid, dest_dir, doc_type)

    async def _search_main(
        self, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        params = urlencode({"query": pid})
        url    = f"{DANFOSS_MAIN}/en/search/?{params}"
        resp   = await self._get(url)
        if resp is None:
            return []
        soup     = self._soup(resp.text, url)
        pid_lower = pid.lower()

        # Try to follow first product page link.
        for a in soup.find_all("a", href=True):
            href   = a["href"]
            hlower = href.lower()
            if pid_lower in hlower and ("/products/" in hlower or "/drives/" in hlower):
                target = urljoin(DANFOSS_MAIN, href)
                found  = await self._harvest_url(target, pid, dest_dir, doc_type)
                if found:
                    return found

        # Harvest PDFs directly from search results page.
        return await self._harvest_pdfs(soup, url, pid, dest_dir, doc_type)

    async def _harvest_url(
        self, url: str, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        resp = await self._get(url)
        if resp is None:
            return []
        ct = resp.headers.get("content-type", "")
        if "pdf" in ct or url.lower().endswith(".pdf"):
            filename = _safe_filename("", url, pid)
            dtype    = doc_type or _infer_doc_type("", url)
            return [await self._download_file(url, dest_dir, filename, dtype)]
        soup    = self._soup(resp.text, url)
        results = await self._harvest_pdfs(soup, url, pid, dest_dir, doc_type)
        if not results:
            for link_url, text in self._find_links_by_text(
                soup, url, [r"download", r"document", r"manual", r"literature"]
            ):
                if link_url == url:
                    continue
                sub = await self._get(link_url)
                if sub is None:
                    continue
                results.extend(await self._harvest_pdfs(
                    self._soup(sub.text, link_url), link_url, pid, dest_dir, doc_type
                ))
                if results:
                    break
        return results

    async def _harvest_pdfs(
        self, soup, base_url: str, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        hints = _TYPE_KEYWORDS.get(doc_type or "", _ALL_KEYWORDS)
        links = self._find_pdf_links(soup, base_url, keyword_hints=hints)
        if not links and doc_type:
            links = self._find_pdf_links(soup, base_url)
        seen: set[str] = set()
        results: list[ScrapeResult] = []
        for pdf_url, text in links:
            if pdf_url in seen:
                continue
            seen.add(pdf_url)
            dtype    = doc_type or _infer_doc_type(text, pdf_url)
            filename = _safe_filename(text, pdf_url, pid)
            results.append(await self._download_file(pdf_url, dest_dir, filename, dtype))
        return results
