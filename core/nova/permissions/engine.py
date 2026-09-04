"""Permission evaluation engine for NOVA.

The engine evaluates a tool's declared permission and risk against the
configured permission policy. It contains no execution logic: it only
decides whether an action is allowed, denied, or requires confirmation.
"""

from __future__ import annotations

import enum
from typing import ClassVar

from pydantic import BaseModel, Field

from nova.permissions.policy import PermissionPolicy
from nova.tools.base import RiskLevel, ToolMetadata


class PermissionDecision(enum.StrEnum):
    """Decision returned by the permission engine."""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


class PermissionResult(BaseModel):
    """Result of evaluating a tool against the permission policy."""

    model_config = {"frozen": True, "extra": "forbid"}

    decision: PermissionDecision
    permission: str = Field(min_length=3, max_length=128)
    risk: RiskLevel
    detail: str = Field(min_length=1, max_length=1024)


class PermissionEngine:
    """Evaluate tool permissions without executing the tool.

    The engine uses a default-deny policy. If a tool requests a permission
    that is not explicitly present in the policy, execution is denied.
    """

    _RISK_ORDER: ClassVar[dict[RiskLevel, int]] = {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
        RiskLevel.CRITICAL: 3,
    }

    def __init__(self, policy: PermissionPolicy) -> None:
        """Create an engine using the supplied immutable policy."""
        self._policy = policy

    def evaluate(self, metadata: ToolMetadata) -> PermissionResult:
        """Evaluate whether a tool may execute.

        Args:
            metadata: Declarative metadata belonging to the registered tool.

        Returns:
            A permission decision.

        The policy is default-deny: missing permissions are denied.
        A tool whose risk exceeds the policy's allowed risk is denied.
        A matching rule requiring confirmation returns CONFIRM.
        Otherwise the action is allowed.
        """
        permission = metadata.required_permission
        risk = metadata.risk
        rule = self._policy.rule_for(permission)

        if rule is None:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                permission=permission,
                risk=risk,
                detail=f"Permission '{permission}' is not granted by policy.",
            )

        if self._RISK_ORDER[risk] > self._RISK_ORDER[rule.allowed_risk]:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                permission=permission,
                risk=risk,
                detail=(
                    f"Tool risk '{risk.value}' exceeds the policy limit "
                    f"'{rule.allowed_risk.value}'."
                ),
            )

        if rule.requires_confirmation:
            return PermissionResult(
                decision=PermissionDecision.CONFIRM,
                permission=permission,
                risk=risk,
                detail=f"Permission '{permission}' requires confirmation.",
            )

        return PermissionResult(
            decision=PermissionDecision.ALLOW,
            permission=permission,
            risk=risk,
            detail=f"Permission '{permission}' is allowed by policy.",
        )
