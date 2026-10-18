"""Selbsttest der Gateway-Konfiguration: ``python -m visu_gateway.selfcheck --config config.yaml``.

Gibt nur Status aus (nie Passwort, Token oder Werte). Liest ausschließlich: Kernel-Version, Zielkategorie.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from .client import IPSError
from .config import ConfigError, GatewayConfig, load_config
from .guard import GuardedClient


async def check(cfg: GatewayConfig, guard: GuardedClient) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    try:
        out.append((True, f"Symcon erreichbar, Kernel-Version {await guard.call('IPS_GetKernelVersion')}"))
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        hint = " (Benutzer/Passwort in symcon.user / symcon.password prüfen)" if code in (401, 403) else ""
        return [*out, (False, f"Symcon antwortet mit HTTP {code}{hint}")]
    except (httpx.TransportError, IPSError) as e:
        return [*out, (False, f"Symcon nicht erreichbar: {type(e).__name__}")]

    try:
        obj = await guard.call("IPS_GetObject", [cfg.target_category_id])
        ok = isinstance(obj, dict) and obj.get("ObjectType") == 0
        name = obj.get("ObjectName") if isinstance(obj, dict) else None
        out.append((ok, f"Zielkategorie {cfg.target_category_id}: {name!r}" + ("" if ok else " – ist keine Kategorie")))
    except IPSError:
        out.append((False, f"Zielkategorie {cfg.target_category_id} existiert nicht"))

    out.append((True, f"Schreiben: {'FREIGEGEBEN' if cfg.enable_write else 'gesperrt (enable_write: false)'}"))
    out.append((bool(cfg.token_sha256), f"Token-Hashes: {len(cfg.token_sha256)}"))
    for label, path in (("TLS-Zertifikat", cfg.tls_cert), ("TLS-Schlüssel", cfg.tls_key)):
        out.append((bool(path and path.is_file()), f"{label}: {'vorhanden' if path and path.is_file() else 'fehlt'}"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="visu_gateway.selfcheck")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as e:
        print(f"[FEHLER] Konfiguration: {e}")
        return 2
    from .__main__ import build_guard

    results = asyncio.run(check(cfg, build_guard(cfg)))
    for ok, msg in results:
        print(f"[{'ok' if ok else 'FEHLER'}] {msg}")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
