from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .contracts import ContractError, Contracts
from .policy_engine import PARTIES, PolicyEngine, amount

if TYPE_CHECKING:
    from .workflow import EvidenceCollection


class VerificationError(ValueError):
    pass


def check_consistency(output: dict[str, Any]) -> None:
    issue = output["assessment"]["primary_issue"]
    status = output["assessment"]["case_status"]
    financial = output["financial_resolution"]
    total = amount(financial["recommended_refund_brl"])
    lines = financial["refund_lines"]
    if total != sum((amount(line["amount_brl"]) for line in lines), Decimal(0)):
        raise VerificationError("REFUND_TOTAL_MISMATCH")
    if len({(line["entity_id"], line["reason_code"]) for line in lines}) != len(lines):
        raise VerificationError("REFUND_LINES_DUPLICATE")
    if any(line["entity_id"] not in output["affected_entities"]["order_ids"] for line in lines):
        raise VerificationError("REFUND_OUTSIDE_CASE")
    if status == "no_action" and total > 0:
        raise VerificationError("STATUS_REFUND_CONFLICT")
    if issue == "insufficient_evidence" and (status != "needs_investigation" or total > 0):
        raise VerificationError("INSUFFICIENT_EVIDENCE_CONFLICT")
    if issue in {"unsupported_claim", "valid_split_payment"} and status != "no_action":
        raise VerificationError("ISSUE_STATUS_CONFLICT")
    if total > 0 and (status != "action_required" or not output["resolution_actions"]):
        raise VerificationError("REFUND_ACTION_MISSING")
    causes = output["root_cause_analysis"]["ranked_causes"]
    if [cause["rank"] for cause in causes] != list(range(1, len(causes) + 1)):
        raise VerificationError("CAUSE_RANK_INVALID")
    parties = output["root_cause_analysis"]["responsible_parties"]
    if len(output["affected_entities"]["order_ids"]) == 1 and any(
        party["party_type"] not in PARTIES[issue] for party in parties
    ):
        raise VerificationError("ISSUE_PARTY_CONFLICT")
    if any(
        party["party_type"] == "seller"
        and party["party_id"] not in output["affected_entities"]["seller_ids"]
        for party in parties
    ):
        raise VerificationError("SELLER_SCOPE_INVALID")
    refs = set(output["evidence_refs"])
    if not refs or any(
        not set(claim["evidence_refs"]) <= refs for claim in output.get("claim_assessments", [])
    ):
        raise VerificationError("CLAIM_EVIDENCE_INVALID")
    if any(
        conflict["selected_source"] not in conflict["sources"]
        for conflict in output["data_conflicts"]
        if conflict["selected_source"] is not None
    ):
        raise VerificationError("CONFLICT_SOURCE_INVALID")
    ceiling = (
        0.45
        if issue == "insufficient_evidence"
        or any(conflict["selected_source"] is None for conflict in output["data_conflicts"])
        else 0.85
        if output["data_conflicts"]
        else 0.95
    )
    if output["assessment"]["confidence"] > ceiling:
        raise VerificationError("CONFIDENCE_TOO_HIGH")
    for claim in output.get("claim_assessments", []):
        claim_ceiling = 0.45 if claim["verdict"] == "insufficient_evidence" else ceiling
        if claim["confidence"] > claim_ceiling:
            raise VerificationError("CLAIM_CONFIDENCE_TOO_HIGH")


