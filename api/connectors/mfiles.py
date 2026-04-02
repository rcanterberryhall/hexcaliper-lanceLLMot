"""
connectors/mfiles.py — M-Files REST API connector.

Uses the M-Files Web Service (MFWS) REST API available on M-Files Server ≥ 2015.3.
All calls are async (httpx).  Authentication uses the M-Files session token approach:
  POST /REST/server/authenticationtokens  →  returns { Value: "<token>" }
  Subsequent calls include X-Authentication: <token>

Scope: read-only indexing by default.  Does not push documents unless explicitly called.

Key operations:
  test_connection()     — authenticate and return server info
  list_objects()        — list objects matching a search query
  get_object_files()    — get file list for an object
  download_file()       — download a specific file
"""
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 30.0
_DOWNLOAD_TIMEOUT = 120.0


class MFilesError(Exception):
    pass


class MFilesConnector:
    """
    Thin async wrapper around the M-Files Web Service REST API.

    :param host:     M-Files server hostname or IP (no scheme).
    :param vault:    Vault GUID (with or without braces).
    :param username: M-Files username.
    :param password: M-Files password.
    :param use_ssl:  Connect over HTTPS (default True).
    :param port:     Override default port (443 for HTTPS, 80 for HTTP).
    """

    def __init__(
        self,
        host:     str,
        vault:    str,
        username: str,
        password: str,
        use_ssl:  bool = True,
        port:     Optional[int] = None,
    ) -> None:
        scheme      = "https" if use_ssl else "http"
        default_port = 443 if use_ssl else 80
        port         = port or default_port
        self._base   = f"{scheme}://{host}:{port}/REST"
        self._vault  = vault.strip("{}").upper()
        self._user   = username
        self._pass   = password
        self._token: Optional[str] = None

    # ── Authentication ────────────────────────────────────────────────────────

    async def _authenticate(self) -> str:
        """Obtain an M-Files session token."""
        url     = f"{self._base}/server/authenticationtokens"
        payload = {
            "Username":    self._user,
            "Password":    self._pass,
            "VaultGuid":   f"{{{self._vault}}}",
            "SessionType": 1,   # 1 = permanent (token doesn't expire after 30 min idle)
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=True) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise MFilesError(
                f"Authentication failed ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        token = data.get("Value")
        if not token:
            raise MFilesError("Authentication response did not contain a token.")
        return token

    async def _token_headers(self) -> dict:
        if not self._token:
            self._token = await self._authenticate()
        return {"X-Authentication": self._token}

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Authenticated GET against the MFWS REST API."""
        url     = f"{self._base}{path}"
        headers = await self._token_headers()
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=True) as client:
            resp = await client.get(url, headers=headers, params=params or {})
        if resp.status_code == 401:
            # Token expired — re-authenticate once and retry.
            self._token = await self._authenticate()
            headers = {"X-Authentication": self._token}
            async with httpx.AsyncClient(timeout=_TIMEOUT, verify=True) as client:
                resp = await client.get(url, headers=headers, params=params or {})
        if not resp.is_success:
            raise MFilesError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ── Server info ───────────────────────────────────────────────────────────

    async def test_connection(self) -> dict:
        """
        Authenticate and return basic server + vault info.
        Raises :class:`MFilesError` on failure.
        """
        server_info = await self._get("/server")
        vault_info  = await self._get(f"/server/vaults/{{{self._vault}}}")
        return {
            "server_version": server_info.get("ServerVersion", {}).get("Display", "unknown"),
            "vault_name":     vault_info.get("Name", self._vault),
            "vault_guid":     self._vault,
        }

    # ── Object search ─────────────────────────────────────────────────────────

    async def search_objects(
        self,
        query:       str,
        object_type: Optional[int] = None,
        limit:       int = 50,
    ) -> list[dict]:
        """
        Full-text search for objects in the vault.

        :param query:       Search string.
        :param object_type: M-Files object type ID (0 = Document, None = all).
        :param limit:       Maximum results to return.
        :returns:           List of object summaries (id, title, object_type, version).
        """
        params: dict = {"q": query, "limit": limit}
        if object_type is not None:
            params["o"] = object_type
        data    = await self._get(f"/objects", params=params)
        items   = data.get("Items") or []
        results = []
        for item in items:
            results.append({
                "id":          item.get("ObjVer", {}).get("ID"),
                "version":     item.get("ObjVer", {}).get("Version"),
                "object_type": item.get("ObjVer", {}).get("Type"),
                "title":       item.get("Title", ""),
                "guid":        item.get("ObjectGUID", ""),
            })
        return results

    async def list_objects(
        self,
        object_type: int = 0,
        limit:       int = 100,
        offset:      int = 0,
    ) -> list[dict]:
        """
        List objects of a given type (default: Documents).
        Useful for bulk indexing.
        """
        params = {
            "o":       object_type,
            "p":       offset,
            "limit":   limit,
            "include": "properties",
        }
        data  = await self._get("/objects", params=params)
        items = data.get("Items") or []
        return [
            {
                "id":          i.get("ObjVer", {}).get("ID"),
                "version":     i.get("ObjVer", {}).get("Version"),
                "object_type": i.get("ObjVer", {}).get("Type"),
                "title":       i.get("Title", ""),
                "guid":        i.get("ObjectGUID", ""),
            }
            for i in items
        ]

    # ── File operations ───────────────────────────────────────────────────────

    async def get_object_files(self, object_type: int, object_id: int, version: int = 0) -> list[dict]:
        """
        List files attached to an object.

        :param version: Use 0 for latest.
        :returns:       List of {file_id, name, extension, size}.
        """
        ver_str = str(version) if version else "latest"
        data    = await self._get(f"/objects/{object_type}/{object_id}/{ver_str}/files")
        files   = data if isinstance(data, list) else data.get("Files") or []
        return [
            {
                "file_id":   f.get("ID"),
                "name":      f.get("Name", ""),
                "extension": f.get("Extension", ""),
                "size":      f.get("LogicalSize", 0),
            }
            for f in files
        ]

    async def download_file(
        self,
        object_type: int,
        object_id:   int,
        file_id:     int,
        version:     int = 0,
    ) -> bytes:
        """
        Download the binary content of a single file.
        """
        ver_str = str(version) if version else "latest"
        url     = f"{self._base}/objects/{object_type}/{object_id}/{ver_str}/files/{file_id}/content"
        headers = await self._token_headers()
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, verify=True) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 401:
            self._token = await self._authenticate()
            headers = {"X-Authentication": self._token}
            async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, verify=True) as client:
                resp = await client.get(url, headers=headers)
        if not resp.is_success:
            raise MFilesError(
                f"Download failed ({resp.status_code}) for object {object_id} file {file_id}"
            )
        return resp.content


def from_config(cfg: dict) -> MFilesConnector:
    """Build an :class:`MFilesConnector` from a stored connection config dict."""
    return MFilesConnector(
        host=cfg.get("host", ""),
        vault=cfg.get("vault", ""),
        username=cfg.get("username", ""),
        password=cfg.get("password", ""),
        use_ssl=cfg.get("use_ssl", True),
        port=cfg.get("port") or None,
    )
