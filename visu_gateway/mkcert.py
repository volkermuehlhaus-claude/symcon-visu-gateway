"""Selbstsigniertes TLS-Zertifikat: ``python -m visu_gateway.mkcert --host symcon-pc --host 192.168.1.20``.

Schreibt ``gateway.crt`` (öffentlich, wird dem Client als vertrauenswürdig mitgegeben) und ``gateway.key``
(privat, nur für das Konto des Gateways lesbar machen). Die Hosts landen als Subject Alternative Names;
der Client muss das Gateway unter genau einem dieser Namen aufrufen.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def make_certificate(hosts: list[str], days: int = 730) -> tuple[bytes, bytes]:
    """Liefert (Zertifikat-PEM, Schlüssel-PEM)."""
    if not hosts:
        raise ValueError("Mindestens ein Host (Name oder IP) ist nötig.")
    key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = []
    for h in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "symcon-visu-gateway")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="visu_gateway.mkcert")
    ap.add_argument("--host", action="append", required=True, help="Rechnername oder IP (mehrfach möglich)")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--out", default=".", help="Zielordner")
    ap.add_argument("--force", action="store_true", help="vorhandene Dateien überschreiben")
    args = ap.parse_args(argv)

    out = Path(args.out)
    crt, key = out / "gateway.crt", out / "gateway.key"
    if (crt.exists() or key.exists()) and not args.force:
        print("gateway.crt/gateway.key existieren schon (mit --force überschreiben).")
        return 1
    cert_pem, key_pem = make_certificate(args.host, args.days)
    key.write_bytes(key_pem)
    crt.write_bytes(cert_pem)
    print(f"Geschrieben: {crt} (öffentlich) und {key} (PRIVAT – Zugriff einschränken)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
