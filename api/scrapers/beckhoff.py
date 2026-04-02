"""
scrapers/beckhoff.py — Beckhoff documentation scraper.

Strategy (in order):
  1. If caller provided a source_url, fetch that directly and harvest PDFs.
  2. Try the Beckhoff InfoSys documentation portal, which has consistent
     per-product URLs: https://infosys.beckhoff.com/content/1033/{pid}/index.html
  3. Try the Beckhoff main product page via pattern-matched URL.
  4. Fall back to site search: https://www.beckhoff.com/en-us/search/?q={pid}
     Parse the first product link and harvest PDFs from it.

Only PDFs are downloaded.  Link text is filtered for document-type keywords
so that firmware ZIPs, EtherCAT ESI files, etc. are skipped unless the
requested doc_type suggests firmware.

Doc-type mapping (inferred from link text):
  "manual"         — Manual, documentation, operating instructions
  "datasheet"      — Datasheet, data sheet, technical data
  "app_note"       — Application note, app note, example
  "firmware_notes" — Firmware, release notes, changelog, history
  "mounting"       — Mounting instructions, quick start
"""
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlencode

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, ScrapeResult, _TYPE_KEYWORDS, _ALL_KEYWORDS

log = logging.getLogger(__name__)

INFOSYS_BASE  = "https://infosys.beckhoff.com"
BECKHOFF_BASE = "https://www.beckhoff.com"


def _infer_doc_type(text: str, href: str) -> str:
    """Infer a doc_type string from link text + href."""
    combined = (text + " " + href).lower()
    for dtype, keywords in _TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return dtype
    return "manual"  # safe default for unrecognised PDF links


def _safe_filename(text: str, fallback: str, pid: str) -> str:
    """
    Build a clean filename from link text.  Keeps alphanumerics, dashes,
    underscores, and dots.  Falls back to ``{pid}_{fallback}.pdf``.
    """
    if text:
        clean = re.sub(r"[^\w\s\-.]", "", text).strip()
        clean = re.sub(r"\s+", "_", clean)
        if clean and not clean.lower().endswith(".pdf"):
            clean += ".pdf"
        if clean:
            return clean
    name = re.sub(r"[^\w\-.]", "_", fallback.split("?")[0].rstrip("/").split("/")[-1])
    if not name.lower().endswith(".pdf"):
        name = f"{pid}_{name}.pdf"
    return name or f"{pid}_doc.pdf"


class BeckhoffScraper(BaseScraper):

    # ── Public entry point ────────────────────────────────────────────────────

    async def scrape_product(
        self,
        manufacturer: str,
        product_id:   str,
        doc_type:     Optional[str] = None,
        source_url:   Optional[str] = None,
    ) -> list[ScrapeResult]:
        """
        Find and download Beckhoff documentation for *product_id*.

        Returns a list of :class:`ScrapeResult` — one per downloaded PDF.
        """
        pid      = product_id.strip().upper()
        dest_dir = self._dest_dir(manufacturer, pid)
        results: list[ScrapeResult] = []

        # 1. If caller provided a direct URL, harvest that.
        if source_url:
            log.info("Beckhoff: using provided URL for %s: %s", pid, source_url)
            found = await self._harvest_url(source_url, pid, dest_dir, doc_type)
            if found:
                return found

        # 2. InfoSys documentation portal (most reliable for EL/EK/EP/AX products).
        infosys_url = f"{INFOSYS_BASE}/content/1033/{pid.lower()}/index.html"
        log.info("Beckhoff: trying InfoSys for %s: %s", pid, infosys_url)
        found = await self._harvest_url(infosys_url, pid, dest_dir, doc_type)
        if found:
            results.extend(found)

        # 3. Main product page via site search (fallback).
        if not results:
            log.info("Beckhoff: InfoSys gave nothing, trying site search for %s", pid)
            found = await self._search_and_harvest(pid, dest_dir, doc_type)
            results.extend(found)

        return results

    # ── Internal methods ──────────────────────────────────────────────────────

    async def _harvest_url(
        self,
        url:      str,
        pid:      str,
        dest_dir: str,
        doc_type: Optional[str],
    ) -> list[ScrapeResult]:
        """Fetch *url*, find PDF links, download them."""
        resp = await self._get(url)
        if resp is None:
            return []

        content_type = resp.headers.get("content-type", "")
        # If the URL itself IS a PDF, download it directly.
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            filename  = _safe_filename("", url, pid)
            inferred  = doc_type or _infer_doc_type("", url)
            return [await self._download_file(url, dest_dir, filename, inferred)]

        soup    = self._soup(resp.text, url)
        results = await self._harvest_pdfs(soup, url, pid, dest_dir, doc_type)

        # If the page has no direct PDFs but has a "Downloads" section link,
        # follow it one hop deeper.
        if not results:
            for link_url, text in self._find_links_by_text(soup, url, [r"download", r"documentation"]):
                if link_url == url:
                    continue
                sub_resp = await self._get(link_url)
                if sub_resp is None:
                    continue
                sub_soup = self._soup(sub_resp.text, link_url)
                results.extend(await self._harvest_pdfs(sub_soup, link_url, pid, dest_dir, doc_type))
                if results:
                    break

        return results

    async def _harvest_pdfs(
        self,
        soup:     BeautifulSoup,
        base_url: str,
        pid:      str,
        dest_dir: str,
        doc_type: Optional[str],
    ) -> list[ScrapeResult]:
        """Find PDF links in *soup* and download each one."""
        # When a specific doc_type is requested, prefer its keywords.
        hints = _TYPE_KEYWORDS.get(doc_type or "", _ALL_KEYWORDS)
        links = self._find_pdf_links(soup, base_url, keyword_hints=hints)

        # If specific hints returned nothing, fall back to all PDFs.
        if not links and doc_type:
            links = self._find_pdf_links(soup, base_url)

        seen_urls: set[str] = set()
        results: list[ScrapeResult] = []
        for pdf_url, text in links:
            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)
            inferred = doc_type or _infer_doc_type(text, pdf_url)
            filename  = _safe_filename(text, pdf_url, pid)
            result    = await self._download_file(pdf_url, dest_dir, filename, inferred)
            results.append(result)
            if not result.success:
                log.warning("Beckhoff: failed to download %s: %s", pdf_url, result.error)

        return results

    async def _search_and_harvest(
        self,
        pid:      str,
        dest_dir: str,
        doc_type: Optional[str],
    ) -> list[ScrapeResult]:
        """
        Use the Beckhoff site search to locate the product page, then harvest
        PDFs from it.
        """
        params      = urlencode({"q": pid})
        search_url  = f"{BECKHOFF_BASE}/en-us/search/?{params}"
        resp        = await self._get(search_url)
        if resp is None:
            return []

        soup = self._soup(resp.text, search_url)

        # Find the first product-specific result link that contains the pid.
        product_url = None
        pid_lower   = pid.lower()
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            if pid_lower in href and "/products/" in href:
                product_url = urljoin(BECKHOFF_BASE, a["href"])
                break

        if not product_url:
            log.warning("Beckhoff: no product page found in search results for %s", pid)
            return []

        log.info("Beckhoff: found product page via search: %s", product_url)
        return await self._harvest_url(product_url, pid, dest_dir, doc_type)
