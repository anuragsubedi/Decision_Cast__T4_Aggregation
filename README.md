# DecisionCast Tier 4 — Aggregation Service

Python data-science microservice for the DecisionCast platform. It consumes the v2
backend's aggregation task queue, runs aggregation methods, and writes results
back into the same table the backend's in-process C# methods use.

**Status: scaffold.** The contract, the client, the worker loop and the method
registry are complete and verified end to end against a live v2 backend. The
methods that ship with it are placeholder aggregations — the real statistical work
(GP diversity/consensus, LLM rationale extraction, chimeric priming) is not
written yet.

## Where this sits

```
tier 1  PostgreSQL
tier 2  Decision_Cast__BE          .NET Core REST API      ← owns the task queue
tier 3  decisioncast_v1            Next.js frontend
tier 4  Decision_Cast__T4_Aggregation   ← this repo, Python/FastAPI
```

Tier 4 talks only to tier 2, over HTTP, as an authenticated user in the
`extended_aggregator` role. It has no database of its own and never touches
tier 1 directly.

**It is pull-based.** The backend has no dispatch code — it writes rows into a
`Tasks` table and exposes endpoints to drain them.

## Quick start

```bash
make install          # venv + `pip install -e .`
make health           # confirm identity and backend reachability
```

`make` with no target lists everything. The common ones:

| Command          | What                                                              |
| ---------------- | ----------------------------------------------------------------- |
| `make health`  | who am I, can I reach the backend                                 |
| `make methods` | what this worker can execute                                      |
| `make seed`    | create a question with published answers + an aggregation request |
| `make once`    | drain the pending queue exactly once                              |
| `make poll`    | drain it continuously                                             |
| `make dev`     | run the API with auto-reload on :8901                             |
| `make serve`   | run the API without reload (debugger-friendly)                    |
| `make test`    | run the test suite (needs no backend)                             |

A full local round trip is two commands:

```bash
make seed && make once
```

### Pointing at the backend

**The scheme must match the port.** The backend serves TLS on **:8855** and
plain HTTP on **:8844**, and under its `https` launch profile it 307-redirects
8844 → 8855. `.env` defaults to `https://localhost:8855`.

Sending `http://` to :8855 gets the connection dropped mid-handshake, which
surfaces as `httpx.RemoteProtocolError: Server disconnected without sending a response`. The client detects this case and says so explicitly.

The local dev certificate is self-signed, hence `TIER4_BE_VERIFY_TLS=false`.
**Turn verification on for any real deployment.**

### Configuration

`tier4/config.py` is the schema — one line per setting, with its type and
default. `.env` is a value source, not the schema; real environment variables
override it, which is how the container is configured.

`TIER4_BE_EMAIL` and `TIER4_BE_PASSWORD` are **required** (no defaults), so a
misconfigured deployment fails at startup instead of silently trying a dev
password. `GET /health` and `tier4 health` report the whole effective config
with secrets redacted.

### API docs

FastAPI generates these automatically — `/docs` (Swagger UI), `/redoc`, and
`/openapi.json` on whatever port the app is serving.

Full detail on running, debugging with breakpoints, and deploying:
[`docs/RUNNING.md`](docs/RUNNING.md).

## Layout

| Path                             | What                                                                                                        |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `tier4/contracts/backend.py`   | Typed mirror of the backend's C# aggregation models. The one place the cross-tier contract is written down. |
| `tier4/contracts/envelope.py`  | The snapshot envelope — the shape a method actually consumes.                                              |
| `tier4/client/decisioncast.py` | Authenticated client for the seven contract endpoints.                                                      |
| `tier4/methods/registry.py`    | Method lookup, keyed`(data_type, method_name)` the way the backend keys it.                               |
| `tier4/methods/builtin.py`     | `Median`, `Sum`, `Tier4Echo`. Proof-of-wiring, not research code.                                     |
| `tier4/worker/loop.py`         | poll → peek → claim → fetch → compute → submit.                                                        |
| `tier4/app.py`                 | FastAPI surface + optional background poller.                                                               |
| `tier4/cli.py`                 | Command-line entrypoints.                                                                                   |
| `fixtures/`                    | Drives the backend API to create aggregatable test data.                                                    |
| `docs/`                        | Architecture, the backend contract, running/debugging, and open questions.                                  |
| `Makefile`                     | Every command below; run`make` alone to list them.                                                        |
| `.vscode/launch.json`          | Debug configurations with breakpoints working.                                                              |
| `Dockerfile`                   | For joining the existing three-tier compose stack.                                                          |

## Adding an aggregation method

1. Write a pure function `InputStack -> MethodResult` and decorate it:

   ```python
   @registry.register("percentile_curve", "GPConsensus", description="...")
   def gp_consensus(stack: InputStack) -> MethodResult:
       ...
   ```
2. Add the method to the **backend's** `ext_config/appsettings.json` under
   `DiscoveryRecord:AggregationMethods` so proposers can select it. Tier 4
   executing a method and the UI offering it are two separate registrations.
3. Confirm the data type has an entry in `DiscoveryRecord:data_types_schemas`,
   or the backend cannot build a snapshot for it at all.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why the boundaries are
drawn where they are, and [`docs/BE_CONTRACT.md`](docs/BE_CONTRACT.md) for the
wire-level detail.
