"""
connectors/sharepoint.py — Microsoft SharePoint via the Graph API.

Authentication uses the OAuth 2.0 client credentials flow:
  POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
  Subsequent calls include:  Authorization: Bearer {access_token}

The app registration in Azure must have the Sites.Read.All (or Files.Read.All)
application permission granted and admin-consented.

Key operations:
  test_connection() — authenticate and return site display name + ID
  list_files()      — list items in a document library folder
  download_file()   — download a file by its site-relative path
"""
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_TOKEN_URL  = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TIMEOUT    = 30.0
_DL_TIMEOUT = 120.0


class SharePointError(Exception):
    pass


class SharePointConnector:
    """
    Async wrapper around the Microsoft Graph API for SharePoint document access.

    :param tenant_id:     Azure Active Directory tenant ID (GUID or domain).
    :param client_id:     App registration (service principal) client ID.
    :param client_secret: App registration client secret.
    :param site_url:      Full SharePoint site URL
                          (e.g. ``https://myorg.sharepoint.com/sites/mysite``).
    """

    def __init__(
        self,
        tenant_id:     str,
        client_id:     str,
        client_secret: str,
        site_url:      str,
    ) -> None:
        self._tenant_id     = tenant_id.strip()
        self._client_id     = client_id.strip()
        self._client_secret = client_secret
        self._site_url      = site_url.strip().rstrip("/")
        self._token: Optional[str] = None

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _authenticate(self) -> str:
        """Obtain an OAuth2 access token via client credentials flow."""
        url  = _TOKEN_URL.format(tenant_id=self._tenant_id)
        data = {
            "grant_type":    "client_credentials",
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
            "scope":         "https://graph.microsoft.com/.default",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, data=data)
        if resp.status_code != 200:
            raise SharePointError(
                f"Authentication failed ({resp.status_code}): {resp.text[:200]}"
            )
        token = resp.json().get("access_token")
        if not token:
            raise SharePointError("Authentication response did not contain an access_token.")
        return token

    async def _headers(self) -> dict:
        if not self._token:
            self._token = await self._authenticate()
        return {"Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str) -> dict:
        """Authenticated GET against the Graph API; retries once on 401."""
        url     = f"{_GRAPH_BASE}{path}"
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 401:
            self._token = await self._authenticate()
            headers = {"Authorization": f"Bearer {self._token}"}
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=headers)
        if not resp.is_success:
            raise SharePointError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ── Site resolution ───────────────────────────────────────────────────────

    def _site_graph_path(self) -> str:
        """Return the Graph API path for this connector's site URL."""
        parsed    = urlparse(self._site_url)
        hostname  = parsed.netloc
        site_path = parsed.path.rstrip("/") or "/"
        return f"/sites/{hostname}:{site_path}"

    async def _get_site_id(self) -> str:
        data = await self._get(self._site_graph_path())
        return data["id"]

    # ── Public API ────────────────────────────────────────────────────────────

    async def test_connection(self) -> dict:
        """
        Authenticate and return SharePoint site information.
        Raises :class:`SharePointError` on failure.
        """
        data = await self._get(self._site_graph_path())
        return {
            "site_name": data.get("displayName", ""),
            "site_id":   data.get("id", ""),
            "web_url":   data.get("webUrl", ""),
        }

    async def list_files(
        self,
        folder_path: str = "",
        drive_id:    Optional[str] = None,
        limit:       int = 100,
    ) -> list[dict]:
        """
        List items in a SharePoint document library folder.

        :param folder_path: Folder path relative to drive root (empty = root).
        :param drive_id:    Drive ID; omit to use the site's default drive.
        :param limit:       Maximum items to return.
        """
        site_id = await self._get_site_id()
        if drive_id:
            base = f"/sites/{site_id}/drives/{drive_id}"
        else:
            base = f"/sites/{site_id}/drive"

        if folder_path:
            path = f"{base}/root:/{folder_path.strip('/')}:/children?$top={limit}"
        else:
            path = f"{base}/root/children?$top={limit}"

        data  = await self._get(path)
        items = data.get("value", [])
        return [
            {
                "id":      item.get("id"),
                "name":    item.get("name", ""),
                "size":    item.get("size", 0),
                "is_file": "file" in item,
                "web_url": item.get("webUrl", ""),
            }
            for item in items
        ]

    async def download_file(self, site_relative_path: str) -> bytes:
        """
        Download a file by its path within the site's default drive.

        :param site_relative_path: Path relative to drive root
                                   (e.g. ``/Shared Documents/manual.pdf``).
        """
        site_id = await self._get_site_id()
        item    = await self._get(
            f"/sites/{site_id}/drive/root:{site_relative_path}"
        )
        dl_url = item.get("@microsoft.graph.downloadUrl")
        if not dl_url:
            raise SharePointError(f"No download URL for {site_relative_path}")
        async with httpx.AsyncClient(timeout=_DL_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(dl_url)
        if not resp.is_success:
            raise SharePointError(
                f"Download failed ({resp.status_code}) for {site_relative_path}"
            )
        return resp.content


def from_config(cfg: dict) -> SharePointConnector:
    """Build a :class:`SharePointConnector` from a stored connection config dict."""
    return SharePointConnector(
        tenant_id=cfg.get("tenant_id", ""),
        client_id=cfg.get("client_id", ""),
        client_secret=cfg.get("client_secret", ""),
        site_url=cfg.get("site_url", ""),
    )
