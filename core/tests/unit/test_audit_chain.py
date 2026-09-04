from typing import Any

from nova.security.audit_chain import (
    GENESIS_HASH,
    canonicalize,
    compute_event_hash,
    verify_chain_link,
    verify_event_hash,
)


def test_canonicalize_is_deterministic() -> None:
    first = canonicalize({"b": 2, "a": 1})
    second = canonicalize({"a": 1, "b": 2})

    assert first == second


def test_compute_event_hash_is_deterministic() -> None:
    kwargs: dict[str, Any] = {
        "actor": "user",
        "action": "execute_tool",
        "tool_name": "test_tool",
        "decision": "allow",
        "result": "success",
        "args_hash": "abc123",
        "detail": "test",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "prev_hash": GENESIS_HASH,
    }

    first = compute_event_hash(**kwargs)
    second = compute_event_hash(**kwargs)

    assert first == second
    assert len(first) == 64


def test_different_previous_hash_changes_event_hash() -> None:
    common: dict[str, Any] = {
        "actor": "user",
        "action": "execute_tool",
        "tool_name": "test_tool",
        "decision": "allow",
        "result": "success",
        "args_hash": None,
        "detail": None,
        "occurred_at": "2026-01-01T00:00:00+00:00",
    }

    first = compute_event_hash(
        **common,
        prev_hash=GENESIS_HASH,
    )

    second = compute_event_hash(
        **common,
        prev_hash="1" * 64,
    )

    assert first != second


def test_verify_event_hash_accepts_correct_hash() -> None:
    kwargs: dict[str, Any] = {
        "actor": "user",
        "action": "execute_tool",
        "tool_name": "test_tool",
        "decision": "allow",
        "result": "success",
        "args_hash": None,
        "detail": "ok",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "prev_hash": GENESIS_HASH,
    }

    expected_hash = compute_event_hash(**kwargs)

    assert verify_event_hash(
        **kwargs,
        expected_hash=expected_hash,
    )


def test_verify_event_hash_rejects_tampering() -> None:
    kwargs: dict[str, Any] = {
        "actor": "user",
        "action": "execute_tool",
        "tool_name": "test_tool",
        "decision": "allow",
        "result": "success",
        "args_hash": None,
        "detail": "original",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "prev_hash": GENESIS_HASH,
    }

    expected_hash = compute_event_hash(**kwargs)

    tampered_kwargs: dict[str, Any] = {
        **kwargs,
        "detail": "tampered",
    }

    assert not verify_event_hash(
        **tampered_kwargs,
        expected_hash=expected_hash,
    )


def test_verify_chain_link_uses_genesis_for_first_event() -> None:
    assert verify_chain_link(
        previous_hash=None,
        current_prev_hash=GENESIS_HASH,
    )

    assert not verify_chain_link(
        previous_hash=None,
        current_prev_hash="1" * 64,
    )


def test_verify_chain_link_accepts_previous_event_hash() -> None:
    previous_hash = "a" * 64

    assert verify_chain_link(
        previous_hash=previous_hash,
        current_prev_hash=previous_hash,
    )

    assert not verify_chain_link(
        previous_hash=previous_hash,
        current_prev_hash="b" * 64,
    )
