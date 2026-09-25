from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, ListToolsResult, Tool

from student_agent import cli
from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.evidence import TOOL_ACCESS
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.policy_engine import PARTIES, CaseRequest, PolicyError, amount
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.verifier import VerificationError, VerifierAgent, check_lifecycle
from student_agent.workflow import solve_case


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def fixture_case() -> dict:
    return {
        "case_id": "CASE_001",
        "order_id": "order-1",
        "policy_version": "fixture-v1",
        "claims": [{"claim_id": "claim-1", "issue": "payment_mismatch"}],
    }


def fixture_policy() -> dict:
    """Test-only machine-readable policy, never submitted as MCP evidence."""
    priority = [
        "canceled_order_paid",
        "unavailable_order_paid",
        "refund_failed",
        "refund_pending",
        "duplicate_charge",
        "late_delivery_seller",
        "late_delivery_logistics",
        "payment_mismatch",
        "valid_split_payment",
        "unsupported_claim",
        "insufficient_evidence",
    ]
    modes = {
        "canceled_order_paid": "unrefunded",
        "unavailable_order_paid": "unrefunded",
        "late_delivery_seller": "freight",
        "late_delivery_logistics": "freight",
        "payment_mismatch": "overpayment",
        "duplicate_charge": "duplicate",
        "refund_failed": "failed",
    }
    rules = {}
    for issue in priority:
        status = (
            "needs_investigation"
            if issue == "insufficient_evidence"
            else "no_action"
            if issue in {"valid_split_payment", "unsupported_claim"}
            else "action_required"
        )
        party = {
            "late_delivery_seller": "seller",
            "late_delivery_logistics": "logistics_provider",
            "canceled_order_paid": "platform",
            "unavailable_order_paid": "platform",
        }.get(issue, sorted(PARTIES[issue])[0])
        rules[issue] = {
            "case_status": status,
            "cause_code": issue.upper(),
            "responsible_party": party,
            "refund_mode": modes.get(issue, "none"),
            "refund_reason_code": "FIXTURE_REFUND",
            "actions": [f"ACTION_{issue.upper()}"],
        }
    return {
        "policy_version": "fixture-v1",
        "currency": "BRL",
        "rules": rules,
        "issue_priority": priority,
        "payment_tolerance_brl": 0.01,
    }


def fixture_data() -> dict:
    payments = [{"payment_reference": "payment-1", "status": "captured", "amount_brl": 100}]
    return {
        "get_order": {"order_id": "order-1", "order_status": "delivered", "total_brl": 100},
        "get_order_items": {
            "order_id": "order-1",
            "items": [
                {"item_id": "item-1", "seller_id": "seller-1", "price": 90, "freight_value": 10}
            ],
        },
        "get_order_payments": {"order_id": "order-1", "payments": payments},
        "get_payment_timeline": {"order_id": "order-1", "payments": deepcopy(payments)},
        "get_refund_timeline": {"order_id": "order-1", "refunds": []},
        "get_shipment_summary": {
            "order_id": "order-1",
            "shipments": [
                {
                    "shipment_id": "shipment-1",
                    "seller_id": "seller-1",
                    "logistics_provider_id": "carrier-1",
                    "promised_at": "2026-01-10T00:00:00Z",
                    "delivered_at": "2026-01-09T00:00:00Z",
                    "seller_handoff_due_at": "2026-01-03T00:00:00Z",
                    "seller_handoff_at": "2026-01-02T00:00:00Z",
                }
            ],
        },
        "get_policy": fixture_policy(),
    }


