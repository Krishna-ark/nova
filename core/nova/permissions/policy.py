"""Declarative permission policy for NOVA.

The policy contains no execution logic. It only describes which permissions
exist and what risk levels they permit. The permission engine is responsible
for evaluating a request against this policy.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from nova.tools.base import RiskLevel


class PermissionRule(BaseModel):
    """A single permission rule."""

    model_config = {"frozen": True, "extra": "forbid"}

    permission: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    allowed_risk: RiskLevel
    requires_confirmation: bool = False


class PermissionPolicy(BaseModel):
    """Complete permission policy used by the engine."""

    model_config = {"frozen": True, "extra": "forbid"}

    rules: tuple[PermissionRule, ...] = ()

    def rule_for(self, permission: str) -> PermissionRule | None:
        """Return the rule for a permission, if one exists."""
        for rule in self.rules:
            if rule.permission == permission:
                return rule
        return None
