"""Tests for the built-in methods and the registry.

Methods are pure functions of a snapshot, which is the property that will let
the real statistical work be developed and validated without any of the
pipeline running.
"""

from __future__ import annotations

import pytest

from tier4.contracts.envelope import InputStack
from tier4.methods import builtin  # noqa: F401 - import registers the methods
from tier4.methods.registry import ANY_DATA_TYPE, MethodRegistry, registry


def stack(values: list[object], name: str = "peak") -> InputStack:
    return InputStack.model_validate(
        {
            "columns": ["row_ids", name],
            "schema": {"value": "integer"},
            "csv_rows": [[f"r{i}@x.local", v] for i, v in enumerate(values)],
            "row_count": len(values),
        }
    )


def test_median_odd_and_even():
    assert builtin.median(stack([12, 30, 21])).json_data["result"]["median"] == 21
    assert builtin.median(stack([1, 2, 3, 4])).json_data["result"]["median"] == 2.5


def test_sum():
    assert builtin.total(stack([12, 30, 21])).json_data["result"]["sum"] == 63


def test_envelope_carries_the_proposer_authored_input_name():
    result = builtin.median(stack([1, 2, 3], name="hospitalisations"))
    assert result.json_data["input_name"] == "hospitalisations"
    assert result.json_data["method"] == "Median"
    assert result.json_data["n"] == 3


def test_numeric_strings_are_coerced_not_dropped():
    result = builtin.median(stack(["12", 30, "21"]))
    assert result.json_data["result"]["median"] == 21
    assert result.json_data["skipped"] == 0


def test_unusable_values_are_counted_not_fatal():
    result = builtin.median(stack([12, "not a number", 30, None]))
    assert result.json_data["n"] == 2
    assert result.json_data["skipped"] == 2


def test_booleans_are_rejected_as_wrong_data_type():
    """bool is an int subclass in Python; treating True as 1 would silently
    aggregate a mis-wired bool_flag widget as a number."""
    result = builtin.median(stack([True, False, 5]))
    assert result.json_data["n"] == 1
    assert result.json_data["skipped"] == 2


def test_empty_input_returns_a_null_result_rather_than_raising():
    """A question can legitimately have zero published answers. That must
    produce a result row saying so, not a crashed task."""
    result = builtin.median(stack([]))
    assert result.json_data["result"]["median"] is None
    assert result.json_data["n"] == 0


def test_echo_accepts_any_data_type():
    spec = registry.resolve("percentile_curve", "Tier4Echo")
    assert spec is not None
    assert spec.data_type == ANY_DATA_TYPE


def test_registry_resolution_is_keyed_on_data_type_and_method():
    assert registry.resolve("int_num", "Median") is not None
    assert registry.resolve("text", "Median") is None
    assert registry.resolve("int_num", "GPConsensus") is None
    assert registry.resolve(None, None) is None


def test_registry_rejects_duplicate_registration():
    local = MethodRegistry()
    local.register("int_num", "X")(lambda s: None)
    with pytest.raises(ValueError):
        local.register("int_num", "X")(lambda s: None)
