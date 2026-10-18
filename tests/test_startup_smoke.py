"""Startet das Gateway wie im Betrieb (`python -m visu_gateway --transport https`) als Unterprozess.

Braucht kein Symcon: initialize und die Auth-Prüfung berühren das Backend nicht.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import subprocess
import sys
import time

import httpx
import pytest

from visu_gateway.mkcert import make_certificate

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running(tmp_path):
    cert, key = make_certificate(["127.0.0.1"])
    (tmp_path / "gateway.crt").write_bytes(cert)
    (tmp_path / "gateway.key").write_bytes(key)
    token, port = "smoke-token", free_port()
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "symcon: {url: 'http://127.0.0.1:9/api/'}\ntarget_category_id: 5\n"
        f"server: {{host: 127.0.0.1, port: {port}, tls_cert: '{(tmp_path / 'gateway.crt').as_posix()}', "
        f"tls_key: '{(tmp_path / 'gateway.key').as_posix()}', token_sha256: ['{hashlib.sha256(token.encode()).hexdigest()}']}}\n",
        encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "visu_gateway", "--transport", "https", "--config", str(cfg)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if proc.poll() is not None:
                pytest.fail(f"Gateway beendet sich: {proc.stderr.read().decode(errors='replace')[-500:]}")
            time.sleep(0.1)
        yield f"https://127.0.0.1:{port}/mcp", tmp_path / "gateway.crt", token
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_real_startup_enforces_token_and_tls(running):
    url, crt, token = running
    with httpx.Client(verify=ssl.create_default_context(cafile=str(crt))) as http:
        assert http.post(url, json=INIT, headers=HEADERS).status_code == 401
        assert http.post(url, json=INIT, headers={**HEADERS, "Authorization": "Bearer falsch"}).status_code == 401
        ok = http.post(url, json=INIT, headers={**HEADERS, "Authorization": f"Bearer {token}"})
        assert ok.status_code == 200 and "symcon_visu_gateway" in ok.text


def test_refuses_to_start_without_tokens(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("symcon: {url: 'http://127.0.0.1:9/api/'}\ntarget_category_id: 5\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "visu_gateway", "--transport", "https", "--config", str(cfg)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "token" in r.stderr.lower()


def test_refuses_remote_bind_without_tls(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("symcon: {url: 'http://127.0.0.1:9/api/'}\ntarget_category_id: 5\n"
                   "server: {host: 0.0.0.0, token_sha256: ['ab']}\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "visu_gateway", "--transport", "https", "--config", str(cfg)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "TLS" in r.stderr
