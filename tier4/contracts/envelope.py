"""The snapshot envelope — the real interface between the backend and Tier 4.

The HTTP endpoints are just plumbing. What actually determines whether a Tier 4
method can do its job is the shape of ``cache_input_for_aggregation
.aggregation_input_stack``, built by ``ExtendedAggregatorController
.CreateAnswerStack_Snapshot``:

.. code-block:: json

    {
      "columns":  ["row_ids", "<QuestionInquiry.Input_Name>"],
      "schema":   {"value": "integer"},
      "csv_rows": [["responder@example.com", 42], ["other@example.com", 17]],
      "row_count": 2
    }

Properties worth knowing before writing a method against it:

* Only **published** answers are included (``Answer.IsPublished``).
* Each row is ``[row_id, value]``. ``value`` is whatever sat under the ``value``
  key of the responder's ``AnswerDataJson`` — a scalar for ``int_num`` /
  ``text`` / ``bool_flag``, a nested array for the curve types.
* ``row_id`` is currently the **responder's email address**. That is a real PII
  and IRB concern; see docs/OPEN_QUESTIONS.md.
* ``schema.value`` is a bare type-name string from
  ``DiscoveryRecord:data_types_schemas`` in the backend's ``ext_config``. It
  tells you almost nothing about structured payloads, and a data type missing
  from that config map causes snapshot construction to fail outright.
* A responder who answered the same inquiry more than once contributes more than
  one row, so ``row_count`` is a row count, not a responder count.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InputStack(BaseModel):
    """The parsed ``aggregation_input_stack`` snapshot."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    columns: list[str] = Field(default_factory=list)
    # ``schema`` shadows a BaseModel attribute, hence the trailing underscore.
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    csv_rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0

    @property
    def value_column_name(self) -> str:
        """The proposer-authored ``Input_Name`` for this inquiry.

        Falls back to a generic label if the backend emitted a short header,
        which happens when ``Input_Name`` was never set.
        """
        return self.columns[1] if len(self.columns) > 1 else "value"

    @property
    def declared_type(self) -> str | None:
        """The ``schema.value`` type-name string, if the backend supplied one."""
        raw = self.schema_.get("value")
        return raw if isinstance(raw, str) else None

    def row_ids(self) -> list[Any]:
        """Row identifiers (today: responder emails) in snapshot order."""
        return [row[0] for row in self.csv_rows if len(row) >= 1]

    def values(self) -> list[Any]:
        """Answer values in snapshot order, skipping structurally short rows."""
        return [row[1] for row in self.csv_rows if len(row) >= 2]

    def pairs(self) -> list[tuple[Any, Any]]:
        """``(row_id, value)`` pairs — use this when provenance matters."""
        return [(row[0], row[1]) for row in self.csv_rows if len(row) >= 2]


class MethodResult(BaseModel):
    """What a Tier 4 method returns, before it is wrapped for submission.

    ``json_data`` becomes ``AggregatedAnswerDataComponent.JsonData`` verbatim —
    it is what the frontend will eventually render, so its shape is a
    frontend-facing contract, not an internal one.

    ``meta`` is diagnostic: how many rows were used, how many were skipped, how
    long it took. It is folded into the task's ``execution_log`` rather than into
    the displayed result, so it can be as verbose as is useful.
    """

    model_config = ConfigDict(extra="forbid")

    json_data: dict[str, Any]
    description: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
