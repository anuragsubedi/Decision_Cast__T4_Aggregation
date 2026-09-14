# Tier 4 architecture

Why the boundaries are where they are. For wire-level detail see
[`BE_CONTRACT.md`](./BE_CONTRACT.md); for things still undecided see
[`OPEN_QUESTIONS.md`](./OPEN_QUESTIONS.md).

## Why a separate service at all

Aggregation for the thesis experiments is statistical work — Gaussian-process
diversity and consensus over percentile bands, LLM-driven rationale extraction,
ensembling human prediction intervals against machine forecasts. None of that
is reasonable to write in C# inside a request handler, and all of it is
iterated on by researchers rather than by the platform engineer.

Splitting it out buys three things:

1. **Researchers can develop methods independently.** A method is a pure
   function from a snapshot to a result. It needs no database, no HTTP, no
   backend running.
2. **The schematic agreement is explicit.** The contract lives in one typed
   module rather than being implied by whatever JSON two codebases happen to
   exchange.
3. **The backend stays a backend.** Tier 2 owns identity, persistence, the
   question lifecycle and the task queue. It does not grow a numerical stack.

## Pull, not push

The backend has **no dispatch code**. `ExtendedAggregatorController` writes rows
into `Tasks` and `cache_input_for_aggregation` and exposes endpoints to drain
them; nothing on the backend ever calls out. Tier 4 is therefore a poller.

This was not a Tier 4 design choice — it is what the v2 backend implements. The
`ExternalAggregator` table has `URL` and `last_heart_beat` columns that suggest
a push or health-check model was contemplated, but no code reads or writes
them, and `RegisterMethod` silently drops the URL. Until that changes, pull is
the only model that works.

Consequences worth knowing:

* Latency is bounded by the poll interval, not by the backend.
* Tier 4 can be restarted freely; unclaimed work waits in the queue.
* Tier 4 needs no inbound network exposure. The FastAPI surface is for
  operators, not for the backend.

## Module boundaries

```
contracts/   ← the only module that knows the backend's JSON shapes
   │
client/      ← the only module that speaks HTTP
   │
worker/      ← orchestration: what to claim, when, and what to do on failure
   │
methods/     ← pure functions. No I/O. Where the research code goes.
```

The dependency arrow never points backwards. A method cannot make an HTTP call
because it is never handed a client, which is deliberate: it keeps research code
testable and keeps retry/idempotency concerns in one place.

`app.py` and `cli.py` are both thin adapters over the same worker.

## Failure handling

A method that raises does **not** submit a result, and the task is left in
`collected` rather than being pushed back to `pending`. Two reasons:

* the backend exposes no endpoint to reset a task, so "put it back" is not
  actually available;
* a task that deterministically crashes the worker would otherwise be retried
  forever. A stuck `collected` task is a visible, diagnosable state.

Backend errors (network, 5xx) are caught per-task so one bad task cannot kill a
polling pass, and per-pass so a backend restart cannot kill the poller.

Token expiry is handled in the client: a 401 triggers one re-authentication and
one retry.

## Idempotency and concurrency

`Collect_Task` does not check the task's current status before stamping it, so
it is **not** a mutual-exclusion primitive — two workers polling simultaneously
can both "claim" the same task and both submit results, producing duplicate
`AggregatedAnswerDataComponent` rows.

For the single-worker pilot this is fine and Tier 4 does not work around it.
Before running more than one worker, the backend needs a conditional claim. See
`OPEN_QUESTIONS.md`.

## What a method sees

A method receives an `InputStack` — the parsed snapshot — and returns a
`MethodResult`:

```python
@registry.register("int_num", "Median")
def median(stack: InputStack) -> MethodResult:
    return MethodResult(
        json_data={"method": "Median", "result": {"median": ...}},
        description="...",
        meta={"diagnostics": "..."},
    )
```

`json_data` becomes `AggregatedAnswerDataComponent.JsonData` verbatim and is
what the frontend renders — so it is a frontend-facing contract, not an internal
one. `meta` is folded into the task's `execution_log` instead, so it can be as
verbose as is useful without affecting display.

The convention the built-ins follow is
`{method, input_name, n, skipped, result}` with `result` a nested object. That
keeps the envelope stable across methods while letting the display layer branch
on `method` — the same reasoning behind the existing `PercentileBandsSummary`
placeholder's method-agnostic envelope.

## Three separate registrations

Adding a method touches three places, and they are genuinely independent:

| Where | What it does | Consequence if you skip it |
|---|---|---|
| Tier 4 `registry.register(...)` | lets this worker execute the method | tasks are left pending |
| Backend `DiscoveryRecord:AggregationMethods` | lets a proposer select it in the UI | nobody can request it |
| Backend `ExternalAggregator` via `RegisterMethod` | human-facing catalogue | nothing breaks — it routes nothing |

Plus: the data type must exist in `DiscoveryRecord:data_types_schemas`, or the
backend cannot build a snapshot for it at all.

## What is deliberately not here

* **Real aggregation algorithms.** The built-ins are wiring proof.
* **A database.** Tier 4 holds no state between tasks; the snapshot is the
  input and the result row is the output.
* **Retry/backoff policy.** Failures are surfaced, not papered over, until
  there is operational experience to base a policy on.
* **Deployment.** No Dockerfile or compose entry yet; the other three tiers are
  containerised and Tier 4 will need to join that stack.
