"""Start: ``python -m visu_gateway --transport stdio|https --config <datei>``.

* ``stdio``  – nur für das lokale Testsystem (Docker-Symcon). Keine Authentifizierung.
* ``https``  – Betrieb auf dem Symcon-Rechner. Verlangt Token-Hashes; TLS Pflicht, außer auf Loopback.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .client import IPSClient
from .config import ConfigError, GatewayConfig, load_config
from .guard import GuardedClient
from .server import HashedTokenVerifier, build_server

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def build_guard(cfg: GatewayConfig) -> GuardedClient:
    raw = IPSClient(url=cfg.ips_url, user=cfg.ips_user, password=cfg.ips_password)
    return GuardedClient(raw, cfg.target_category_id, cfg.enable_write, cfg.audit_log, cfg.max_string,
                         cfg.allow_groups, cfg.visu_instance_id, cfg.allow_layout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="visu_gateway")
    ap.add_argument("--transport", choices=["stdio", "https"], default="stdio")
    ap.add_argument("--config", default=None, help="Pfad zur Konfiguration (sonst VISU_GATEWAY_CONFIG)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)  # stdout gehört dem MCP-Protokoll
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as e:
        print(f"Konfiguration: {e}", file=sys.stderr)
        return 2

    guard = build_guard(cfg)
    if args.transport == "stdio":
        build_server(guard).run(transport="stdio", show_banner=False)
        return 0

    if not cfg.token_sha256:
        print("server.token_sha256 ist leer – ohne Token startet das Gateway nicht.", file=sys.stderr)
        return 2
    uvicorn_config: dict[str, object] = {}
    if cfg.tls_cert and cfg.tls_key:
        uvicorn_config = {"ssl_certfile": str(cfg.tls_cert), "ssl_keyfile": str(cfg.tls_key)}
    elif cfg.listen_host not in LOOPBACK:
        print("Ohne TLS (server.tls_cert/tls_key) nur auf Loopback erlaubt.", file=sys.stderr)
        return 2
    build_server(guard, HashedTokenVerifier(cfg.token_sha256)).run(
        transport="http", host=cfg.listen_host, port=cfg.listen_port, show_banner=False,
        uvicorn_config=uvicorn_config or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
