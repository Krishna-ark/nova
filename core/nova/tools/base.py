"""The tool interface.

A tool is the only way NOVA can affect anything. There is no general shell
tool and no path from a language model to arbitrary code: the model may
eventually choose *which* registered tool to request, but it cannot define a
new capability at runtime.

Every tool declares its metadata as class attributes, so the registry can
validate a tool without instantiating it, and the permission engine can reach
a decision without executing anything.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, Field


class RiskLevel(enum.StrEnum):
    """How much damage a tool can do if invoked wrongly.

    Declared by the tool and used by the permission engine. A tool cannot
    lower its own risk level at runtime: the value is a class attribute read
    at registration time.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class VerificationLevel(enum.StrEnum):
    """How a tool's effect is confirmed after execution.

    ``NONE`` is honest rather than lazy: a read-only tool that returns data
    has nothing to verify beyond its return value. Anything that changes
    state should declare a stronger level and implement ``verify``.
    """

    NONE = "none"
    PROCESS = "process"
    FILESYSTEM = "filesystem"
    CHECKSUM = "checksum"
    STATE_QUERY = "state_query"


class ToolMetadata(BaseModel):
    """Declarative description of a tool.

    Serialisable on purpose: the same structure will later be rendered into
    a tool-calling schema for a language model and into the permissions
    screen of the desktop UI.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    tool_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    risk: RiskLevel
    required_permission: str = Field(min_length=3, max_length=128)
    idempotent: bool = False
    verification: VerificationLevel = VerificationLevel.NONE


class ToolOutcome(BaseModel):
    """What a tool returns.

    A tool reports failure by returning ``ok=False`` rather than by raising,
    so an expected failure (a file that does not exist) is distinguishable
    from a defect (a bug in the tool). The executor treats an unhandled
    exception as a defect and records it separately.
    """

    model_config = {"extra": "forbid"}

    ok: bool
    detail: str | None = Field(default=None, max_length=4096)
    data: Mapping[str, str | int | float | bool | None] = Field(default_factory=dict)


class Tool[ToolArguments: BaseModel](ABC):
    """Base class for every NOVA capability.

    Subclasses declare ``metadata`` and ``input_model`` as class attributes.
    The registry rejects a subclass that omits either, so a half-defined tool
    fails at registration rather than at execution.
    """

    metadata: ClassVar[ToolMetadata]
    input_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def execute(self, arguments: ToolArguments) -> ToolOutcome:
        """Perform the tool's work.

        Args:
            arguments: An instance of ``input_model``, already validated.

        Returns:
            The outcome. Raising is reserved for defects.
        """

    async def verify(
        self,
        arguments: ToolArguments,
        outcome: ToolOutcome,
    ) -> bool | None:
        """Confirm the tool's effect actually happened.

        Returns:
            True when verified, False when verification failed, and None when
            the tool declares no verification. The default returns None so a
            tool cannot accidentally claim to have been verified.
        """
        _ = arguments, outcome
        return None
