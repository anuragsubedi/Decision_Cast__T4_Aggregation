"""Methods shipped with the scaffold.

**These are not the thesis aggregations.** They exist to prove the round trip is
wired correctly end to end, and to give the contract something concrete to be
tested against. The real work — GP diversity/consensus over percentile bands,
LLM rationale extraction, chimeric priming — lands in sibling modules once the
algorithms are settled.

``Median`` and ``Sum`` were chosen deliberately: the v2 backend advertises both
in ``DiscoveryRecord:AggregationMethods`` for ``int_num`` but implements neither
in ``MainAggregationService``'s switch, so today the proposer UI offers two
methods that do nothing. Implementing them here is a real, if small, piece of
work rather than a toy, and it demonstrates the intended division of labour:
the backend advertises, Tier 4 executes.

Result-shape convention
-----------------------
Every method writes a ``json_data`` of the form::

    {"method": ..., "input_name": ..., "n": ..., "result": {...}, "skipped": ...}

Keeping ``result`` as a nested object rather than a bare scalar means the
frontend display layer can branch on ``method`` without the envelope changing
shape, which is the same reasoning behind the ``PercentileBandsSummary``
placeholder's method-agnostic envelope.
"""

from __future__ import annotations

import statistics
from typing import Any

from tier4.contracts.envelope import InputStack, MethodResult
from tier4.methods.registry import ANY_DATA_TYPE, registry


def _numeric_values(stack: InputStack) -> tuple[list[float], int]:
    """Coerce snapshot values to floats, reporting how many were unusable.

    Answer values arrive as whatever JSON the responder's widget stored. For
    ``int_num`` that is normally a number, but a widget that stored a numeric
    string would otherwise poison the whole aggregation, so we coerce leniently
    and count what we dropped instead of raising.
    """
    numbers: list[float] = []
    skipped = 0
    for value in stack.values():
        if isinstance(value, bool):
            # bool is an int subclass in Python; a boolean here means the
            # question was wired to the wrong data type.
            skipped += 1
            continue
        if isinstance(value, (int, float)):
            numbers.append(float(value))
            continue
        if isinstance(value, str):
            try:
                numbers.append(float(value.strip()))
                continue
            except ValueError:
                pass
        skipped += 1
    return numbers, skipped


def _envelope(
    method: str, stack: InputStack, result: dict[str, Any], n: int, skipped: int
) -> dict[str, Any]:
    return {
        "method": method,
        "input_name": stack.value_column_name,
        "n": n,
        "skipped": skipped,
        "result": result,
    }


@registry.register("int_num", "Median", description="Median of the responses")
def median(stack: InputStack) -> MethodResult:
    """Median of the numeric responses."""
    numbers, skipped = _numeric_values(stack)
    if not numbers:
        return MethodResult(
            json_data=_envelope("Median", stack, {"median": None}, 0, skipped),
            description="Median over 0 usable responses",
            meta={"reason": "no usable numeric values"},
        )
    value = statistics.median(numbers)
    return MethodResult(
        json_data=_envelope("Median", stack, {"median": value}, len(numbers), skipped),
        description=f"Median over {len(numbers)} responses",
        meta={"min": min(numbers), "max": max(numbers)},
    )


@registry.register("int_num", "Sum", description="Sum of the responses")
def total(stack: InputStack) -> MethodResult:
    """Sum of the numeric responses."""
    numbers, skipped = _numeric_values(stack)
    return MethodResult(
        json_data=_envelope(
            "Sum", stack, {"sum": sum(numbers)}, len(numbers), skipped
        ),
        description=f"Sum over {len(numbers)} responses",
    )


@registry.register(
    ANY_DATA_TYPE,
    "Tier4Echo",
    description="Diagnostic passthrough — returns the snapshot unchanged",
)
def echo(stack: InputStack) -> MethodResult:
    """Return the snapshot back, for verifying the pipeline without any maths.

    Registered against the wildcard data type so it works for any widget,
    including ones Tier 4 has no real method for yet. Use it to confirm a new
    data type's snapshot actually arrives intact before writing statistics
    against it.
    """
    return MethodResult(
        json_data=_envelope(
            "Tier4Echo",
            stack,
            {
                "columns": stack.columns,
                "declared_type": stack.declared_type,
                "rows": stack.csv_rows,
            },
            stack.row_count,
            0,
        ),
        description=f"Echo of {stack.row_count} rows",
    )
