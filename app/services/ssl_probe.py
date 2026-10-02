"""One-shot TLS certificate inspection for the public SSL checker tool.

Anyone on the internet can type a hostname into /tools/ssl-checker, which makes
*our* server open a TCP connection. That is the same risk class as alert
webhooks, so the same rules apply (see app/services/url_guard.py):

- Port 443 only. No user-chosen ports, so the tool cannot scan services.
- Resolve the name ourselves, refuse any private/loopback/link-local/metadata
  address, then connect to the vetted IP literal (SNI still carries the name).
  Connecting to the IP we checked, not re-resolving, closes the DNS-rebinding
  window that url_guard has to leave open.
- Short timeouts, one connection per lookup, no HTTP request is sent.
- Error text never echoes a resolved address.

The route adds per-IP and global rate limits on top.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

from app.services.url_guard import _address_is_private

CONNECT_TIMEOUT = 5.0
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)([a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")


class ProbeError(ValueError):
    """User-facing reason the lookup could not run. Safe to display."""


@dataclass
class CertReport:
    host: str
    trusted: bool
    trust_error: str | None
    subject_cn: str | None
    issuer: str | None
    sans: list[str] = field(default_factory=list)
    not_before: datetime | None = None
    not_after: datetime | None = None
    tls_version: str | None = None

    @property
    def days_left(self) -> int | None:
        if not self.not_after:
            return None
        return (self.not_after - datetime.now(timezone.utc)).days

    @property
    def verdict(self) -> str:
        d = self.days_left
        if d is not None and d < 0:
            return "expired"
        if not self.trusted:
            return "untrusted"
        if d is not None and d <= 14:
            return "expiring"
        return "ok"


def normalize_host(raw: str) -> str:
    """Turn whatever someone pasted into a bare, validated hostname."""
    s = (raw or "").strip().lower()
    if not s:
        raise ProbeError("Enter a domain, for example example.com.")
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)   # scheme
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in s:
        raise ProbeError("Enter just the domain, without a username.")
    if s.startswith("["):
        raise ProbeError("Enter a domain name, not an IP address.")
    if ":" in s:
        host, _, port = s.rpartition(":")
        if port != "443":
            raise ProbeError("Only HTTPS on port 443 can be checked.")
        s = host
    s = s.rstrip(".")
    try:
        ipaddress.ip_address(s)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        # Raised outside the try: ProbeError subclasses ValueError and would
        # otherwise be swallowed by the except above.
        raise ProbeError("Enter a domain name, not an IP address.")
    try:
        s = s.encode("idna").decode("ascii")
    except UnicodeError:
        raise ProbeError("That does not look like a valid domain.") from None
    if not _HOST_RE.match(s):
        raise ProbeError("That does not look like a valid domain.")
    return s


def _public_ip(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ProbeError(f"{host} does not resolve in DNS.") from None
    addrs = sorted({info[4][0] for info in infos}, key=lambda a: (":" in a, a))  # IPv4 first
    if not addrs:
        raise ProbeError(f"{host} does not resolve in DNS.")
    for a in addrs:
        if _address_is_private(ipaddress.ip_address(a)):
            raise ProbeError("That domain points to a private address and cannot be checked.")
    return addrs[0]


def _handshake(ip: str, host: str, verify: bool) -> tuple[bytes, str | None]:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((ip, 443), timeout=CONNECT_TIMEOUT) as sock:
        sock.settimeout(CONNECT_TIMEOUT)
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
            return der, tls.version()


def _name(n: x509.Name, oid) -> str | None:
    attrs = n.get_attributes_for_oid(oid)
    return str(attrs[0].value) if attrs else None


def probe(raw_host: str) -> CertReport:
    host = normalize_host(raw_host)
    ip = _public_ip(host)

    trusted, trust_error = True, None
    try:
        der, version = _handshake(ip, host, verify=True)
    except ssl.SSLCertVerificationError as e:
        trusted = False
        trust_error = (e.verify_message or "certificate could not be verified").rstrip(".")
        try:
            der, version = _handshake(ip, host, verify=False)
        except (OSError, ssl.SSLError):
            raise ProbeError(f"{host} accepted the connection but the TLS handshake failed.") from None
    except ssl.SSLError:
        raise ProbeError(f"{host} answered on port 443 but the TLS handshake failed.") from None
    except socket.timeout:
        raise ProbeError(f"{host} did not answer on port 443 within {int(CONNECT_TIMEOUT)} seconds.") from None
    except ConnectionRefusedError:
        raise ProbeError(f"{host} refused the connection on port 443.") from None
    except OSError:
        raise ProbeError(f"Could not connect to {host} on port 443.") from None

    if not der:
        raise ProbeError(f"{host} did not present a certificate.")
    cert = x509.load_der_x509_certificate(der)
    try:
        sans = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    issuer = _name(cert.issuer, NameOID.ORGANIZATION_NAME) or _name(cert.issuer, NameOID.COMMON_NAME)
    return CertReport(
        host=host,
        trusted=trusted,
        trust_error=trust_error,
        subject_cn=_name(cert.subject, NameOID.COMMON_NAME),
        issuer=issuer,
        sans=list(sans)[:50],
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        tls_version=version,
    )
