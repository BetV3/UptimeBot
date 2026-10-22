"""Every contact address the site shows must be one that can receive mail.

checkpulse.dev has no MX record (send-only: Postmark/Resend transactional), so a
"support@checkpulse.dev" contact silently dropped privacy and deletion requests the
policy promises to answer. Contact addresses live on trycheckpulse.com (Google
Workspace; support@ is an alias of elvis@, verified by SMTP RCPT 250 on 2026-10-21).
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
RECEIVING_DOMAINS = {"trycheckpulse.com"}
SEND_ONLY_OK = {"no-reply@checkpulse.dev"}  # From: address for transactional mail, never a contact

ADDR_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _addresses():
    for f in sorted(TEMPLATES.rglob("*.html")):
        for a in ADDR_RE.findall(f.read_text(encoding="utf-8")):
            if a.endswith(("checkpulse.dev", "checkpulse.com")):
                yield f.name, a.lower()


def test_contact_addresses_are_on_a_receiving_domain():
    bad = [(f, a) for f, a in _addresses()
           if a not in SEND_ONLY_OK and a.split("@", 1)[1] not in RECEIVING_DOMAINS]
    assert not bad, f"contact address on a domain with no inbound mail: {bad}"


def test_privacy_policy_names_the_support_address():
    legal = (TEMPLATES / "legal.html").read_text(encoding="utf-8")
    assert "support@trycheckpulse.com" in legal
    assert 'href="mailto:support@trycheckpulse.com"' in legal
