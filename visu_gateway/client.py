"""Async JSON-RPC client for IP-Symcon.

IP-Symcon exposes every PHP function over a JSON-RPC 2.0 endpoint (default
`http://<host>:3777/api/`). Methods are the IPS function names (e.g. ``GetValue``,
``IPS_GetObject``), parameters are passed as a positional array. Authentication is
HTTP Basic Auth using a user configured in the IP-Symcon user management.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0


class IPSConfigError(RuntimeError):
    """Raised when the client is misconfigured (e.g. missing IPS_URL)."""


class IPSError(RuntimeError):
    """Raised when IP-Symcon returns a JSON-RPC error object."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"IP-Symcon error {code}: {message}")


def _normalize_url(url: str) -> str:
    """Ensure the configured URL points at the JSON-RPC endpoint (``.../api/``)."""
    url = url.strip().rstrip("/")
    if not url:
        raise IPSConfigError("IPS_URL is empty")
    if not url.endswith("/api"):
        url = url + "/api"
    return url + "/"


class IPSClient:
    """Thin async wrapper around the IP-Symcon JSON-RPC API."""

    def __init__(
        self,
        url: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        url = url if url is not None else os.environ.get("IPS_URL", "")
        if not url:
            raise IPSConfigError(
                "IPS_URL is not set. Point it at your IP-Symcon JSON-RPC endpoint, "
                "e.g. http://192.168.1.10:3777/api/"
            )
        self.url = _normalize_url(url)
        self.user = user if user is not None else os.environ.get("IPS_USER", "")
        self.password = password if password is not None else os.environ.get("IPS_PASSWORD", "")
        self.timeout = timeout
        self._id = 0

    @property
    def _auth(self) -> tuple[str, str] | None:
        if self.user or self.password:
            return (self.user, self.password)
        return None

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Call an arbitrary IP-Symcon function and return its result.

        Args:
            method: IPS function name, e.g. ``"GetValue"`` or ``"IPS_GetObject"``.
            params: Positional parameter list for the function.

        Returns:
            The ``result`` value of the JSON-RPC response.

        Raises:
            IPSError: when IP-Symcon returns a JSON-RPC error.
            httpx.HTTPStatusError / httpx.TransportError: on transport problems.
        """
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else [],
            "id": self._id,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.url, json=payload, auth=self._auth)
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            if isinstance(err, dict):
                raise IPSError(int(err.get("code", -1)), str(err.get("message", err)))
            raise IPSError(-1, str(err))
        return data.get("result") if isinstance(data, dict) else data