class FixtureSession:
    def __init__(self, data: dict):
        self.data = data
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self, *, params=None):
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    input_schema={
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            TOOL_ACCESS[name][2]: {"type": "string"},
                        },
                        "required": ["case_id", TOOL_ACCESS[name][2]],
                    },
                )
                for name in self.data
            ]
        )

    async def call_tool(self, name, *, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return CallToolResult(
            content=[],
            structured_content={
                "schema_version": "day09-mcp-evidence-v1",
                "domain": TOOL_ACCESS[name][1],
                "evidence_ref": "ev_fixture_" + name + "_01234567890123456789",
                "result_hash": "sha256:" + "a" * 64,
                "data": deepcopy(self.data[name]),
            },
        )


async def run(
    data: dict, contracts: Contracts, tmp_path: Path
) -> tuple[dict, TraceWriter, FixtureSession]:
    session = FixtureSession(data)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = await solve_case(fixture_case(), EvidenceGateway(session, contracts), trace)
    return output, trace, session


def configure_issue(data: dict, issue: str) -> None:
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        data["get_order"]["order_status"] = (
            "canceled" if issue == "canceled_order_paid" else "unavailable"
        )
    elif issue in {"late_delivery_seller", "late_delivery_logistics"}:
        row = data["get_shipment_summary"]["shipments"][0]
        row["delivered_at"] = "2026-01-12T00:00:00Z"
        if issue == "late_delivery_seller":
            row["seller_handoff_at"] = "2026-01-05T00:00:00Z"
    elif issue == "payment_mismatch":
        data["get_order_payments"]["payments"][0]["amount_brl"] = 130
    elif issue == "duplicate_charge":
        data["get_order_payments"]["payments"].append(
            {
                "payment_reference": "payment-2",
                "status": "captured",
                "amount_brl": 100,
                "duplicate_of": "payment-1",
            }
        )
    elif issue == "valid_split_payment":
        data["get_order_payments"]["payments"] = [
            {"payment_reference": f"payment-{i}", "status": "captured", "amount_brl": 50}
            for i in (1, 2)
        ]
    elif issue in {"refund_pending", "refund_failed"}:
        data["get_refund_timeline"]["refunds"] = [
            {
                "refund_id": "refund-1",
                "payment_reference": "payment-1",
                "amount_brl": 20,
                "status": "pending" if issue == "refund_pending" else "failed",
            }
        ]
    elif issue == "insufficient_evidence":
        data["get_shipment_summary"]["shipments"][0]["promised_at"] = None
    data["get_payment_timeline"]["payments"] = deepcopy(data["get_order_payments"]["payments"])


@pytest.mark.parametrize(
    "issue,refund",
    [
        ("canceled_order_paid", 100),
        ("unavailable_order_paid", 100),
        ("late_delivery_seller", 10),
        ("late_delivery_logistics", 10),
        ("payment_mismatch", 30),
        ("duplicate_charge", 100),
        ("valid_split_payment", 0),
        ("refund_pending", 0),
        ("refund_failed", 20),
        ("unsupported_claim", 0),
        ("insufficient_evidence", 0),
    ],
)
def test_issues_output_and_lifecycle(contracts, tmp_path, issue, refund):
    data = fixture_data()
    configure_issue(data, issue)
    output, trace, session = asyncio.run(run(data, contracts, tmp_path))
    assert output["assessment"]["primary_issue"] == issue
    assert output["financial_resolution"]["recommended_refund_brl"] == refund
    assert output["assessment"]["confidence"] < 1
    contracts.validate_output(output, "output")
    assert all(args["case_id"] == "CASE_001" for _, args in session.calls)
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")
    check_lifecycle(output, trace.events("CASE_001"), finalized=True)


def test_completed_and_pending_refunds_reduce_recommendation(contracts, tmp_path):
    data = fixture_data()
    configure_issue(data, "canceled_order_paid")
    data["get_refund_timeline"]["refunds"] = [
        {
            "refund_id": "r1",
            "payment_reference": "payment-1",
            "amount_brl": 30,
            "status": "completed",
        },
        {
            "refund_id": "r2",
            "payment_reference": "payment-1",
            "amount_brl": 20,
            "status": "pending",
        },
    ]
    output, _, _ = asyncio.run(run(data, contracts, tmp_path))
    assert output["financial_resolution"]["recommended_refund_brl"] == 50


@pytest.mark.parametrize("resolved", [False, True])
def test_authoritative_conflict_needs_priority_and_lowers_confidence(contracts, tmp_path, resolved):
    data = fixture_data()
    data["get_order"]["total_brl"] = 200
    if resolved:
        data["get_policy"]["source_priority"] = {"total": ["get_order_items", "get_order"]}
    output, _, _ = asyncio.run(run(data, contracts, tmp_path))
    assert output["assessment"]["primary_issue"] == (
        "unsupported_claim" if resolved else "insufficient_evidence"
    )
    assert output["assessment"]["confidence"] <= (0.85 if resolved else 0.45)
    assert output["data_conflicts"][0]["selected_source"] == (
        "get_order_items" if resolved else None
    )


def test_unknown_policy_shape_fails_before_final_decision(contracts, tmp_path):
    data = fixture_data()
    data["get_policy"] = {"policy_version": "fixture-v1", "description": "unknown shape"}
    with pytest.raises(PolicyError, match="POLICY_FORMAT_UNSUPPORTED"):
        asyncio.run(run(data, contracts, tmp_path))
    written = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert not any(event["event_type"] in {"policy_decided", "case_finalized"} for event in written)


@pytest.mark.parametrize("change", ["refund", "party", "confidence", "fake_ref", "field"])
def test_verifier_rejects_tampered_candidate(contracts, tmp_path, change):
    data = fixture_data()
    configure_issue(data, "late_delivery_seller")
    output, trace, _ = asyncio.run(run(data, contracts, tmp_path))
    if change == "refund":
        output["financial_resolution"]["recommended_refund_brl"] += 1
    elif change == "party":
        output["root_cause_analysis"]["responsible_parties"][0]["party_type"] = "logistics_provider"
    elif change == "confidence":
        output["assessment"]["confidence"] = 1
    elif change == "fake_ref":
        output["evidence_refs"].append("ev_unknown_01234567890123456789")
    else:
        output["debug"] = "extra"
    # Existing policy_decided event precedes the verifier's own VERIFY_PASS event;
    # pass only its input prefix to exercise candidate verification again.
    events = [
        event
        for event in trace.events("CASE_001")
        if event["event_type"] != "verification_completed"
    ]
    with pytest.raises(VerificationError):
        VerifierAgent(contracts).verify(
            fixture_case(), _collection_for_verifier(data, contracts, tmp_path), output, events
        )


def _collection_for_verifier(data, contracts, tmp_path):
    # A fresh collection of the same test evidence; refs and hashes stay equal.
    from student_agent.evidence import CaseScope
    from student_agent.specialists import SpecialistTask
    from student_agent.workflow import collect_case_evidence

    async def collect():
        tasks = [
            SpecialistTask(
                name,
                TOOL_ACCESS[name][0],
                name,
                {TOOL_ACCESS[name][2]: "fixture-v1" if name == "get_policy" else "order-1"},
            )
            for name in data
        ]
        return await collect_case_evidence(
            CaseScope("CASE_001", ("order-1",), ("fixture-v1",)),
            EvidenceGateway(FixtureSession(data), contracts),
            TraceWriter(tmp_path / "fresh-trace.jsonl", contracts),
            tasks,
        )

    return asyncio.run(collect())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, -1, "0.001"])
