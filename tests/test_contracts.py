"""Contract tests.

The payloads below are **real responses captured from a running v2 backend**
(commit `eac4f3b`, 2026-09-09), not hand-written guesses. That matters: the
whole point of the contract layer is to catch the day the backend's wire format
drifts, so the fixtures have to be ground truth.

If one of these starts failing, the backend changed. Update the models and the
docs together — do not just loosen the assertion.
"""

from __future__ import annotations

from tier4.contracts.backend import (
    AggregatedAnswerDataComponent,
    CacheInputRecord,
    TaskRecord,
    TaskResult,
)
from tier4.contracts.envelope import InputStack

# --- captured payloads -----------------------------------------------------

TASK_JSON = {
    "taskID": 1,
    "fK_AggRequestId": 1,
    "fK_QuestionId": 2,
    "fK_QuestionInquiryId": 2,
    "createdAt": "2026-09-09T11:13:52.569885",
    "metaData": {
        "aggregator_name": "tier4-datascience",
        "invoking_user_email": "agguser@consumer.com",
    },
    "requestingUserEmail": "t4proposer@dcast.local",
    "status": "completed",
    "execution_log": {"method": "Median", "row_count": 3},
    "input_cache_stack_key": 1,
}

CACHE_JSON = {
    "input_cache_stack_key": 1,
    "fK_AggRequestId": 1,
    "fK_QuestionInquiryId": 2,
    "aggregation_input_stack": {
        "schema": {"value": "integer"},
        "columns": ["row_ids", "peak_hospitalisations"],
        "csv_rows": [
            ["t4resp1@dcast.local", 12],
            ["t4resp2@dcast.local", 30],
            ["t4resp3@dcast.local", 21],
        ],
        "row_count": 3,
    },
    "cache_state": "collected",
    "aggregation_method": "Median",
    "data_type": "int_num",
    "created_at": "2026-09-09T11:13:52.55845",
}

RESULT_JSON = {
    "aggDataId": 1,
    "fK_AggregationRequestID": 1,
    "fK_QuestionInquiryId": 2,
    "jsonData": {"n": 3, "method": "Median", "result": {"median": 21.0}},
    "description": "Median over 3 responses",
    "renderingHints": None,
    "aggregationMethodName": "Median",
    "result_meta_data": {"max": 30.0, "min": 12.0},
    "status": "completed",
}


# --- parsing ---------------------------------------------------------------


def test_task_parses_camel_case_wire_format():
    task = TaskRecord.model_validate(TASK_JSON)
    assert task.task_id == 1
    # The .NET camelCase policy stops at the underscore, so this really is
    # "fK_AggRequestId" on the wire, not "fkAggRequestId".
    assert task.fk_agg_request_id == 1
    assert task.fk_question_inquiry_id == 2
    assert task.input_cache_stack_key == 1
    assert task.requesting_user_email == "t4proposer@dcast.local"


def test_cache_record_and_snapshot_parse():
    cache = CacheInputRecord.model_validate(CACHE_JSON)
    assert cache.aggregation_method == "Median"
    assert cache.data_type == "int_num"

    stack = InputStack.model_validate(cache.aggregation_input_stack)
    assert stack.row_count == 3
    assert stack.value_column_name == "peak_hospitalisations"
    assert stack.declared_type == "integer"
    assert stack.values() == [12, 30, 21]
    assert stack.row_ids()[0] == "t4resp1@dcast.local"


def test_result_component_round_trips():
    component = AggregatedAnswerDataComponent.model_validate(RESULT_JSON)
    assert component.aggregation_method_name == "Median"
    assert component.json_data["result"]["median"] == 21.0
    # Serialising back out must reproduce the backend's own key names.
    dumped = component.model_dump(by_alias=True, exclude_none=True)
    assert "fK_AggregationRequestID" in dumped
    assert "jsonData" in dumped
    assert "aggregationMethodName" in dumped


def test_unknown_backend_fields_do_not_break_parsing():
    """A column added on the backend must not crash a deployed worker."""
    task = TaskRecord.model_validate({**TASK_JSON, "someBrandNewColumn": 99})
    assert task.task_id == 1


def test_task_result_serialises_the_keys_the_backend_requires():
    result = TaskResult(
        task_id=7,
        aggregated_answer_data_component=AggregatedAnswerDataComponent(
            json_data={"ok": True}
        ),
    )
    payload = result.model_dump(by_alias=True, exclude_none=True)
    assert payload["taskID"] == 7
    # submit_task_result rejects the request without this nested JsonData.
    assert payload["aggregatedAnswerDataComponent"]["jsonData"] == {"ok": True}


def test_snapshot_tolerates_malformed_rows():
    """Short rows are skipped rather than raising, so one bad row cannot
    destroy an otherwise usable aggregation."""
    stack = InputStack.model_validate(
        {
            "columns": ["row_ids", "v"],
            "schema": {"value": "integer"},
            "csv_rows": [["a", 1], ["b"], ["c", 3]],
            "row_count": 3,
        }
    )
    assert stack.values() == [1, 3]


def test_empty_snapshot_is_valid():
    stack = InputStack.model_validate({})
    assert stack.values() == []
    assert stack.value_column_name == "value"
