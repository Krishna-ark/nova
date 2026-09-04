"""Hash-chain utilities for NOVA audit events.

Every audit event is linked to the hash of the previous event. The resulting
chain makes modification or deletion of an earlier event detectable.

The hashing code is deliberately independent from database transaction
handling. The executor is responsible for obtaining the latest event and
assigning the resulting hash before the repository flushes the new event.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64


def canonicalize(value: Any) -> str:
    """Convert a value into deterministic JSON.

    Deterministic serialization is required so the same audit event always
    produces the same hash regardless of dictionary insertion order or
    whitespace formatting.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def compute_event_hash(
    *,
    actor: str,
    action: str,
    tool_name: str,
    decision: str,
    result: str,
    args_hash: str | None,
    detail: str | None,
    occurred_at: str,
    prev_hash: str,
) -> str:
    """Compute the SHA-256 hash for one audit event.

    The previous event's hash is included in the payload, creating the
    chain relationship.

    Args:
        actor: Identity responsible for the action.
        action: Action being attempted.
        tool_name: Registered tool involved in the attempt.
        decision: Permission decision.
        result: Execution result.
        args_hash: Hash of the tool arguments, if available.
        detail: Human-readable event detail.
        occurred_at: Event timestamp in canonical string form.
        prev_hash: Hash of the immediately preceding audit event.

    Returns:
        A lowercase hexadecimal SHA-256 digest.
    """
    payload = {
        "actor": actor,
        "action": action,
        "tool_name": tool_name,
        "decision": decision,
        "result": result,
        "args_hash": args_hash,
        "detail": detail,
        "occurred_at": occurred_at,
        "prev_hash": prev_hash,
    }

    canonical_payload = canonicalize(payload)
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def verify_event_hash(
    *,
    actor: str,
    action: str,
    tool_name: str,
    decision: str,
    result: str,
    args_hash: str | None,
    detail: str | None,
    occurred_at: str,
    prev_hash: str,
    expected_hash: str,
) -> bool:
    """Verify that an audit event's stored hash is correct.

    This performs only the cryptographic comparison. It does not inspect
    database ordering or verify that ``prev_hash`` points to an actual
    previous event.
    """
    actual_hash = compute_event_hash(
        actor=actor,
        action=action,
        tool_name=tool_name,
        decision=decision,
        result=result,
        args_hash=args_hash,
        detail=detail,
        occurred_at=occurred_at,
        prev_hash=prev_hash,
    )

    return actual_hash == expected_hash


def verify_chain_link(
    *,
    previous_hash: str | None,
    current_prev_hash: str,
) -> bool:
    """Verify the link between two adjacent audit events.

    For the first event, ``previous_hash`` is ``None`` and the current event
    must reference the genesis hash.
    """
    expected_prev_hash = GENESIS_HASH if previous_hash is None else previous_hash

    return current_prev_hash == expected_prev_hash