def test_money_refuses_invalid_or_finer_than_cent(bad):
    with pytest.raises(PolicyError):
        amount(bad)


def test_conflicting_order_scope_is_rejected():
    case = fixture_case()
    case["order_ids"] = ["other-order"]
    with pytest.raises(PolicyError, match="CASE_ORDER_SCOPE_CONFLICT"):
        CaseRequest.parse(case)


def test_empty_shipment_rows_do_not_disprove_delivery_claim(contracts, tmp_path):
    data = fixture_data()
    data["get_shipment_summary"]["shipments"] = []
    case = fixture_case()
    case["claims"][0]["issue"] = "late_delivery_logistics"
    session = FixtureSession(data)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(solve_case(case, EvidenceGateway(session, contracts), trace))
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


def test_cli_run_finalizes_and_validates_artifacts(monkeypatch, contracts, tmp_path):
    case = fixture_case()
    case_set = CaseSet("fixture-v1", "L3A", (case["case_id"],), {case["case_id"]: case})
    session = FixtureSession(fixture_data())

    @asynccontextmanager
    async def fake_connect(endpoint, api_key, loaded_contracts):
        assert endpoint == "fixture://gateway"
        assert api_key == "fixture-key"
        yield EvidenceGateway(session, loaded_contracts)

    monkeypatch.setattr(cli, "connect_gateway", fake_connect)
    monkeypatch.setattr(cli, "load_case_set", lambda root: case_set)
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda root: SimpleNamespace(mcp_endpoint="fixture://gateway", team_api_key="fixture-key"),
    )
    (tmp_path / "contracts" / "schemas").mkdir(parents=True)
    monkeypatch.setattr(cli, "Contracts", lambda path: contracts)
    asyncio.run(cli._run(tmp_path))
    outputs, trace_lines = validate_artifacts(tmp_path, case_set, contracts)
    assert outputs["CASE_001"]["assessment"]["primary_issue"] == "unsupported_claim"
    assert json.loads(trace_lines[-1])["event_type"] == "case_finalized"
    assert all(args["case_id"] == "CASE_001" for _, args in session.calls)


