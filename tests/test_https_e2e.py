"""End-to-End: das Gateway über HTTPS mit Token starten und per MCP-Client ansprechen (Simulator-Backend)."""

from __future__ import annotations

import hashlib
import socket
import ssl
import threading
import time

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from visu_gateway.guard import GuardedClient
from visu_gateway.mkcert import make_certificate
from visu_gateway.server import HashedTokenVerifier, build_server

TOKEN = "test-token-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def https_server(world, tmp_path):
    cert, key = make_certificate(["127.0.0.1", "localhost"])
    (tmp_path / "gw.crt").write_bytes(cert)
    (tmp_path / "gw.key").write_bytes(key)
    mcp = build_server(GuardedClient(world.fake, 500, False), HashedTokenVerifier([hashlib.sha256(TOKEN.encode()).hexdigest()]))
    port = free_port()
    app = mcp.http_app(transport="http")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           ssl_certfile=str(tmp_path / "gw.crt"), ssl_keyfile=str(tmp_path / "gw.key")))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"https://127.0.0.1:{port}/mcp", tmp_path / "gw.crt"
    server.should_exit = True
    t.join(timeout=5)


async def test_https_with_token_and_pinned_cert(https_server):
    url, crt = https_server

    def factory(**kw):
        return httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(crt)), **kw)

    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {TOKEN}"}, httpx_client_factory=factory)
    async with Client(transport) as c:
        names = {t.name for t in await c.list_tools()}
        assert "visu_info" in names
        res = await c.call_tool("visu_info", {})
        assert "500" in res.content[0].text and "9.0" in res.content[0].text


async def test_https_rejects_missing_or_wrong_token(https_server):
    url, crt = https_server
    async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(crt))) as http:
        for headers in ({}, {"Authorization": "Bearer falsch"}):
            r = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                headers={"Accept": "application/json, text/event-stream", **headers})
            assert r.status_code == 401


async def test_untrusted_certificate_is_rejected(https_server):
    url, _ = https_server
    async with httpx.AsyncClient() as http:  # System-CAs kennen das selbstsignierte Zertifikat nicht
        with pytest.raises(httpx.ConnectError):
            await http.post(url, json={})
