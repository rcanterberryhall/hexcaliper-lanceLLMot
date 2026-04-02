"""
scrapers/siemens.py — Siemens documentation scraper.

Strategy (in order):
  1. If caller provided a source_url, harvest that directly.
  2. Search the Siemens Industry Support portal (cache.industry.siemens.com).
     Many manuals and datasheets are indexed here by article number.
  3. Fall back to the general support.industry.siemens.com search.

Note: Some Siemens documents require a Siemens ID login — these will fail
at download time with a redirect to login.  The user can paste the direct
URL after downloading manually.
"""
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlencode

from scrapers.base import BaseScraper, ScrapeResult, _TYPE_KEYWORDS, _ALL_KEYWORDS

log = logging.getLogger(__name__)

INDUSTRY_CACHE  = "https://cache.industry.siemens.com"
INDUSTRY_SUPPORT = "https://support.industry.siemens.com"


def _infer_doc_type(text: str, href: str) -> str:
    combined = (text + " " + href).lower()
    for dtype, keywords in _TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return dtype
    # Siemens naming: BA=operating instructions, GSD=device description
    href_lower = href.lower()
    if any(s in href_lower for s in ("_ba", "-ba_", "operating")):
        return "manual"
    if any(s in href_lower for s in ("_gsd", "device-description")):
        return "datasheet"
    return "manual"


def _safe_filename(text: str, url: str, pid: str) -> str:
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    if not name.lower().endswith(".pdf"):
        clean = re.sub(r"[^\w\-.]", "_", text or pid)
        name = f"{clean}.pdf"
    return name


class SiemensScraper(BaseScraper):

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
            log.info("Siemens: using provided URL for %s", pid)
            found = await self._harvest_url(source_url, pid, dest_dir, doc_type)
            if found:
                return found

        # 2. Industry cache search.
        log.info("Siemens: trying industry cache for %s", pid)
        found = await self._search_cache(pid, dest_dir, doc_type)
        if found:
            results.extend(found)

        # 3. Support portal search fallback.
        if not results:
            log.info("Siemens: trying support portal for %s", pid)
            found = await self._search_support(pid, dest_dir, doc_type)
            results.extend(found)

        return results

    async def _search_cache(
        self, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        """Try the Siemens industry cache — direct document URLs often work here."""
        # The cache stores docs under /dl_center/files/… — search via support portal first
        params = urlencode({"q": pid, "scope": "all", "lang": "en"})
        url    = f"{INDUSTRY_SUPPORT}/cs/search?{params}"
        resp   = await self._get(url)
        if resp is None:
            return []
        soup = self._soup(resp.text, url)
        return await self._harvest_pdfs(soup, url, pid, dest_dir, doc_type)

    async def _search_support(
        self, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        """Search support.industry.siemens.com for the product."""
        params = urlencode({"searchTerm": pid, "lang": "en"})
        url    = f"{INDUSTRY_SUPPORT}/cs/ww/en/sc/2067?{params}"
        resp   = await self._get(url)
        if resp is None:
            return []
        soup = self._soup(resp.text, url)

        # Follow the first product/manual page link.
        pid_lower = pid.lower()
        for a in soup.find_all("a", href=True):
            href   = a["href"]
            hlower = href.lower()
            if pid_lower in hlower and ("/cs/" in hlower or "/dl_center/" in hlower):
                target = urljoin(INDUSTRY_SUPPORT, href)
                found  = await self._harvest_url(target, pid, dest_dir, doc_type)
                if found:
                    return found
        return []

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
                soup, url, [r"download", r"manual", r"documentation"]
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
