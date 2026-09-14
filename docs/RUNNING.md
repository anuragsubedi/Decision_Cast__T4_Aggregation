# Running, debugging and deploying Tier 4

## 1. Pointing at the backend

**The scheme must match the port.** The backend's launch profiles are:

| Profile   | Binds                                                              | Notes                                                |
| --------- | ------------------------------------------------------------------ | ---------------------------------------------------- |
| `http`  | `http://localhost:8844`                                          | plain HTTP only                                      |
| `https` | `https://localhost:8855` **and** `http://localhost:8844` | `UseHttpsRedirection()` 307-redirects 8844 → 8855 |

So under the `https` profile, `https://localhost:8855` is the reliable target.

The dev certificate is self-signed, hence `TIER4_BE_VERIFY_TLS=false` for
localhost. **Turn verification on for any real deployment.**

Inside a Docker network there is no TLS termination between containers, so
`http://dcast_t2_backend:8844` is correct there.

## 2. Running the API

`tier4/app.py` is a module inside the `tier4` package, so it must be addressed
as `tier4.app:app`, and the package must be importable.

```bash
make install          # includes `pip install -e .`, which is what makes
                      # `tier4` importable from any directory
make dev              # uvicorn with --reload, port 8901
make serve            # no reload, binds 0.0.0.0 (production-shaped)
```

Equivalent raw commands, from the project root:

```bash
.venv/bin/python -m uvicorn tier4.app:app --reload --port 8901
```

**What does not work, and why:**

| Command                                    | Why it fails                                                                                                                                                               |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `uvicorn app:app` from inside `tier4/` | `app.py` loads as a *top-level* module, so its `from tier4.client...` imports have no package to resolve against → `ModuleNotFoundError: No module named 'tier4'` |
| `uvicorn app:app` from the project root  | there is no top-level`app` module → `Could not import module "app"`                                                                                                   |

Running `pip install -e .` fixes the first case permanently, because `tier4`
then resolves from site-packages regardless of cwd.

### `uvicorn` vs `fastapi run`

Plain `pip install fastapi` gives you a `fastapi` binary that is only a **stub**
— running it prints *"To use the fastapi command, please install
fastapi[standard]"*. This project now depends on `fastapi-cli` explicitly, so
both work:

```bash
fastapi dev tier4/app.py     # reload on, binds 127.0.0.1  -- development
fastapi run tier4/app.py     # reload off, binds 0.0.0.0   -- production
```

`fastapi dev/run` is a thin wrapper that shells out to uvicorn. It is friendlier
(it takes a *file path*, so the package-import trap above does not bite, and it
prints the docs URL). It is also less explicit about what it is doing and gives
you less control over worker counts and lifecycle.

**Recommendation:** `fastapi dev` for quick interactive work; `uvicorn tier4.app:app` everywhere that matters — the Makefile, the Dockerfile, the
debugger config — because an explicit import path is what the container and the
debugger both need anyway. Do not mix the two in scripts.

## 3. API docs (Swagger)

Already configured — FastAPI generates it from the route signatures, nothing
was set up by hand:

| URL                                    | What                     |
| -------------------------------------- | ------------------------ |
| `http://localhost:8901/docs`         | Swagger UI (interactive) |
| `http://localhost:8901/redoc`        | ReDoc (reference-style)  |
| `http://localhost:8901/openapi.json` | the raw OpenAPI 3 spec   |

Currently documented: `GET /health`, `GET /methods`, `POST /run-once`,
`POST /register-methods`.

Note the endpoints return plain dicts, so the spec has no response *schemas* —
only paths and methods. If Tier 4's HTTP surface ever becomes something another
system consumes, give the endpoints Pydantic `response_model`s and the schemas
appear for free. It is not worth doing while the surface is operator-only,
since the contract that actually matters is the backend's, documented in
[`BE_CONTRACT.md`](./BE_CONTRACT.md).

## 4. Debugging with breakpoints

`.vscode/launch.json` ships with seven configurations. The two you want most:

* **"tier4: drain queue once (CLI)"** — best for exploring the flow. Set
  breakpoints anywhere in `tier4/` and step through poll → peek → claim →
  fetch → compute → submit. Seed some work first with `make seed`.
