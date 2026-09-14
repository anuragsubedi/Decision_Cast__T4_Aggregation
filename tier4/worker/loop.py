"""The poll/claim/fetch/compute/submit loop.

Tier 4 is a **pull-based worker**. The backend never calls it; it never holds a
long-lived connection. That is a deliberate consequence of how the v2 backend
was built — there is no dispatch code on the backend side, only a task table and
endpoints to drain it. (``ExternalAggregator.URL`` and ``last_heart_beat``
columns exist for a push model that was never wired up.)

One iteration:

1. ``GET Get_Pending_Tasks``
2. for each task, ``peek_on_task_input`` to learn the method *without* mutating
   ``cache_state`` — this is why peek exists and why we use it before claiming
3. if no registered method matches, leave the task pending for someone else
4. ``Collect_Task``            (task: pending -> collected)
5. ``Collect_Task_Input``      (cache: snapshot -> collected)
6. run the method in-process
7. ``submit_task_result``      (task: -> completed, request: -> completed/partial)

A method that raises does **not** submit a result. The task is deliberately left
in ``collected`` rather than forced back to ``pending``: the backend exposes no
endpoint to reset a task, and silently retrying a task that crashes the worker
would spin forever. A stuck ``collected`` task is a visible, diagnosable state.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tier4.client.decisioncast import BackendError, DecisionCastClient
from tier4.contracts.backend import (
    AggregatedAnswerDataComponent,
    TaskRecord,
    TaskResult,
)
from tier4.contracts.envelope import InputStack
from tier4.methods.registry import MethodRegistry

log = logging.getLogger(__name__)


@dataclass
class IterationReport:
    """What one pass over the pending queue did. Returned so the FastAPI
    ``/run-once`` endpoint and the tests can assert on it."""

    polled: int = 0
    completed: list[int] = field(default_factory=list)
    skipped_no_method: list[int] = field(default_factory=list)
    failed: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "polled": self.polled,
            "completed": self.completed,
            "skipped_no_method": self.skipped_no_method,
            "failed": self.failed,
        }


class AggregationWorker:
    def __init__(
        self,
        client: DecisionCastClient,
        registry: MethodRegistry,
        aggregator_name: str = "tier4-datascience",
        claim_unknown_methods: bool = False,
    ) -> None:
        self.client = client
        self.registry = registry
        self.aggregator_name = aggregator_name
        self.claim_unknown_methods = claim_unknown_methods
        client.set_aggregator_name(aggregator_name)

    # -- one pass ----------------------------------------------------------

    def run_once(self) -> IterationReport:
        report = IterationReport()
        tasks = self.client.get_pending_tasks()
        report.polled = len(tasks)
        if not tasks:
            return report

        log.info("polled %d pending task(s)", len(tasks))
        for task in tasks:
            self._handle_task(task, report)
        return report

    def _handle_task(self, task: TaskRecord, report: IterationReport) -> None:
        tid = task.task_id

        # Fan-out failed to build a snapshot for this task. Nothing to do, and
        # nothing this worker can fix.
        if task.input_cache_stack_key is None:
            log.warning("task %s has no input_cache_stack_key; skipping", tid)
            report.skipped_no_method.append(tid)
            return

        try:
            # Peek first: we must know the method before deciding to claim, and
            # peeking leaves cache_state alone so another worker can still take it.
            preview = self.client.peek_task_input(task.input_cache_stack_key)
            spec = self.registry.resolve(preview.data_type, preview.aggregation_method)

            if spec is None and not self.claim_unknown_methods:
                log.info(
                    "task %s wants %s/%s which this worker does not implement; leaving pending",
                    tid,
                    preview.data_type,
                    preview.aggregation_method,
                )
                report.skipped_no_method.append(tid)
                return

            self.client.collect_task(tid)
            cache = self.client.collect_task_input(task.input_cache_stack_key)

            if spec is None:
                raise RuntimeError(
                    f"no method registered for "
                    f"({cache.data_type!r}, {cache.aggregation_method!r})"
                )

            stack = InputStack.model_validate(cache.aggregation_input_stack or {})

            started = time.monotonic()
            result = spec.fn(stack)
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)

            component = AggregatedAnswerDataComponent(
                json_data=result.json_data,
                description=result.description
                or f"Aggregated by {self.aggregator_name} using {spec.method_name}",
                # Populated even though submit_task_result currently ignores
                # them, so the values are already correct if the backend starts
                # persisting them. See docs/OPEN_QUESTIONS.md.
                aggregation_method_name=spec.method_name,
                status="completed",
                result_meta_data=result.meta or None,
            )

            self.client.submit_task_result(
                TaskResult(
                    task_id=tid,
                    fk_agg_request_id=task.fk_agg_request_id,
                    fk_question_id=task.fk_question_id,
                    fk_question_inquiry_id=task.fk_question_inquiry_id,
                    submit_user_email=self.client.email,
                    input_cache_stack_key=task.input_cache_stack_key,
                    execution_log={
                        "aggregator": self.aggregator_name,
                        "method": spec.method_name,
                        "data_type": cache.data_type,
                        "row_count": stack.row_count,
                        "elapsed_ms": elapsed_ms,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "meta": result.meta,
                    },
                    aggregated_answer_data_component=component,
                )
            )

            log.info("task %s completed via %s in %sms", tid, spec.method_name, elapsed_ms)
            report.completed.append(tid)

        except BackendError as exc:
            log.error("task %s failed against the backend: %s", tid, exc)
            report.failed.append({"task_id": tid, "error": str(exc), "kind": "backend"})
        except Exception as exc:  # noqa: BLE001 - one bad task must not kill the loop
            log.error("task %s failed: %s\n%s", tid, exc, traceback.format_exc())
            report.failed.append({"task_id": tid, "error": str(exc), "kind": "method"})

    # -- continuous ---------------------------------------------------------

    def run_forever(
        self, poll_interval: float = 5.0, max_iterations: int = 0
    ) -> list[IterationReport]:
        """Poll until stopped, or for ``max_iterations`` passes if non-zero."""
        reports: list[IterationReport] = []
        iteration = 0
        while max_iterations == 0 or iteration < max_iterations:
            iteration += 1
            try:
                reports.append(self.run_once())
            except BackendError as exc:
                # The backend being down is expected and transient; keep polling.
                log.error("poll failed: %s", exc)
            if max_iterations and iteration >= max_iterations:
                break
            time.sleep(poll_interval)
        return reports
