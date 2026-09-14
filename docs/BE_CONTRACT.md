# The tier 2 ↔ tier 4 contract

Everything here was verified against a running v2 backend at commit `eac4f3b`
on 2026-09-09. The payloads in `tests/test_contracts.py` are captured from that
run, so the test suite is the executable form of this document.

Backend source of truth: `Decision_Cast__BE/DecisionCast_BE_Api/Controllers/ExtendedAggregatorController.cs`.

---

## 1. Identity

Tier 4 authenticates as an ordinary user in the `extended_aggregator` role.

```
POST /api/Auth/login   {"Email": "...", "Password": "..."}
  -> {"username", "email", "token", "expiresAt_ISO_timestamp"}
```

The role is seeded at backend startup by `SeedExtendedAggregatorUserAsync`,
which creates `agguser@consumer.com` with a password hardcoded in the backend's
`Program.cs`. Every `api/ExtendedAggregator` route is guarded by
`[Authorize(Roles = "Proposer,Admin,extended_aggregator")]`.

`Collect_Task` additionally cross-checks that the `invoking_user_email` in the
request body equals the email on the bearer token, so the worker must know its
own identity — it cannot claim tasks anonymously.

## 2. Wire format

The backend calls `AddControllers()` without setting a `PropertyNamingPolicy`,
so ASP.NET Core applies `JsonSerializerDefaults.Web`:

* **Responses are camelCase.** .NET's camelCase policy lowercases only the
  leading uppercase run, and an underscore stops that run. So:

  | C# property | On the wire |
  |---|---|
  | `TaskID` | `taskID` |
  | `FK_AggRequestId` | `fK_AggRequestId` |
  | `FK_QuestionInquiryId` | `fK_QuestionInquiryId` |
  | `AggregationRequestID` | `aggregationRequestID` |
  | `JsonData` | `jsonData` |
  | `metaData`, `status`, `execution_log`, `input_cache_stack_key` | unchanged |

* **Requests are matched case-insensitively**, so either casing is accepted on
  the way in. Tier 4 sends the alias for symmetry.

## 3. The flow

```
  proposer                backend (tier 2)                 tier 4
     │                          │                             │
     ├─ POST aggregationrequest ┤                             │
     │                          │ fan-out: per AggArray entry │
     │                          │   1 cache_input_for_aggregation (snapshot)
     │                          │   1 Tasks row              (pending)
     │                          │                             │
     │                          │◄── GET Get_Pending_Tasks ───┤ poll
     │                          │◄── GET peek_on_task_input ──┤ what method?
     │                          │◄── POST Collect_Task ───────┤ claim  (→ collected)
     │                          │◄── POST Collect_Task_Input ─┤ fetch  (→ collected)
     │                          │                             │ compute
     │                          │◄── POST submit_task_result ─┤ return (→ completed)
     │                          │ writes AggregatedAnswerDataComponent
     │                          │ sets request completed / partially_completed
```

Tier 4 **peeks before claiming**. The method name lives on the cache row, not
the task, so the worker cannot decide whether it can handle a task until it has
looked at the input — and peeking leaves `cache_state` alone so a worker that
cannot handle the method leaves it available for one that can.

## 4. Endpoints

Base: `/api/ExtendedAggregator`

| Endpoint | Method | Effect |
|---|---|---|
| `Get_Pending_Tasks` | GET | all `Tasks` where `status == "pending"` |
| `peek_on_task/{taskId}` | GET | read a task, no mutation |
| `Collect_Task/{taskId}` | POST | body `{invoking_user_email, aggregator_name}`; stamps body into `metaData`, sets `status = "collected"` |
| `peek_on_task_input/{key}` | GET | read the cache row, no mutation |
| `Collect_Task_Input/{key}` | POST | returns the cache row **and** sets `cache_state = "collected"` |
| `submit_task_result` | POST | body `TaskResult`; see §6 |
| `RegisterMethod` | POST | inserts an `ExternalAggregator` row |
| `aggregationrequest` | POST | proposer-side fan-out; Tier 4 does not call this |

## 5. The snapshot envelope

This is the real interface. Built by `CreateAnswerStack_Snapshot`, stored on
`cache_input_for_aggregation.aggregation_input_stack`:

```json
{
  "columns":   ["row_ids", "peak_hospitalisations"],
  "schema":    {"value": "integer"},
  "csv_rows":  [["t4resp1@dcast.local", 12],
                ["t4resp2@dcast.local", 30],
                ["t4resp3@dcast.local", 21]],
  "row_count": 3
}
```

* `columns[1]` is the proposer-authored `QuestionInquiry.Input_Name`. The
  backend requires it non-empty and rejects spaces and commas in it, because it
  becomes a CSV-shaped column header.
* Only **published** answers appear (`Answer.IsPublished`).
* Each row is `[row_id, value]`, where `value` is whatever sat under the `value`
  key of the responder's `AnswerDataJson` — a scalar for `int_num` / `text` /
  `bool_flag`, a nested array for the curve types.
* **`row_id` is the responder's email address.** See `OPEN_QUESTIONS.md`.
* `schema.value` is a bare type-name string read from
  `DiscoveryRecord:data_types_schemas` in the backend's `ext_config`. A data
  type absent from that map causes snapshot construction to **fail**, and the
  failing task is silently emitted with id `0`.
* A responder who answered the same inquiry twice contributes two rows, so
  `row_count` counts rows, not responders.

## 6. Submitting a result

```json
{
  "taskID": 1,
  "submitUserEmail": "agguser@consumer.com",
  "execution_log": { "...": "free-form diagnostics" },
  "aggregatedAnswerDataComponent": {
    "jsonData": { "method": "Median", "n": 3, "result": {"median": 21.0} },
    "description": "Median over 3 responses",
    "aggregationMethodName": "Median",
    "status": "completed",
    "result_meta_data": {"min": 12.0, "max": 30.0}
  }
}
```

* `taskID` and a non-null `aggregatedAnswerDataComponent.jsonData` are required;
  everything else is optional.
* `FK_AggregationRequestID` and `FK_QuestionInquiryId` are **overwritten by the
  backend** from the task record, so a worker cannot mis-address a result.
* `aggregationMethodName`, `status` and `result_meta_data` are *not* populated
  by the backend, but they **are** persisted from whatever the worker sends —
  verified empirically. Tier 4 therefore owns those three fields.
* `jsonData` becomes what the frontend renders, so its shape is a
  frontend-facing contract. Keep `result` a nested object so the display layer
  can branch on `method` without the envelope changing shape.

## 7. State machines

```
Tasks.status                 pending → collected → completed
cache_input_for_aggregation  snapshot → collected
AggregatedAnswer_request     Received → partially_completed → completed
```

These are bare string literals in the C# — no enum, no config list, unlike the
question-state FSM. There is **no endpoint to reset a task**, so a task that
fails mid-flight stays in `collected` and needs manual intervention.

## 8. Registration is descriptive only

`RegisterMethod` inserts into `ExternalAggregator`, but:

* the backend **drops** `URL` and `last_heart_beat` on insert — `RegisterMethod`
  copies only name/type/method/description/metaData;
* there is no read endpoint and no de-duplication, so calling it twice creates
  two rows;
* nothing in the backend routes work based on this table.

So registration is a human-facing catalogue, not a dispatch mechanism. Tier 4
does not register at boot for that reason; use `tier4.cli register` deliberately.

For a method to be *selectable by a proposer*, it must be added to the
backend's `DiscoveryRecord:AggregationMethods` config — a separate, config-file
registration from both this table and Tier 4's own registry.
