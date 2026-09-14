# Open questions and known gaps

Raised by building the Tier 4 scaffold against the v2 backend on 2026-09-09.
Background analysis:
`decisioncast_v1/documentations/exploration_reports/2026-09-09__v2-backend-aggregation-orchestration.md`.

## For Mohamed — backend changes

### 1. `input_stack_cacheController` has no `[Authorize]` attribute

Every other controller in the API is role-guarded. This one is not, so

```
GET /api/input_stack_cache/cache_input_for_aggregation
```

returns **every cached snapshot to an unauthenticated caller** — and snapshots
contain responder email addresses. This should be fixed before anything is
deployed. Tier 4 does not use this controller (it uses the guarded
`ExtendedAggregator` peek/collect routes), so adding the attribute breaks
nothing on our side.

### 2. Task claiming is not exclusive

`Collect_Task` stamps and sets `status = "collected"` without checking the
current status, so two workers polling concurrently can both claim the same
task and both submit results, producing duplicate result rows.

Fine for the single-worker pilot. Before scaling out, the claim needs to be
conditional — update `WHERE TaskID = @id AND status = 'pending'` and return a
409 when no row was affected.

### 3. Silent failure during fan-out

`Create_QuestionInquiry_agg_task` returns `0` when the question-inquiry is
missing or the snapshot cannot be built, and `0` is then appended to the
request's `input_cache_stack_key_s` CSV as though it were a task id. The caller
gets a success response describing a task that does not exist.

This is how a `percentile_curve` request fails today (see §6).

### 4. No way to reset a failed task

A task that fails mid-flight stays in `collected` forever — there is no endpoint
to return it to `pending`. Tier 4 deliberately does not try to work around this.
A `Release_Task` endpoint, or a timeout that reclaims stale `collected` tasks,
would make the queue self-healing.

### 5. `Median` and `Sum` — whose job?

Both are advertised in `DiscoveryRecord:AggregationMethods` for `int_num` but
neither appears in `MainAggregationService`'s switch, so the proposer UI offers
two methods that silently do nothing through the legacy path.

Tier 4 now implements both, which resolves it if that was the intent. **Needs
confirming** — if they were meant to be C# methods, Tier 4 should drop them to
avoid two engines claiming the same method name.

### 6. `curve` and `percentile_curve` are missing from `data_types_schemas`

`CreateAnswerStack_Snapshot` fails outright for any data type absent from that
config map. Currently it holds only `text`, `int_num`, `bool_flag`, `raw_curve`.

Combined with §3, submitting a `percentile_curve` aggregation request against
v2 today returns success and produces a phantom task id `0`.

Two things are needed: the config entries, and a richer schema than a bare
type-name string (see §8).

---

## Design decisions needed

### 7. `schema.value` is too thin for structured payloads

The snapshot's schema half is a bare type-name string — `"integer"`, `"array"`.
That is enough for `int_num`. It is not enough to tell a worker how to parse a
`percentile_curve` payload, which is three ordered named bands of `[week, y]`
pairs.

This is a config edit rather than a code change, which makes it cheap. Worth
deciding the shape before the percentile work lands, e.g.

```json
"percentile_curve": {
  "value": "object",
  "bands": ["lower", "median", "upper"],
  "point": ["week", "y"]
}
```

Tier 4's `InputStack.schema_` already carries the whole object through, so a
richer schema needs no Tier 4 change to *arrive* — only methods that use it.

### 8. Deployment

The other three tiers are containerised and released to
`decisioncast.cc.lehigh.edu` via Docker Hub images. Tier 4's  Dockerfile  needs to join that stack, which also means deciding
where its credentials come from (currently a `.env` mirroring hardcoded backend defaults).
