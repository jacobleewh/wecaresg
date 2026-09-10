"""
PDPA-compliant sanitizer for Singapore-specific PII
=======================================================
Masks Singapore NRIC/FIN numbers and local mobile phone numbers found in
free-text hardship narratives before they are triaged or persisted, in line
with Singapore's Personal Data Protection Act (PDPA) data-minimisation
principles.
"""

import re

# NRIC/FIN: 1 letter (S/T/F/G/M) + 7 digits + 1 checksum letter, e.g. S1234567A
NRIC_PATTERN = re.compile(r"\b([STFGM])(\d{4})(\d{3})([A-Z])\b")

# SG mobile numbers: 8 digits starting with 8 or 9, e.g. 91234567
PHONE_PATTERN = re.compile(r"\b([89]\d{3})(\d{4})\b")


def mask_nric(text: str) -> str:
    """Replace NRIC/FIN numbers with e.g. S****567A (first letter + last 3
    digits + checksum letter visible, remaining digits masked)."""
    return NRIC_PATTERN.sub(lambda m: f"{m.group(1)}****{m.group(3)}{m.group(4)}", text)


def mask_phone(text: str) -> str:
    """Replace 8-digit SG mobile numbers with e.g. ****4567 (last 4 digits visible)."""
    return PHONE_PATTERN.sub(lambda m: f"****{m.group(2)}", text)


def sanitize(text: str) -> str:
    """Apply all PDPA masking rules. Safe to call on any free-text input
    before it is passed to the triage engine or persisted to the database."""
    return mask_phone(mask_nric(text))
