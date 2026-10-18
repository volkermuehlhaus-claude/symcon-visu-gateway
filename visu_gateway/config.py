"""Gateway-Konfiguration aus einer YAML-Datei.

Der Pfad kommt aus ``VISU_GATEWAY_CONFIG`` (nur der Pfad, keine Geheimnisse in der Umgebung).
Die Datei enthält das Symcon-Passwort und muss für das Benutzerkonto von Claude Code unlesbar sein.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .secret_filter import DEFAULT_MAX_STRING


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayConfig:
    ips_url: str
    ips_user: str
    ips_password: str
    target_category_id: int
    enable_write: bool = False
    allow_groups: bool = False  # Dummy-Instanzen (Sammelkacheln) unterhalb der Zielkategorie anlegen/löschen
    audit_log: Path | None = None
    max_string: int = DEFAULT_MAX_STRING
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    tls_cert: Path | None = None
    tls_key: Path | None = None
    token_sha256: tuple[str, ...] = field(default_factory=tuple)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: str | os.PathLike[str] | None = None) -> GatewayConfig:
    import yaml

    path = path or os.environ.get("VISU_GATEWAY_CONFIG", "")
    if not path:
        raise ConfigError("VISU_GATEWAY_CONFIG ist nicht gesetzt (Pfad zur Gateway-Konfigurationsdatei).")
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    ips = data.get("symcon") or {}
    srv = data.get("server") or {}

    url = str(ips.get("url") or "").strip()
    if not url:
        raise ConfigError("symcon.url fehlt in der Gateway-Konfiguration.")
    try:
        target = int(data.get("target_category_id"))
    except (TypeError, ValueError):
        raise ConfigError("target_category_id fehlt oder ist keine ganze Zahl.") from None
    if target <= 0:
        raise ConfigError("target_category_id muss eine echte Objekt-ID sein (nicht 0 = Wurzel).")

    def _path(key: str) -> Path | None:
        v = srv.get(key) if key.startswith("tls_") else data.get(key)
        return Path(v).expanduser() if v else None

    return GatewayConfig(
        ips_url=url,
        ips_user=str(ips.get("user") or ""),
        ips_password=str(ips.get("password") or ""),
        target_category_id=target,
        enable_write=_as_bool(data.get("enable_write", False)),
        allow_groups=_as_bool(data.get("allow_groups", False)),
        audit_log=_path("audit_log"),
        max_string=int(data.get("max_string", DEFAULT_MAX_STRING)),
        listen_host=str(srv.get("host") or "127.0.0.1"),
        listen_port=int(srv.get("port") or 8765),
        tls_cert=_path("tls_cert"),
        tls_key=_path("tls_key"),
        token_sha256=tuple(str(t).strip().lower() for t in (srv.get("token_sha256") or [])),
    )
