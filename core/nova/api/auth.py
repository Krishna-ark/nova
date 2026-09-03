"""Authentication for the NOVA API.

Group 4B scope: verifying a bearer token against the configured value.
There is no user model, no session store, no role system and no device
authentication here. Those belong to later milestones.

The comparison is timing-safe. A naive ``==`` on secrets leaks information
through response timing: an attacker measuring how long a rejection takes
can recover a token character by character. ``secrets.compare_digest``
always examines the whole value.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import SecretStr

_BEARER_PREFIX = "bearer "


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header.

    Returns None when the header is absent, malformed, or uses a different
    scheme. The scheme comparison is case-insensitive, as RFC 7235 requires.
    """
    if not authorization_header:
        return None

    if not authorization_header.lower().startswith(_BEARER_PREFIX):
        return None

    token = authorization_header[len(_BEARER_PREFIX):].strip()

    return token or None


def token_is_valid(provided: str | None, expected: SecretStr) -> bool:
    """Return True when the provided token matches the configured one.

    Uses a constant-time comparison so a rejection reveals nothing about how
    much of the token was correct.
    """
    if not provided:
        return False

    return secrets.compare_digest(
        provided,
        expected.get_secret_value(),
    )