"""Registry for NOVA tools.

The registry is deliberately boring: it validates tool declarations at
registration time and exposes metadata, never arbitrary callables.

This keeps the permission boundary explicit. A caller can ask which tool
exists and what it requires, but execution must go through ToolExecutor.
"""

from __future__ import annotations

from pydantic import BaseModel

from nova.tools.base import Tool, ToolMetadata


class ToolRegistrationError(ValueError):
    """Raised when a tool cannot be registered safely."""


class ToolRegistry:
    """Immutable-by-convention collection of validated tool classes."""

    def __init__(self) -> None:
        self._tools: dict[str, type[Tool]] = {}

    def register(self, tool: type[Tool]) -> None:
        """Register a tool class after validating its declaration."""
        if not issubclass(tool, Tool):
            raise ToolRegistrationError("registered object must subclass Tool")

        metadata = getattr(tool, "metadata", None)
        input_model = getattr(tool, "input_model", None)

        if not isinstance(metadata, ToolMetadata):
            raise ToolRegistrationError(f"{tool.__name__} must declare a valid ToolMetadata")

        if not isinstance(input_model, type) or not issubclass(input_model, BaseModel):
            raise ToolRegistrationError(f"{tool.__name__} must declare a Pydantic input_model")

        if metadata.tool_id in self._tools:
            raise ToolRegistrationError(f"tool id already registered: {metadata.tool_id}")

        self._tools[metadata.tool_id] = tool

    def get(self, tool_id: str) -> type[Tool] | None:
        """Return the registered tool class, or None when unknown."""
        return self._tools.get(tool_id)

    def metadata(self) -> tuple[ToolMetadata, ...]:
        """Return metadata for all registered tools."""
        return tuple(tool.metadata for tool in self._tools.values())

    def __contains__(self, tool_id: str) -> bool:
        """Return whether a tool id is registered."""
        return tool_id in self._tools

    def __len__(self) -> int:
        """Return the number of registered tools."""
        return len(self._tools)
