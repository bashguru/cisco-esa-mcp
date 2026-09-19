"""Filename + content classifier.

The corpus repeats the same guide across many software versions, so the single
most important accuracy lever is tagging every document with product, version,
and doc type and letting callers filter on them. This module derives that
metadata from the filename first (reliable for Cisco's naming) and falls back
to a content sample for the version when needed.

It ships tuned for Cisco Secure Email / Content Security filenames but is just
a table of rules - edit PRODUCT_RULES / DOCTYPE_RULES for your own corpus.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# Cisco AsyncOS major versions we expect. Guards against matching hardware model
# numbers (380-680, C195, x70) as if they were versions.
_ALLOWED_MAJORS = {"9", "10", "11", "12", "13", "14", "15", "16", "17", "18"}
_VERSION_RE = re.compile(r"(?<!\d)(\d{1,2})[._-](\d{1,2})(?:[._-](\d{1,2}))?(?!\d)")
_ASYNCOS_RE = re.compile(r"AsyncOS[\s_-]*v?(\d{1,2})[._-](\d{1,2})(?:[._-](\d{1,2}))?", re.I)

# (regex on filename, product label). First match wins; order matters.
PRODUCT_RULES: list[tuple[str, str]] = [
    (r"(^|[_-])app[_-]", "Cisco Advanced Phishing Protection (APP)"),
    (r"(^|[_-])dp[_-]|Domain-Protection", "Cisco Domain Protection (DP)"),
    (r"Registered-Envelope|(^|[_-])res[_-]|CRES", "Cisco Registered Envelope Service (CRES)"),
    (r"Domain-Protection", "Cisco Domain Protection (DP)"),
    (r"Content-Security|x[789]0-Series|380-680|Installation-and-Maintenance",
     "Cisco Content Security Appliance (hardware)"),
    (r"\bSMA\b|Security-Management|Web-Manager", "Cisco Secure Email and Web Manager (SMA)"),
]
_DEFAULT_PRODUCT = "Cisco Secure Email Gateway (ESA)"

# (regex on filename, doc type). First match wins; order matters.
DOCTYPE_RULES: list[tuple[str, str]] = [
    (r"CLI[-_]?Reference", "CLI Reference Guide"),
    (r"API.*Getting-Started|Getting-Started-Guide.*API|API-Getting-Started", "API Getting Started Guide"),
    (r"Admin[-_]Guide", "Admin Guide"),
    (r"User[-_ ]?Guide|user_guide", "User Guide"),
    (r"Release[-_]Notes", "Release Notes"),
    (r"Hardware-Installation|Installation-and-Maintenance|Hardware", "Hardware Installation Guide"),
    (r"Deployment-Guide|deployment", "Deployment Guide"),
    (r"Quickstart|Getting-Started|Welcome", "Getting Started / Quickstart"),
    (r"prework|implementation", "Implementation Guide"),
    (r"Best-Practice", "Best Practices"),
    (r"Compatibility_Matrix|At-a-Glance|Infographic|Data-Sheet|Terminology|Useful-Links|Lifecycle|Efficacy",
     "Data Sheet / Reference"),
    (r"^(Configure|Troubleshoot|How-?to|How-do|Enable|Create|Verify|Reset|Exempt|Block|Mitigate|"
     r"Provision|Load|Download|Find|Generate|Restart|Submit|Understand|Use|Explain|Trigger|Disable|"
     r"Configure-|Migrate)", "How-To / Troubleshooting"),
    (r"encryption-add-in|submission-add-in|add-in", "Add-in User Guide"),
]
_DEFAULT_DOCTYPE = "Other"


def _norm_version(m: re.Match) -> str:
    parts = [p for p in m.groups() if p]
    return ".".join(parts)


def detect_version(filename: str, text_sample: Optional[str] = None) -> Optional[str]:
    # Prefer an explicit "AsyncOS X.Y.Z" in the filename.
    m = _ASYNCOS_RE.search(filename)
    if m and m.group(1) in _ALLOWED_MAJORS:
        return _norm_version(m)
    # Any version-like token in the filename with a plausible major.
    for m in _VERSION_RE.finditer(filename):
        if m.group(1) in _ALLOWED_MAJORS:
            return _norm_version(m)
    # Fall back to the first page of text.
    if text_sample:
        m = _ASYNCOS_RE.search(text_sample)
        if m and m.group(1) in _ALLOWED_MAJORS:
            return _norm_version(m)
    return None


def detect_product(filename: str) -> str:
    for pattern, label in PRODUCT_RULES:
        if re.search(pattern, filename, re.I):
            return label
    return _DEFAULT_PRODUCT


def detect_doc_type(filename: str) -> str:
    for pattern, label in DOCTYPE_RULES:
        if re.search(pattern, filename, re.I):
            return label
    return _DEFAULT_DOCTYPE


def prettify_title(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"^b_", "", stem)
    return re.sub(r"[_-]+", " ", stem).strip()


def classify(filename: str, text_sample: Optional[str] = None,
             doc_title: Optional[str] = None) -> dict[str, Optional[str]]:
    base = os.path.basename(filename)
    return {
        "product": detect_product(base),
        "version": detect_version(base, text_sample),
        "doc_type": detect_doc_type(base),
        "title": (doc_title or "").strip() or prettify_title(base),
    }
