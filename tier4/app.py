"""FastAPI surface for the Tier 4 service.

The worker is pull-based, so this HTTP surface is *not* how work arrives. It
exists for three things:

* **Operability** — ``/health`` and ``/methods`` let you see what a running
  instance is and what it claims to be able to do.
* **Manual control** — ``/run-once`` drives exactly one pass over the pending
  queue, which is how you drive the pipeline during development without leaving
  a poller running.
* **A landing place for push** — if the backend ever uses the
  ``ExternalAggregator.URL`` column it added, the notify endpoint belongs here.

The background poller is started only when ``TIER4_AUTOSTART_POLLER`` is true,
so importing this module for a test or a one-shot run does not start draining
the real queue. That flag is read through ``settings`` — reading it from
``os.environ`` would ignore ``.env``, since pydantic-settings loads the file
into the model without exporting it to the process environment.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException

from tier4.client.decisioncast import BackendError, DecisionCastClient
from tier4.config import settings
from tier4.methods import builtin  # noqa: F401 - import registers the methods
from tier4.methods.registry import registry
from tier4.worker.loop import AggregationWorker

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("tier4")

_poller_stop = threading.Event()

# Observed poller state, as opposed to the configured intent in
# `settings.autostart_poller`. /health reports both, because "configured on but
# not actually running" is exactly the situation worth noticing.
_poller_state: dict[str, Any] = {"running": False, "last_pass": None}


def build_worker() -> AggregationWorker:
    client = DecisionCastClient(
        base_url=settings.be_base_url,
        email=settings.be_email,
        password=settings.be_password.get_secret_value(),
        timeout=settings.request_timeout_seconds,
        verify_tls=settings.be_verify_tls,
    )
    return AggregationWorker(
        client=client,
        registry=registry,
        aggregator_name=settings.aggregator_name,
        claim_unknown_methods=settings.claim_unknown_methods,
    )


def _poll_loop() -> None:
    worker = build_worker()
    while not _poller_stop.is_set():
        try:
            report = worker.run_once()
            _poller_state["last_pass"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                **report.as_dict(),
            }
        except BackendError as exc:
            log.error("background poll failed: %s", exc)
            _poller_state["last_pass"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("background poll raised")
            _poller_state["last_pass"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            }
        _poller_stop.wait(settings.poll_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    thread: threading.Thread | None = None
    # Read through `settings`, never os.environ: pydantic-settings loads .env
    # into the model and does NOT export it to the process environment, so an
    # os.environ lookup here would ignore .env entirely.
    if settings.autostart_poller:
        log.info(
            "starting background poller, one pass every %ss",
            settings.poll_interval_seconds,
        )
        thread = threading.Thread(target=_poll_loop, daemon=True, name="tier4-poller")
        thread.start()
        _poller_state["running"] = True
    else:
        log.info("background poller disabled (set TIER4_AUTOSTART_POLLER=true to enable)")
    yield
    _poller_stop.set()
    _poller_state["running"] = False
    if thread is not None:
        thread.join(timeout=5)


app = FastAPI(
    title="DecisionCast Tier 4 — Aggregation Service",
    description=(
        "Pull-based data-science worker for the DecisionCast platform. Drains the "
        "v2 backend's aggregation task queue and returns results into the same "
        "AggregatedAnswerDataComponent table the in-process C# methods use."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness, effective configuration, and backend reachability.

    The whole effective config is reported (secrets redacted) so that
    "what is this instance actually doing?" is answerable from one call —
    without SSHing in to read a .env that may not even be the active source.
    """
    status: dict[str, Any] = {
        "service": "tier4-aggregation",
        "version": app.version,
        "registered_methods": len(registry),
        "poller": {
            # Configured intent vs. observed reality. A mismatch means the
            # poller thread died, which is otherwise invisible.
            "configured": settings.autostart_poller,
            "running": _poller_state["running"],
            "interval_seconds": settings.poll_interval_seconds,
            "last_pass": _poller_state["last_pass"],
        },
        "config": settings.public_summary(),
    }

    try:
        client = DecisionCastClient(
            settings.be_base_url,
            settings.be_email,
            settings.be_password.get_secret_value(),
            timeout=5.0,
            verify_tls=settings.be_verify_tls,
        )
        with client:
            info = client.whoami()
        status["backend"] = "reachable"
        status["identity"] = {"email": info.get("email"), "roles": info.get("roles")}
    except Exception as exc:  # noqa: BLE001
        status["backend"] = "unreachable"
        status["error"] = str(exc)
    return status


@app.get("/methods")
def methods() -> list[dict[str, Any]]:
    """Everything this worker can execute, keyed the way the backend keys it."""
    return [
        {
            "data_type": spec.data_type,
            "method_name": spec.method_name,
            "description": spec.description,
            "meta": spec.meta,
        }
        for spec in registry.all()
    ]


@app.post("/run-once")
def run_once() -> dict[str, Any]:
    """Drive exactly one pass over the pending queue and report what happened."""
    worker = build_worker()
    try:
        with worker.client:
            return worker.run_once().as_dict()
    except BackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/register-methods")
def register_methods() -> dict[str, Any]:
    """Announce every registered method to the backend's ExternalAggregator table.

    Deliberately manual, not run at boot: the backend's ``RegisterMethod`` has no
    de-duplication, so calling this repeatedly accumulates duplicate rows.
    """
    from tier4.contracts.backend import ExternalAggregatorRegistration

    worker = build_worker()
    announced: list[str] = []
    with worker.client as client:
        for spec in registry.all():
            client.register_method(
                ExternalAggregatorRegistration(
                    aggregator_service_name=settings.aggregator_name,
                    data_type=spec.data_type,
                    method_name=spec.method_name,
                    description=spec.description,
                    meta_data={"source": "tier4", **spec.meta},
                )
            )
            announced.append(f"{spec.data_type}/{spec.method_name}")
    return {"registered": announced}
