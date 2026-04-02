"""
connectors/webdav.py — Generic REST / WebDAV connector.

Supports three authentication modes:
  none   — unauthenticated requests
  basic  — HTTP Basic (username + password)
  bearer — Authorization: Bearer {token}

The test_connection() method sends a WebDAV PROPFIND (Depth: 0) and falls back
to OPTIONS if the server returns 405 (not a WebDAV server).

Key operations:
  test_connection() — probe the server and return basic metadata
  list_files()      — WebDAV PROPFIND (Depth: 1) to list a directory
  download_file()   — HTTP GET a file at a given path
"""
import logging
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_TIMEOUT    = 30.0
_DL_TIMEOUT = 120.0

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:">'
    '<d:prop><d:displayname/><d:resourcetype/><d:getcontentlength/></d:prop>'
    '</d:propfind>'
)


class WebDAVError(Exception):
    pass


class WebDAVConnector:
    """
    Async HTTP client for a generic REST or WebDAV endpoint.

    :param url:        Base URL of the server (no trailing slash).
    :param username:   Username (Basic auth only).
    :param password:   Password / secret.
    :param auth_type:  ``'none'``, ``'basic'``, or ``'bearer'``.
    :param token:      Bearer token (``auth_type='bearer'`` only).
    :param verify_ssl: Whether to verify TLS certificates.
    """

    def __init__(
        self,
        url:        str,
        username:   str  = "",
        password:   str  = "",
        auth_type:  str  = "none",
        token:      str  = "",
        verify_ssl: bool = True,
    ) -> None:
        self._url        = url.strip().rstrip("/")
        self._username   = username
        self._password   = password
        self._auth_type  = auth_type.lower()
        self._token      = token
        self._verify_ssl = verify_ssl

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _extra_headers(self) -> dict:
        if self._auth_type == "bearer" and self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _auth(self) -> Optional[tuple]:
        if self._auth_type == "basic" and self._username:
            return (self._username, self._password)
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    async def test_connection(self) -> dict:
        """
        Probe the server with WebDAV PROPFIND (Depth: 0), falling back to
        OPTIONS for plain HTTP servers.  Returns basic server metadata.

        Raises :class:`WebDAVError` on authentication failure or unreachable host.
        """
        headers = {**self._extra_headers(), "Depth": "0", "Content-Type": "application/xml"}
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, verify=self._verify_ssl
        ) as client:
            resp = await client.request(
                "PROPFIND", self._url,
                headers=headers,
                auth=self._auth(),
                content=_PROPFIND_BODY.encode(),
            )

        if resp.status_code == 405:
            # Not a WebDAV server — fall back to OPTIONS.
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, verify=self._verify_ssl
            ) as client:
                resp = await client.options(
                    self._url,
                    headers=self._extra_headers(),
                    auth=self._auth(),
                )

        if resp.status_code in (401, 403):
            raise WebDAVError(f"Authentication failed ({resp.status_code}).")
        if not resp.is_success and resp.status_code != 207:
            raise WebDAVError(
                f"Server returned {resp.status_code}: {resp.text[:200]}"
            )

        return {
            "url":         self._url,
            "status_code": resp.status_code,
            "server":      resp.headers.get("server", ""),
            "dav":         resp.headers.get("dav", ""),
        }

    async def list_files(self, path: str = "/") -> list[dict]:
        """
        List files and folders at *path* via WebDAV PROPFIND (Depth: 1).

        :param path: Path relative to the base URL.
        :return:     List of ``{name, href, is_collection, size}`` dicts.
        """
        url     = self._url + path
        headers = {
            **self._extra_headers(),
            "Depth": "1",
            "Content-Type": "application/xml",
        }
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, verify=self._verify_ssl
        ) as client:
            resp = await client.request(
                "PROPFIND", url,
                headers=headers,
                auth=self._auth(),
                content=_PROPFIND_BODY.encode(),
            )
        if not resp.is_success and resp.status_code != 207:
            raise WebDAVError(f"PROPFIND {path} → {resp.status_code}")

        results: list[dict] = []
        try:
            root = ET.fromstring(resp.text)
            ns   = {"d": "DAV:"}
            for response in root.findall("d:response", ns):
                href    = response.findtext("d:href", "", ns)
                name    = (
                    response.findtext("d:propstat/d:prop/d:displayname", "", ns)
                    or href.rstrip("/").split("/")[-1]
                )
                is_coll = (
                    response.find(
                        "d:propstat/d:prop/d:resourcetype/d:collection", ns
                    ) is not None
                )
                size_str = response.findtext(
                    "d:propstat/d:prop/d:getcontentlength", "0", ns
                )
                results.append({
                    "name":          name,
                    "href":          href,
                    "is_collection": is_coll,
                    "size":          int(size_str) if size_str.isdigit() else 0,
                })
        except ET.ParseError:
            pass
        return results

    async def download_file(self, path: str) -> bytes:
        """
        Download the file at *path* (relative to the base URL).

        :raises WebDAVError: On a non-2xx response.
        """
        url = self._url + path
        async with httpx.AsyncClient(
            timeout=_DL_TIMEOUT,
            verify=self._verify_ssl,
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                url,
                headers=self._extra_headers(),
                auth=self._auth(),
            )
        if not resp.is_success:
            raise WebDAVError(f"Download failed ({resp.status_code}) for {path}")
        return resp.content


def from_config(cfg: dict) -> WebDAVConnector:
    """Build a :class:`WebDAVConnector` from a stored connection config dict."""
    return WebDAVConnector(
        url=cfg.get("url", ""),
        username=cfg.get("username", ""),
        password=cfg.get("password", ""),
        auth_type=cfg.get("auth_type", "none"),
        token=cfg.get("token", ""),
        verify_ssl=cfg.get("verify_ssl", True),
    )
