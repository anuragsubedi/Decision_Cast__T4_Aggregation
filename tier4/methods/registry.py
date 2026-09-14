"""The aggregation method registry.

A *method* is a pure function from an :class:`InputStack` snapshot to a
:class:`MethodResult`. It does no HTTP, touches no database, and knows nothing
about tasks — which is what makes methods testable in isolation and what will
let the real statistical work (GP diversity/consensus, LLM rationale extraction,
chimeric priming) be developed without the rest of the pipeline running.

Methods are keyed by ``(data_type, method_name)``, matching how the backend's
``DiscoveryRecord:AggregationMethods`` config is keyed. A method may register
for the wildcard data type ``"*"`` to accept any input.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tier4.contracts.envelope import InputStack, MethodResult

MethodFn = Callable[[InputStack], MethodResult]

ANY_DATA_TYPE = "*"


@dataclass(frozen=True)
class MethodSpec:
    """A registered method and the metadata the backend registry wants."""

    data_type: str
    method_name: str
    fn: MethodFn
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.data_type, self.method_name)


class MethodRegistry:
    """Lookup table of methods this worker can execute."""

    def __init__(self) -> None:
        self._methods: dict[tuple[str, str], MethodSpec] = {}

    def register(
        self,
        data_type: str,
        method_name: str,
        description: str = "",
        **meta: Any,
    ) -> Callable[[MethodFn], MethodFn]:
        """Decorator form: ``@registry.register("int_num", "Median")``."""

        def decorator(fn: MethodFn) -> MethodFn:
            spec = MethodSpec(
                data_type=data_type,
                method_name=method_name,
                fn=fn,
                description=description or (fn.__doc__ or "").strip().split("\n")[0],
                meta=meta,
            )
            if spec.key in self._methods:
                raise ValueError(f"method already registered: {spec.key}")
            self._methods[spec.key] = spec
            return fn

        return decorator

    def resolve(self, data_type: str | None, method_name: str | None) -> MethodSpec | None:
        """Find a method for this cache row, falling back to the wildcard type.

        Both keys come off ``cache_input_for_aggregation`` and either can be
        null if fan-out went wrong, so this tolerates ``None``.
        """
        if not method_name:
            return None
        exact = self._methods.get((data_type or "", method_name))
        if exact is not None:
            return exact
        return self._methods.get((ANY_DATA_TYPE, method_name))

    def all(self) -> list[MethodSpec]:
        return list(self._methods.values())

    def __len__(self) -> int:
        return len(self._methods)


registry = MethodRegistry()
