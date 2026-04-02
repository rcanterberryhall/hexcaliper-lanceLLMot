"""
scrapers/allen_bradley.py — Allen Bradley / Rockwell Automation documentation scraper.

Strategy (in order):
  1. If caller provided a source_url, harvest that directly.
  2. Search the Rockwell literature library, which indexes most published manuals:
     https://literature.rockwellautomation.com/idc/groups/literature/documents/
     The library search endpoint returns HTML with direct PDF links.
  3. Fall back to the main site search:
     https://www.rockwellautomation.com/en-us/search.html?q={product_id}
     Parse the first Document result link and harvest from there.

Note: Some Rockwell documents require an account login (TechConnect) — these will
fail at download time with a 4xx and be reported as file errors.  The user can
then paste the direct URL after downloading manually.

Doc-type keywords follow the same conventions as the rest of the scraper package.
"""
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlencode

from scrapers.base import BaseScraper, ScrapeResult, _TYPE_KEYWORDS, _ALL_KEYWORDS

log = logging.getLogger(__name__)

RA_LITERATURE_BASE = "https://literature.rockwellautomation.com"
RA_MAIN_BASE       = "https://www.rockwellautomation.com"

# Rockwell publication number patterns appear in filenames and link text.
# e.g. "1756-UM001", "520-UM001", "LOGIX-UM001"
_PUB_PATTERN = re.compile(r"\b\d{3,4}-[A-Z]{2}\d{3}\b|\b[A-Z]+-[A-Z]{2}\d{3}\b", re.IGNORECASE)


def _infer_doc_type(text: str, href: str) -> str:
    combined = (text + " " + href).lower()
    for dtype, keywords in _TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return dtype
    # Rockwell suffix conventions: UM=user manual, RM=reference, IN=install, QS=quick start
    href_lower = href.lower()
    if any(s in href_lower for s in ("-um", "_um", "user-manual", "user_manual")):
        return "manual"
    if any(s in href_lower for s in ("-rm", "_rm", "reference")):
        return "manual"
    if any(s in href_lower for s in ("-in", "_in", "install")):
        return "mounting"
    if any(s in href_lower for s in ("-qs", "_qs", "quick")):
        return "mounting"
    if any(s in href_lower for s in ("-td", "_td", "tech-data", "spec")):
        return "datasheet"
    return "manual"


def _safe_filename(text: str, url: str, pid: str) -> str:
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    if not name.lower().endswith(".pdf"):
        clean = re.sub(r"[^\w\-.]", "_", text or pid)
        name = f"{clean}.pdf"
    return name


class AllenBradleyScraper(BaseScraper):

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

        # 1. Direct URL provided by user.
        if source_url:
            log.info("Allen Bradley: using provided URL for %s", pid)
            found = await self._harvest_url(source_url, pid, dest_dir, doc_type)
            if found:
                return found

        # 2. Rockwell literature library search.
        log.info("Allen Bradley: trying literature library for %s", pid)
        found = await self._search_literature(pid, dest_dir, doc_type)
        if found:
            results.extend(found)

        # 3. Main site search fallback.
        if not results:
            log.info("Allen Bradley: trying main site search for %s", pid)
            found = await self._search_main_site(pid, dest_dir, doc_type)
            results.extend(found)

        return results

    async def _search_literature(
        self, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        """Search the Rockwell literature library for the product."""
        params = urlencode({"q": pid, "search_type": "all"})
        url    = f"{RA_LITERATURE_BASE}/search?{params}"
        resp   = await self._get(url)
        if resp is None:
            return []
        soup  = self._soup(resp.text, url)
        return await self._harvest_pdfs(soup, url, pid, dest_dir, doc_type)

    async def _search_main_site(
        self, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        """Search rockwellautomation.com and follow the first document result."""
        params = urlencode({"q": pid})
        url    = f"{RA_MAIN_BASE}/en-us/search.html?{params}"
        resp   = await self._get(url)
        if resp is None:
            return []
        soup = self._soup(resp.text, url)

        # Find first link that looks like a product or document page.
        pid_lower = pid.lower()
        for a in soup.find_all("a", href=True):
            href  = a["href"]
            hlower = href.lower()
            if pid_lower in hlower and ("/products/" in hlower or "/literature/" in hlower):
                target = urljoin(RA_MAIN_BASE, href)
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
        soup = self._soup(resp.text, url)
        results = await self._harvest_pdfs(soup, url, pid, dest_dir, doc_type)
        if not results:
            for link_url, text in self._find_links_by_text(soup, url, [r"download", r"document", r"manual"]):
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
