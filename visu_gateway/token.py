"""Erzeugt ein Zugriffs-Token: das Token geht an den Client, nur der SHA-256-Hash in die Gateway-Konfiguration."""

import hashlib
import secrets

if __name__ == "__main__":
    token = secrets.token_urlsafe(32)
    print(f"Token (nur an den Client, nirgends sonst ablegen): {token}")
    print(f"token_sha256-Eintrag fuer die Konfiguration:        {hashlib.sha256(token.encode()).hexdigest()}")
