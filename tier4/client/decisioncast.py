"""Authenticated HTTP client for the DecisionCast v2 backend.

Wraps the seven ``api/ExtendedAggregator`` endpoints that make up the external
aggregation contract, plus login. Everything returns a parsed contract model, so
a shape change on the backend surfaces as a ``ValidationError`` here.

The transport is deliberately dumb: no retries, no backoff, no connection
pooling beyond what httpx gives us. Failure handling belongs in the worker loop,
which knows whether a given failure should abandon a task or retry it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tier4.contracts.backend import (
    AggregatedAnswerDataComponent,
    AuthResponse,
    CacheInputRecord,
    CollectorRequest,
    ExternalAggregatorRegistration,
    TaskRecord,
    TaskResult,
)

log = logging.getLogger(__name__)


class BackendError(RuntimeError):
    """A non-2xx response from the backend, with the body attached."""

    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class DecisionCastClient:
    """Client for the tier-2 backend, authenticated as the aggregator identity."""

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        timeout: float = 30.0,
        verify_tls: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._token: str | None = None
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            # The backend's local dev cert is self-signed.
            verify=verify_tls,
            # Under the backend's "https" launch profile, UseHttpsRedirection
            # turns plain requests to :8844 into a 307 to :8855. Following it
            # means either base URL works.
            follow_redirects=True,
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "DecisionCastClient":
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def email(self) -> str:
        """This worker's own identity — ``Collect_Task`` cross-checks it."""
        return self._email

    # -- auth --------------------------------------------------------------

    def login(self) -> AuthResponse:
        """Exchange credentials for a bearer token and cache it."""
        payload = {"Email": self._email, "Password": self._password}
        auth = AuthResponse.model_validate(
            self._request("POST", "/api/Auth/login", json=payload, authed=False)
        )
        self._token = auth.token
        log.info("authenticated with the backend as %s", auth.email)
        return auth

    def whoami(self) -> dict[str, Any]:
        """``getinfo`` — useful to confirm the token carries the right role."""
        return self._request("GET", "/api/Auth/getinfo")

    # -- the external aggregation contract ---------------------------------

    def register_method(
        self, registration: ExternalAggregatorRegistration
    ) -> str:
        """Announce a method to the backend's ``ExternalAggregator`` registry.

        Note that the backend drops ``URL`` and ``last_heart_beat`` on insert,
        and there is no read endpoint and no de-duplication — calling this twice
        creates two rows. The worker therefore does not register on every boot.
        """
        return str(
            self._request(
                "POST",
                "/api/ExtendedAggregator/RegisterMethod",
                json=registration.model_dump(by_alias=True, exclude_none=True),
            )
        )

    def get_pending_tasks(self) -> list[TaskRecord]:
        """All tasks with ``status == "pending"``."""
        raw = self._request("GET", "/api/ExtendedAggregator/Get_Pending_Tasks")
        return [TaskRecord.model_validate(item) for item in raw or []]

    def peek_task(self, task_id: int) -> TaskRecord:
        """Read a task without changing its status."""
        return TaskRecord.model_validate(
            self._request("GET", f"/api/ExtendedAggregator/peek_on_task/{task_id}")
        )

    def collect_task(self, task_id: int) -> TaskRecord:
        """Claim a task, moving it ``pending -> collected``.

        The backend does **not** check the task's current status first, so this
        is not a safe mutual-exclusion primitive: two workers polling at the
        same time can both "claim" the same task. Fine for the single-worker
        pilot; see docs/OPEN_QUESTIONS.md before scaling out.
        """
        body = CollectorRequest(
            invoking_user_email=self._email,
            aggregator_name=self._aggregator_name,
        )
        return TaskRecord.model_validate(
            self._request(
                "POST",
                f"/api/ExtendedAggregator/Collect_Task/{task_id}",
                json=body.model_dump(by_alias=True),
            )
        )

    def collect_task_input(self, cache_key: int) -> CacheInputRecord:
        """Fetch the frozen input, moving the cache row ``snapshot -> collected``."""
        return CacheInputRecord.model_validate(
            self._request(
                "POST", f"/api/ExtendedAggregator/Collect_Task_Input/{cache_key}"
            )
        )

    def peek_task_input(self, cache_key: int) -> CacheInputRecord:
        """Read the frozen input without changing ``cache_state``."""
        return CacheInputRecord.model_validate(
            self._request(
                "GET", f"/api/ExtendedAggregator/peek_on_task_input/{cache_key}"
            )
        )

    def submit_task_result(self, result: TaskResult) -> dict[str, Any]:
        """Post a finished result, moving the task to ``completed``.

        The backend then re-checks sibling tasks and sets the parent request to
        ``completed`` or ``partially_completed``.
        """
        return self._request(
            "POST",
            "/api/ExtendedAggregator/submit_task_result",
            json=result.model_dump(by_alias=True, exclude_none=True),
        )

    # -- read-only helpers used by the fixtures and by diagnostics ----------

    def get_aggregated_data_components(
        self,
    ) -> list[AggregatedAnswerDataComponent]:
        """Every result row. Used to verify a round trip actually landed."""
        raw = self._request("GET", "/api/AggregatedAnswerDataComponents")
        return [AggregatedAnswerDataComponent.model_validate(i) for i in raw or []]

    # -- transport ---------------------------------------------------------

    _aggregator_name: str = "tier4-datascience"

    def set_aggregator_name(self, name: str) -> None:
        self._aggregator_name = name

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        authed: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if authed:
            if self._token is None:
                self.login()
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            response = self._http.request(method, path, json=json, headers=headers)
        except httpx.RemoteProtocolError as exc:
            # Almost always a scheme/port mismatch: a plain HTTP request sent to
            # a TLS port gets the connection closed mid-handshake. Say so,
            # because the raw error ("Server disconnected without sending a
            # response") gives no hint at all.
            if self.base_url.startswith("http://"):
                raise BackendError(
                    method,
                    path,
                    0,
                    f"Server at {self.base_url} closed the connection without responding. "
                    f"This usually means the port speaks HTTPS but the URL says http://. "
                    f"Try https:// instead (the backend serves TLS on :8855, plain HTTP on :8844).",
                ) from exc
            raise
        except httpx.ConnectError as exc:
            raise BackendError(
                method, path, 0, f"Cannot reach {self.base_url}: {exc}"
            ) from exc

        # A cached token can outlive its expiry mid-run; re-auth once and retry.
        if response.status_code == 401 and authed:
            log.info("token rejected, re-authenticating")
            self.login()
            headers["Authorization"] = f"Bearer {self._token}"
            response = self._http.request(method, path, json=json, headers=headers)

        if response.status_code >= 400:
            raise BackendError(method, path, response.status_code, response.text)

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # Some endpoints (RegisterMethod) return a bare string body.
            return response.text
