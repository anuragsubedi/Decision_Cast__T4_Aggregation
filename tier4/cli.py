"""Command-line entrypoints.

    python -m tier4.cli health        # who am I, can I reach the backend
    python -m tier4.cli methods       # what can this worker execute
    python -m tier4.cli once          # drain the pending queue once
    python -m tier4.cli poll          # drain it continuously
    python -m tier4.cli register      # announce methods to ExternalAggregator
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from tier4.client.decisioncast import BackendError, DecisionCastClient
from tier4.config import settings
from tier4.contracts.backend import ExternalAggregatorRegistration
from tier4.methods import builtin  # noqa: F401 - import registers the methods
from tier4.methods.registry import registry
from tier4.worker.loop import AggregationWorker


def _client() -> DecisionCastClient:
    return DecisionCastClient(
        base_url=settings.be_base_url,
        email=settings.be_email,
        password=settings.be_password.get_secret_value(),
        timeout=settings.request_timeout_seconds,
        verify_tls=settings.be_verify_tls,
    )


def _worker(client: DecisionCastClient) -> AggregationWorker:
    return AggregationWorker(
        client=client,
        registry=registry,
        aggregator_name=settings.aggregator_name,
        claim_unknown_methods=settings.claim_unknown_methods,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tier4")
    parser.add_argument(
        "command",
        choices=["health", "methods", "once", "poll", "register"],
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.command == "methods":
        print(
            json.dumps(
                [
                    {
                        "data_type": s.data_type,
                        "method_name": s.method_name,
                        "description": s.description,
                    }
                    for s in registry.all()
                ],
                indent=2,
            )
        )
        return 0

    try:
        with _client() as client:
            if args.command == "health":
                status: dict[str, Any] = {
                    "whoami": client.whoami(),
                    # be_password is a SecretStr, so it renders as ********
                    # rather than reaching the terminal or a captured log.
                    "config": settings.public_summary(),
                    "poller": {
                        "configured": settings.autostart_poller,
                        "interval_seconds": settings.poll_interval_seconds,
                        "note": "the CLI does not run the poller; "
                        "`tier4 poll` or the API with autostart does",
                    },
                }
                print(json.dumps(status, indent=2, default=str))

            elif args.command == "once":
                print(json.dumps(_worker(client).run_once().as_dict(), indent=2))
            elif args.command == "poll":
                _worker(client).run_forever(
                    poll_interval=settings.poll_interval_seconds,
                    max_iterations=settings.max_iterations,
                )
            elif args.command == "register":
                for spec in registry.all():
                    client.register_method(
                        ExternalAggregatorRegistration(
                            aggregator_service_name=settings.aggregator_name,
                            data_type=spec.data_type,
                            method_name=spec.method_name,
                            description=spec.description,
                            meta_data={"source": "tier4"},
                        )
                    )
                    print(f"registered {spec.data_type}/{spec.method_name}")
    except BackendError as exc:
        print(f"backend error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
