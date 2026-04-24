"""Utility helpers: hashing and email validation."""

from __future__ import annotations

import hashlib
import re


def hash_token(value: str, *, salt: str = "") -> str:
    """Return a deterministic hex digest of ``value`` with an optional ``salt``."""
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def validate_email(email: str) -> bool:
    """Very loose email shape check. Not RFC-compliant — good enough for tests."""
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))