* **"tier4: FastAPI server (no reload, debuggable)"** — then hit
  `POST /run-once` from Swagger UI and break inside the handler.

**The single biggest gotcha: `--reload` breaks breakpoints.** Uvicorn's
reloader runs your app in a *child* process that the debugger never attached
to, so breakpoints are silently ignored — no error, they just never hit. The
debug configurations therefore omit `--reload` deliberately. Use reload when
you are not debugging, and not otherwise.

`justMyCode: false` is set throughout, so you can also step into httpx,
pydantic and FastAPI when a problem turns out to be below your own code.

To debug a worker running elsewhere (a container, another shell):

```bash
python -m debugpy --listen 5678 --wait-for-client -m tier4.cli poll
# or: make debug     (does this for the API)
```

then attach with **"tier4: attach to running process"**.

For quick one-offs without VS Code, `breakpoint()` in the source drops you into
pdb, which works fine under `make once`.

## 5. Managing it going forward

**Configuration.** `tier4/config.py` is the schema — one line per setting
giving its name, type and default. `.env` is one *value source*, not the
schema. Precedence, highest first:

```
real environment variables  >  .env at the project root  >  defaults in config.py
```

That ordering is what lets one image run everywhere: compose sets real
environment variables and they win. **Never bake a `.env` into an image.**

Three consequences worth knowing:

* **A key in `.env` with no matching field in `config.py` is silently
  ignored** (`extra="ignore"`). If a setting "isn't taking effect", check it
  has a field first.
* **Never read settings with `os.environ.get("TIER4_...")`.**
  pydantic-settings loads `.env` into the model but does *not* export it to the
  process environment, so an `os.environ` lookup ignores `.env` entirely. This
  was a live bug: `TIER4_AUTOSTART_POLLER` was read from `os.environ`, so
  setting it in `.env` did nothing.
* **`TIER4_BE_EMAIL` and `TIER4_BE_PASSWORD` are required and have no
  defaults**, so a misconfigured deployment fails at startup rather than
  silently trying a dev password. Nothing secret is committed in `config.py`;
  `be_password` is a `SecretStr`, so it renders as `**********` anywhere the
  config is dumped.

The effective configuration is visible at runtime — `GET /health` and
`tier4 health` both report every setting (secrets redacted), so "is the poller
actually on, and at what interval?" is answerable from one call rather than by
guessing which `.env` the process read.

**Process model.** One container can both poll and serve
(`TIER4_AUTOSTART_POLLER=1`, which the Dockerfile sets). To scale, split them:
run the image once with the poller off for the API, and once with
`python -m tier4.cli poll` for the worker.

**Before running more than one poller**, the backend needs a conditional claim
on `Collect_Task` — today it does not check the task's current status, so two
workers can claim the same task and produce duplicate results. This is open
question #2 in [`OPEN_QUESTIONS.md`](./OPEN_QUESTIONS.md).

**Deployment** follows the pattern the other three tiers already use — a Docker
Hub image under the `binarystash` org and a service entry in the host's
`~/d-cast-services/docker-compose.yml`. A compose entry looks roughly like:

```yaml
  dcast_t4_aggregation:
    image: binarystash/decisioncast_tier4:latest
    container_name: dcast_t4_aggregation
    restart: unless-stopped
    environment:
      TIER4_BE_BASE_URL: http://dcast_t2_backend:8844
      TIER4_BE_EMAIL: ${T4_EMAIL}
      TIER4_BE_PASSWORD: ${T4_PASSWORD}
      TIER4_AUTOSTART_POLLER: "1"
      TIER4_POLL_INTERVAL_SECONDS: "10"
    depends_on: [dcast_t2_backend]
    ports: ["8901:8901"]
```

**Credentials.** The `.env` currently carries the aggregator password in
plaintext, mirroring the backend's own hardcoded default in `Program.cs`. Both
should move to secrets before this is deployed anywhere real — tracked as open
question #11.

**Dependencies** are pinned loosely in `pyproject.toml`. Since Tier 4 will grow
a numerical stack (numpy/scipy/scikit-learn, possibly torch) for the real
aggregations, consider a lockfile at that point; the current dependency set is
small enough not to need one yet.
