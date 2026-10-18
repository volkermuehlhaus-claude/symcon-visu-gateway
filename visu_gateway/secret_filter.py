"""Secret-Filter für alle Antworten des Gateways.

Python-Port der Logik aus ``symcon_visu_export.php`` (Projektordner): verdächtige
Schlüssel/Namen, token-artige Strings, Zugangsdaten in URLs, verdächtige URL-Parameter.

Grenzen (bewusst, siehe notes/): Der Filter erkennt Muster, keine Bedeutung. Ein
Passwort in einer Variablen mit harmlosem Namen und kurzem Wert wird nicht erkannt.
Deshalb ist er nur die zweite Verteidigungslinie hinter der Methoden-Whitelist.
"""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "***REDACTED***"
DEFAULT_MAX_STRING = 300

SECRET_KEY = re.compile(
    r"(pass|pwd|kennwort|passwort|token|secret|api.?key|auth|user|benutzer|login|mail|client.?id|"
    r"refresh|session|cookie|bearer|\bpin\b|iban|serial|seriennummer)",
    re.IGNORECASE,
)
_JWT = re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]+$")
_LONG_TOKEN = re.compile(r"^[A-Za-z0-9+/=_\-]{32,}$")
_URL_CREDENTIALS = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
_URL_PARAM = re.compile(r"([?&](?:token|key|apikey|api_key|password|pass|secret|auth)=)[^&\s\"]+", re.IGNORECASE)


def is_suspicious_name(name: Any) -> bool:
    return isinstance(name, str) and SECRET_KEY.search(name) is not None


def looks_like_secret(s: str) -> bool:
    return bool(_JWT.match(s) or _LONG_TOKEN.match(s))


def clean_string(s: str, path: str, suspicious: bool, redactions: list[tuple[str, str]],
                 max_len: int = DEFAULT_MAX_STRING) -> str:
    if s == "":
        return s
    if suspicious:
        redactions.append((path, "Schlüssel/Name verdächtig"))
        return REDACTED
    if looks_like_secret(s):
        redactions.append((path, "sieht aus wie Token"))
        return REDACTED
    clean = _URL_CREDENTIALS.sub(r"\1***:***@", s)
    if clean != s:
        redactions.append((path, "Zugangsdaten in URL"))
    clean2 = _URL_PARAM.sub(r"\1***", clean)
    if clean2 != clean:
        redactions.append((path, "verdächtiger URL-Parameter"))
    if len(clean2) > max_len:
        clean2 = clean2[:max_len] + "…[gekürzt]"
    return clean2


def clean_recursive(v: Any, path: str, redactions: list[tuple[str, str]], max_len: int = DEFAULT_MAX_STRING) -> Any:
    """Bereinigt verschachtelte Strukturen. Strings unter verdächtigen Dict-Schlüsseln werden maskiert."""
    if isinstance(v, dict):
        out: dict[Any, Any] = {}
        for k, item in v.items():
            p = f"{path}.{k}"
            if isinstance(item, str):
                out[k] = clean_string(item, p, is_suspicious_name(k), redactions, max_len)
            else:
                out[k] = clean_recursive(item, p, redactions, max_len)
        return out
    if isinstance(v, list):
        return [clean_recursive(item, f"{path}.{i}", redactions, max_len) for i, item in enumerate(v)]
    if isinstance(v, str):
        return clean_string(v, path, False, redactions, max_len)
    return v


def clean_value(value: Any, name: str | None, path: str, redactions: list[tuple[str, str]],
                max_len: int = DEFAULT_MAX_STRING) -> Any:
    """Variablenwert: String wird maskiert, wenn der Variablenname verdächtig ist oder er wie ein Token aussieht."""
    if isinstance(value, str):
        return clean_string(value, path, is_suspicious_name(name), redactions, max_len)
    return value


def decode_json_fields(item: dict[str, Any], fields: tuple[str, ...] = ("Configuration",)) -> dict[str, Any]:
    """JSON-Strings (z. B. WebFront-``Configuration``) dekodieren, damit der Filter hineinsieht."""
    out = dict(item)
    for f in fields:
        raw = out.get(f)
        if isinstance(raw, str):
            try:
                out[f] = json.loads(raw)
            except ValueError:
                pass
    return out
