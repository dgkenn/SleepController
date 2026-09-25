"""The LAN-access check must recognise a domain network as covered by a Domain firewall rule.

A firewall rule's Profile reads "Domain, Private" while a connection profile's NetworkCategory
reads "DomainAuthenticated", so matching the category against the profile never succeeded on a
domain network and lan.state said covered=false even with the rule the watchdog itself creates
for Private+Domain (audit 2026-09-25). There is no PowerShell in CI, so the block is checked as
source and its matching rule is mirrored in Python.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _block() -> str:
    src = (ROOT / "scripts" / "windows-watchdog.ps1").read_text(encoding="utf-8", errors="replace")
    i = src.index("$covered = $false")
    return src[i:i + 2500]


def test_domain_authenticated_is_mapped_to_the_firewall_domain_profile():
    block = _block()
    assert re.search(r'if \(\$c -eq "DomainAuthenticated"\) \{ "Domain" \} else \{ \$c \}', block)
    assert "$prof -match $fwProf" in block
    assert "$prof -match $c)" not in block, "still matching the raw network category"


def _covered(rule_profile: str, category: str) -> bool:
    """Python mirror of the PowerShell rule (-match is a case-insensitive regex search)."""
    fw = "Domain" if category == "DomainAuthenticated" else category
    return bool(re.search("Any", rule_profile, re.I) or re.search(fw, rule_profile, re.I))


def test_the_matching_rule_covers_each_network_category():
    assert _covered("Domain, Private", "DomainAuthenticated")
    assert _covered("Domain, Private", "Private")
    assert not _covered("Domain, Private", "Public")
    assert _covered("Public", "Public")
    assert _covered("Any", "DomainAuthenticated")
    # the old comparison is exactly what failed
    assert not re.search("DomainAuthenticated", "Domain, Private", re.I)
