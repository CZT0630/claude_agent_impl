"""Interceptor chain for model and tool calls.

Interceptors are the s23 attachment point for cross-cutting runtime behavior:
tracing, policy checks, retries, timeout guards, cost accounting, and audits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


Request = dict[str, Any]
Handler = Callable[[Request], Any]


class ModelInterceptor:
    def order(self) -> int:
        """Lower values wrap the call earlier and observe the result later."""
        return 0

    def intercept_model(self, request: Request, handler: Handler) -> Any:
        """Inspect or modify a model request, then optionally call handler."""
        return handler(request)


class ToolInterceptor:
    def order(self) -> int:
        """Lower values wrap the call earlier and observe the result later."""
        return 0

    def intercept_tool(self, request: Request, handler: Handler) -> Any:
        """Inspect or modify a tool request, then optionally call handler."""
        return handler(request)


class InterceptorChain:
    def __init__(
        self,
        *,
        model_interceptors: list[ModelInterceptor] | None = None,
        tool_interceptors: list[ToolInterceptor] | None = None,
    ):
        self.model_interceptors = self._ordered(model_interceptors or [])
        self.tool_interceptors = self._ordered(tool_interceptors or [])

    @staticmethod
    def _ordered(interceptors: list[Any]) -> list[Any]:
        return sorted(interceptors, key=lambda item: item.order())

    def add_model(self, interceptor: ModelInterceptor) -> None:
        self.model_interceptors = self._ordered(self.model_interceptors + [interceptor])

    def add_tool(self, interceptor: ToolInterceptor) -> None:
        self.tool_interceptors = self._ordered(self.tool_interceptors + [interceptor])

    def intercept_model(self, request: Request, handler: Handler) -> Any:
        return self._invoke(self.model_interceptors, "intercept_model", request, handler)

    def intercept_tool(self, request: Request, handler: Handler) -> Any:
        return self._invoke(self.tool_interceptors, "intercept_tool", request, handler)

    @staticmethod
    def _invoke(
        interceptors: list[Any],
        method_name: str,
        request: Request,
        handler: Handler,
    ) -> Any:
        # Build nested calls like middleware: first interceptor is outermost.
        def call_at(index: int, current_request: Request) -> Any:
            if index >= len(interceptors):
                return handler(current_request)

            interceptor = interceptors[index]
            method = getattr(interceptor, method_name)
            return method(current_request, lambda next_request: call_at(index + 1, next_request))

        return call_at(0, request)