def check_lifecycle(output: dict[str, Any], events: list[dict], *, finalized: bool) -> None:
    events = [event for event in events if event["case_id"] == output["case_id"]]
    names = [event["event_type"] for event in events]
    expected_actors = {
        "case_received": "coordinator",
        "task_assigned": "coordinator",
        "policy_decided": "policy-agent",
        "verification_completed": "verifier",
        "case_finalized": "coordinator",
    }
    if any(
        event["actor"] != expected_actors[event["event_type"]]
        for event in events
        if event["event_type"] in expected_actors
    ):
        raise VerificationError("LIFECYCLE_ACTOR_INVALID")
    if not names or names[0] != "case_received" or names.count("case_received") != 1:
        raise VerificationError("LIFECYCLE_RECEIVE_INVALID")
    chain = ["case_received", "task_assigned", "tool_result_consumed", "handoff", "policy_decided"]
    if finalized:
        chain += ["verification_completed", "case_finalized"]
    index = -1
    for name in chain:
        index = next(
            (offset for offset in range(index + 1, len(names)) if names[offset] == name), -1
        )
        if index < 0:
            raise VerificationError("LIFECYCLE_MISSING_OR_OUT_OF_ORDER")
    if finalized:
        last_verification = max(
            i for i, name in enumerate(names) if name == "verification_completed"
        )
        last_policy = max(i for i, name in enumerate(names) if name == "policy_decided")
        if (
            names[-1] != "case_finalized"
            or names.count("case_finalized") != 1
            or last_verification <= last_policy
            or last_verification != len(names) - 2
            or events[last_verification].get("decision_code") != "VERIFY_PASS"
        ):
            raise VerificationError("LIFECYCLE_FINALIZE_INVALID")
    elif "case_finalized" in names:
        raise VerificationError("LIFECYCLE_PREMATURE_FINALIZATION")
    policy_index = max(i for i, name in enumerate(names) if name == "policy_decided")
    consumed = set().union(
        *(
            set(event.get("evidence_refs", []))
            for event in events[:policy_index]
            if event["event_type"] == "tool_result_consumed" and event.get("tool_name")
        )
    )
    if not set(output["evidence_refs"]) <= consumed:
        raise VerificationError("EVIDENCE_NOT_CONSUMED")
    if not any(
        event.get("target") and event["actor"] != event["target"]
        for event in events
        if event["event_type"] == "handoff"
    ):
        raise VerificationError("LIFECYCLE_HANDOFF_MISSING")


class VerifierAgent:
    def __init__(self, contracts: Contracts) -> None:
        self.contracts = contracts

    def verify(
        self,
        case: dict[str, Any],
        collection: EvidenceCollection,
        candidate: dict[str, Any],
        events: list[dict],
    ) -> None:
        try:
            json.dumps(candidate, allow_nan=False)
            self.contracts.validate_output(candidate, "candidate")
            if candidate["case_id"] != case["case_id"]:
                raise VerificationError("CASE_ID_MISMATCH")
            for ref, envelope in collection.evidence.items():
                self.contracts.validate_evidence(envelope, "stored evidence")
                if ref != envelope["evidence_ref"]:
                    raise VerificationError("EVIDENCE_REF_MISMATCH")
            for event in events:
                self.contracts.validate_trace(event, "trace")
            if len({event["event_id"] for event in events}) != len(events):
                raise VerificationError("EVENT_ID_DUPLICATE")
            check_consistency(candidate)
            check_lifecycle(candidate, events, finalized=False)
            required = set(self.contracts.scoring_policy()["workflow_required_events"])
            present = {
                event["event_type"] for event in events if event["case_id"] == case["case_id"]
            }
            if not (required - {"verification_completed", "case_finalized"}) <= present:
                raise VerificationError("SCORING_LIFECYCLE_INCOMPLETE")
            # Replay from the immutable evidence collection; never trust the
            # candidate's own declarations about monetary or party provenance.
            grounded = PolicyEngine().evaluate(case, collection)
            for key in (
                "assessment",
                "affected_entities",
                "root_cause_analysis",
                "evidence_refs",
                "data_conflicts",
                "financial_resolution",
                "resolution_actions",
                "claim_assessments",
            ):
                if key == "assessment":
                    if (
                        candidate[key]["primary_issue"] != grounded[key]["primary_issue"]
                        or candidate[key]["case_status"] != grounded[key]["case_status"]
                        or candidate[key]["confidence"] > grounded[key]["confidence"]
                    ):
                        raise VerificationError("ASSESSMENT_NOT_GROUNDED")
                elif candidate.get(key) != grounded.get(key):
                    raise VerificationError(f"{key.upper()}_NOT_GROUNDED")
        except VerificationError:
            raise
        except (ContractError, ValueError, TypeError, OverflowError) as exc:
            raise VerificationError("VERIFICATION_INVALID_DATA") from exc
