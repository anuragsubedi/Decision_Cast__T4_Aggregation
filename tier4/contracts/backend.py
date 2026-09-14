"""Typed mirror of the DecisionCast v2 backend's aggregation models.

Every model here corresponds 1:1 to a C# class in
``DecisionCast_BE_Api/Models/B_Logic/``. This module is the single place where
the cross-tier contract is written down, so when Mohamed changes a field on the
backend the failure shows up here as a validation error rather than as a wrong
number in a plot.

Wire format notes
-----------------
The backend registers controllers with ``AddControllers()`` and sets no
``PropertyNamingPolicy``, so ASP.NET Core applies ``JsonSerializerDefaults.Web``:

* **Responses** are serialised camelCase. .NET's camelCase policy lowercases the
  leading uppercase run only, which is why ``FK_AggRequestId`` goes out as
  ``fK_AggRequestId`` (the underscore stops the run) while ``TaskID`` goes out as
  ``taskID``. Fields that already start lowercase — ``metaData``, ``status``,
  ``execution_log``, ``input_cache_stack_key`` — travel unchanged.
* **Requests** are deserialised case-insensitively, so posting either casing
  works. We post the alias for symmetry.

Each model therefore declares the observed camelCase name as its alias and sets
``populate_by_name=True`` so Python-side construction can use the readable name.
``extra="allow"`` keeps us forward-compatible: a column added on the backend
will not break a running worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Task lifecycle strings. These are bare string literals in the C# — there is no
# enum and no config list backing them, unlike the question-state FSM.
TASK_PENDING = "pending"
TASK_COLLECTED = "collected"
TASK_COMPLETED = "completed"

# cache_input_for_aggregation.cache_state
CACHE_SNAPSHOT = "snapshot"
CACHE_COLLECTED = "collected"

# AggregatedAnswer_request.status
REQUEST_RECEIVED = "Received"
REQUEST_PARTIAL = "partially_completed"
REQUEST_COMPLETED = "completed"


class _Wire(BaseModel):
    """Shared config for every model that crosses the tier boundary."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class TaskRecord(_Wire):
    """Mirror of ``Models/B_Logic/Tasks.cs`` (table ``Tasks``).

    One unit of work: "aggregate question-inquiry X of question Y by method Z".
    The method name itself is *not* on this record — it lives on the linked
    cache row, which is why a worker must fetch the input before it can dispatch.
    """

    task_id: int = Field(alias="taskID")
    fk_agg_request_id: int = Field(alias="fK_AggRequestId")
    fk_question_id: int = Field(alias="fK_QuestionId")
    fk_question_inquiry_id: int = Field(alias="fK_QuestionInquiryId")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    meta_data: dict[str, Any] | None = Field(default=None, alias="metaData")
    requesting_user_email: str | None = Field(default=None, alias="requestingUserEmail")
    status: str | None = None
    execution_log: dict[str, Any] | None = None
    # Nullable on the backend. A null here means fan-out failed to build a
    # snapshot and the task is not actionable.
    input_cache_stack_key: int | None = None


class CacheInputRecord(_Wire):
    """Mirror of ``Models/B_Logic/cache_input_for_aggregation.cs``.

    The frozen input a task consumes, plus the method to apply to it. The
    snapshot in ``aggregation_input_stack`` is parsed separately by
    :class:`tier4.contracts.envelope.InputStack`.
    """

    input_cache_stack_key: int
    fk_agg_request_id: int = Field(alias="fK_AggRequestId")
    fk_question_inquiry_id: int = Field(alias="fK_QuestionInquiryId")
    aggregation_input_stack: dict[str, Any] | None = None
    cache_state: str | None = None
    aggregation_method: str | None = None
    data_type: str | None = None
    created_at: datetime | None = None


class CollectorRequest(_Wire):
    """Mirror of ``Models/B_Logic/collector_request.cs`` — the claim payload.

    The backend rejects the claim unless ``invoking_user_email`` equals the email
    on the bearer token, so the worker has to know its own identity.
    """

    invoking_user_email: str
    aggregator_name: str


class AggregatedAnswerDataComponent(_Wire):
    """Mirror of ``Models/B_Logic/AggregatedAnswerDataComponent.cs``.

    The result row. Both the legacy in-process C# path and this external path
    write into this same table, which is what lets the frontend render a
    Tier 4 result with no changes.

    ``FK_AggregationRequestID`` and ``FK_QuestionInquiryId`` are overwritten by
    the backend from the task record on submit, so a worker cannot mis-address a
    result. We leave them unset on the way out.

    ``AggregationMethodName``, ``status`` and ``result_meta_data`` were added by
    the consolidated migration but ``submit_task_result`` does not currently
    populate them — see docs/OPEN_QUESTIONS.md.
    """

    agg_data_id: int | None = Field(default=None, alias="aggDataId")
    fk_aggregation_request_id: int | None = Field(
        default=None, alias="fK_AggregationRequestID"
    )
    fk_question_inquiry_id: int | None = Field(
        default=None, alias="fK_QuestionInquiryId"
    )
    json_data: dict[str, Any] | None = Field(default=None, alias="jsonData")
    description: str | None = None
    rendering_hints: dict[str, Any] | None = Field(
        default=None, alias="renderingHints"
    )
    aggregation_method_name: str | None = Field(
        default=None, alias="aggregationMethodName"
    )
    result_meta_data: dict[str, Any] | None = None
    status: str | None = None


class TaskResult(_Wire):
    """Mirror of ``Models/B_Logic/TaskResult.cs`` — the submit payload.

    The backend requires ``TaskID`` and a non-null
    ``aggregatedAnswerDataComponent.JsonData``; everything else is optional and
    the identifying foreign keys are re-derived server-side.
    """

    task_id: int = Field(alias="taskID")
    fk_agg_request_id: int = Field(default=0, alias="fK_AggRequestId")
    fk_question_id: int = Field(default=0, alias="fK_QuestionId")
    fk_question_inquiry_id: int = Field(default=0, alias="fK_QuestionInquiryId")
    submit_user_email: str | None = Field(default=None, alias="submitUserEmail")
    execution_log: dict[str, Any] | None = None
    input_cache_stack_key: int | None = None
    aggregated_answer_data_component: AggregatedAnswerDataComponent | None = Field(
        default=None, alias="aggregatedAnswerDataComponent"
    )


class ExternalAggregatorRegistration(_Wire):
    """Mirror of ``Models/B_Logic/ExternalAggregator.cs`` — the method registry.

    ``URL`` and ``last_heart_beat`` exist as columns but ``RegisterMethod``
    silently drops them, so today this registry is descriptive only: it tells a
    human (and eventually the proposer UI) which methods an external service
    claims to offer. It does not route anything — dispatch is pull-based.
    """

    ea_registration_id: int | None = Field(default=None, alias="eaRegistrationId")
    aggregator_service_name: str | None = Field(
        default=None, alias="aggregatorServiceName"
    )
    data_type: str | None = Field(default=None, alias="dataType")
    method_name: str | None = Field(default=None, alias="methodName")
    description: str | None = None
    meta_data: dict[str, Any] | None = Field(default=None, alias="metaData")
    url: str | None = None
    last_heart_beat: datetime | None = None


class AuthResponse(_Wire):
    """Mirror of ``Models/Auth/AuthResponse.cs``."""

    username: str | None = None
    email: str | None = None
    token: str
    expires_at_iso_timestamp: str | None = Field(
        default=None, alias="expiresAt_ISO_timestamp"
    )
