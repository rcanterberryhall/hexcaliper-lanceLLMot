"""
scrapers/phoenix_contact.py — Phoenix Contact documentation scraper.

Strategy (in order):
  1. If caller provided a source_url, harvest that directly.
  2. Try the Phoenix Contact product page directly:
     https://www.phoenixcontact.com/en-us/products/{product_id}
     Product pages typically list documentation links.
  3. Try the documentation sub-page:
     https://www.phoenixcontact.com/en-us/products/{product_id}/documentation
  4. Fall back to site search:
     https://www.phoenixcontact.com/en-us/search?query={product_id}
"""
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlencode

from scrapers.base import BaseScraper, ScrapeResult, _TYPE_KEYWORDS, _ALL_KEYWORDS

log = logging.getLogger(__name__)

PC_BASE = "https://www.phoenixcontact.com"


def _infer_doc_type(text: str, href: str) -> str:
    combined = (text + " " + href).lower()
    for dtype, keywords in _TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return dtype
    return "manual"


def _safe_filename(text: str, url: str, pid: str) -> str:
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    if not name.lower().endswith(".pdf"):
        clean = re.sub(r"[^\w\-.]", "_", text or pid)
        name = f"{clean}.pdf"
    return name


class PhoenixContactScraper(BaseScraper):

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
            log.info("Phoenix Contact: using provided URL for %s", pid)
            found = await self._harvest_url(source_url, pid, dest_dir, doc_type)
            if found:
                return found

        # 2. Product page.
        log.info("Phoenix Contact: trying product page for %s", pid)
        product_url = f"{PC_BASE}/en-us/products/{pid}"
        found = await self._harvest_url(product_url, pid, dest_dir, doc_type)
        if found:
            results.extend(found)

        # 3. Documentation sub-page.
        if not results:
            log.info("Phoenix Contact: trying documentation sub-page for %s", pid)
            doc_url = f"{PC_BASE}/en-us/products/{pid}/documentation"
            found   = await self._harvest_url(doc_url, pid, dest_dir, doc_type)
            results.extend(found)

        # 4. Site search fallback.
        if not results:
            log.info("Phoenix Contact: falling back to site search for %s", pid)
            found = await self._search_site(pid, dest_dir, doc_type)
            results.extend(found)

        return results

    async def _search_site(
        self, pid: str, dest_dir: str, doc_type: Optional[str]
    ) -> list[ScrapeResult]:
        params = urlencode({"query": pid})
        url    = f"{PC_BASE}/en-us/search?{params}"
        resp   = await self._get(url)
        if resp is None:
            return []
        soup     = self._soup(resp.text, url)
        pid_lower = pid.lower()
        for a in soup.find_all("a", href=True):
            href   = a["href"]
            hlower = href.lower()
            if pid_lower in hlower and "/products/" in hlower:
                target = urljoin(PC_BASE, href)
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
                soup, url, [r"download", r"document", r"manual"]
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
