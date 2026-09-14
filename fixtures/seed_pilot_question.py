"""Create a real, aggregatable question in a DecisionCast v2 backend.

Tier 4 cannot be exercised against an empty database: the task queue is only
fed by ``POST api/ExtendedAggregator/aggregationrequest``, and that only
produces useful snapshots when a question has published answers behind it. This
script drives the full proposer/responder flow over the public API to produce
that state, then optionally submits the aggregation request.

Run against a local backend::

    python -m fixtures.seed_pilot_question
    python -m fixtures.seed_pilot_question --method Median --no-request

It is idempotent only in the weak sense that it creates a *new* question every
run — it never mutates or deletes existing data. Do not point it at production.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from tier4.config import settings

ADMIN_EMAIL = "admin@admin.com"
ADMIN_PASSWORD = "admin"

PROPOSER = {"username": "t4proposer@dcast.local", "password": "t4pass1", "roles": ["Proposer"]}
RESPONDERS = [
    {"username": "t4resp1@dcast.local", "password": "t4pass1", "roles": ["Responder"], "value": 12},
    {"username": "t4resp2@dcast.local", "password": "t4pass1", "roles": ["Responder"], "value": 30},
    {"username": "t4resp3@dcast.local", "password": "t4pass1", "roles": ["Responder"], "value": 21},
]


class Api:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            verify=settings.be_verify_tls,
            follow_redirects=True,
        )
        self.token: str | None = None

    def login(self, email: str, password: str) -> None:
        r = self.http.post("/api/Auth/login", json={"Email": email, "Password": password})
        r.raise_for_status()
        self.token = r.json()["token"]

    def call(self, method: str, path: str, payload: Any = None) -> Any:
        r = self.http.request(
            method,
            path,
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text


def ensure_user(admin: Api, user: dict[str, Any]) -> None:
    """Register a user, tolerating the 'already exists' case on a re-run."""
    try:
        admin.call(
            "POST",
            "/api/Auth/register",
            {
                "Username": user["username"],
                "Email": user["username"],
                "Password": user["password"],
                "Roles": user["roles"],
            },
        )
        print(f"  registered {user['username']} {user['roles']}")
    except RuntimeError as exc:
        if "DuplicateUserName" in str(exc) or "already taken" in str(exc):
            print(f"  {user['username']} already exists")
        else:
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=settings.be_base_url)
    parser.add_argument(
        "--method",
        default="Median",
        help="aggregation method name written onto the task (default: Median)",
    )
    parser.add_argument(
        "--no-request",
        action="store_true",
        help="create the question and answers but do not submit the aggregation request",
    )
    args = parser.parse_args(argv)

    print(f"backend: {args.base_url}")

    # --- 1. users -------------------------------------------------------
    print("\n[1] ensuring users exist")
    admin = Api(args.base_url)
    admin.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    ensure_user(admin, PROPOSER)
    for responder in RESPONDERS:
        ensure_user(admin, responder)

    # --- 2. question + inquiry -------------------------------------------
    print("\n[2] creating the question")
    proposer = Api(args.base_url)
    proposer.login(PROPOSER["username"], PROPOSER["password"])

    question = proposer.call(
        "POST",
        "/api/Questions",
        {
            "FK_ProposerEmail": PROPOSER["username"],
            "FK_BureauId": 1,
            "QuestionText": "Tier 4 pilot: peak weekly influenza hospitalisations?",
            "Description": "Fixture question used to exercise the Tier 4 aggregation pipeline.",
            "BackgroundInformation": "Synthetic. Not research data.",
            "State": "Draft",
        },
    )
    qid = question["questionId"]
    print(f"  question {qid} created in state {question['state']}")

    inquiry = proposer.call(
        "POST",
        "/api/QuestionInquiries",
        {
            "FK_QuestionId": qid,
            "DataType": "int_num",
            # Input_Name is required in v2 and must not contain spaces or
            # commas -- it becomes the data column header in the snapshot.
            "Input_Name": "peak_hospitalisations",
            "SortOrder": 1,
            "IsEnabled": True,
            "IsOptional": False,
        },
    )
    qiid = inquiry["questionInquiryId"]
    print(f"  inquiry {qiid} ({inquiry['dataType']} / {inquiry['input_Name']}) created")

    # --- 3. open the question --------------------------------------------
    # State is NOT settable via PUT /api/Questions/{id} -- that handler
    # explicitly restores the original state, because transitions are owned by
    # the config-driven FSM. They go through StateSchedules instead, and
    # force_immediate is the "apply now" variant of a scheduled transition.
    print("\n[3] moving the question to Open_for_answers")
    for state in ("Submitted", "Open_for_answers"):
        proposer.call(
            "POST",
            "/api/StateSchedules/force_immediate",
            {"FK_QuestionId": qid, "ToState": state},
        )
        print(f"  -> {state}")

    # --- 4. answers -------------------------------------------------------
    print("\n[4] collecting answers")
    for responder in RESPONDERS:
        api = Api(args.base_url)
        api.login(responder["username"], responder["password"])

        answer = api.call(
            "POST",
            "/api/Answers",
            {
                "FK_QuestionId": qid,
                "FK_ResponderEmail": responder["username"],
                "Rationale": "Fixture answer.",
                "IsPublished": False,
            },
        )
        aid = answer["answerId"]

        api.call(
            "POST",
            "/api/AnswerDataComponents",
            {
                "FK_AnswerId": aid,
                "FK_QuestionInquiryId": qiid,
                # The snapshot builder reads the "value" key out of this object.
                "AnswerDataJson": {"value": responder["value"]},
                "Description": "Fixture answer component.",
            },
        )

        answer["isPublished"] = True
        api.call("PUT", f"/api/Answers/{aid}", answer)
        print(f"  {responder['username']} answered {responder['value']} and published")

    if args.no_request:
        print(f"\nDone. question={qid} inquiry={qiid} (no aggregation request submitted)")
        return 0

    # --- 5. aggregation request -------------------------------------------
    print(f"\n[5] submitting the aggregation request (method={args.method})")
    request = proposer.call(
        "POST",
        "/api/ExtendedAggregator/aggregationrequest",
        {
            "FK_QuestionId": qid,
            "metaData": {"AggArray": [{"fK_QuestionInquiryId": qiid, "method": args.method}]},
        },
    )
    print(json.dumps(request, indent=2)[:900])
    print(
        f"\nDone. question={qid} inquiry={qiid} "
        f"aggregationRequest={request.get('aggregationRequestID')}"
    )
    print("Now run:  python -m tier4.cli once")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