def test_verification_failure_never_finalizes(monkeypatch, contracts, tmp_path):
    from student_agent import workflow

    def fail_verification(self, case, collection, candidate, events):
        raise VerificationError("FIXTURE_VERIFIER_REJECTED")

    monkeypatch.setattr(workflow.VerifierAgent, "verify", fail_verification)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    with pytest.raises(VerificationError, match="FIXTURE_VERIFIER_REJECTED"):
        asyncio.run(
            solve_case(
                fixture_case(), EvidenceGateway(FixtureSession(fixture_data()), contracts), trace
            )
        )
    events = trace.events("CASE_001")
    assert events[-1]["event_type"] == "verification_completed"
    assert events[-1]["decision_code"] == "VERIFY_FAILED"
    assert all(event["event_type"] != "case_finalized" for event in events)


@pytest.mark.parametrize(
    "issue,carrier_at,shipping_limit_at,expected",
    [
        (
            "late_delivery_seller",
            "2018-02-26T09:00:00-03:00",
            "2018-02-22T09:00:00-03:00",
            "late_delivery_seller",
        ),
        (
            "late_delivery_logistics",
            "2018-02-21T09:00:00-03:00",
            "2018-02-22T09:00:00-03:00",
            "late_delivery_logistics",
        ),
    ],
)
def test_official_adapter_uses_as_of_handoff(
    contracts, tmp_path, issue, carrier_at, shipping_limit_at, expected
):
    case = {
        "case_id": "CASE_001",
        "opened_at": "2018-03-03T09:00:00-03:00",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": issue}],
        },
    }
    data = {
        "get_order": {
            "order_id": "order-1",
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-02-19T09:00:00-03:00",
            "order_delivered_customer_date": "2018-03-05T09:00:00-03:00",
            "order_estimated_delivery_date": "2018-03-01T09:00:00-03:00",
        },
        "get_order_items": [
            {
                "order_id": "order-1",
                "order_item_id": "item-1",
                "seller_id": "seller-1",
                "price": "79.00",
                "freight_value": "18.00",
            }
        ],
        "get_order_payments": [{"order_id": "order-1", "payment_value": "89.00"}],
        "get_payment_timeline": {
            "order_id": "order-1",
            "events": [
                {
                    "event_at": "2018-02-19T10:00:00-03:00",
                    "event_type": "captured",
                    "status": "confirmed",
                    "amount_brl": "89.00",
                }
            ],
        },
        "get_refund_timeline": {"order_id": "order-1", "events": []},
        "get_shipment_summary": {
            "order_id": "order-1",
            "delivered_carrier_at": carrier_at,
            "delivered_customer_at": "2018-03-05T09:00:00-03:00",
            "estimated_delivery_at": "2018-03-01T09:00:00-03:00",
            "shipping_limits": [
                {
                    "order_item_id": "item-1",
                    "seller_id": "seller-1",
                    "shipping_limit_at": shipping_limit_at,
                }
            ],
        },
        "get_policy": {
            "policy_version": "EC_POLICY_V1",
            "currency": "BRL",
            "rules": {
                issue: {
                    "case_status": "action_required",
                    "recommended_action": "refund_freight",
                    "refund_brl": 18,
                    "responsible_parties": [
                        {
                            "party_type": "seller"
                            if issue == "late_delivery_seller"
                            else "logistics_provider",
                            "party_id": "seller-1" if issue == "late_delivery_seller" else None,
                        }
                    ],
                }
            },
        },
    }
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(solve_case(case, EvidenceGateway(FixtureSession(data), contracts), trace))
    assert output["assessment"]["primary_issue"] == expected
    assert output["financial_resolution"]["recommended_refund_brl"] == 18
    contracts.validate_output(output, "official candidate")
