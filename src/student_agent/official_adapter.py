"""Adapter for the released L3A input bundle and EC_POLICY_V1 MCP payloads.

This module does not rewrite evidence. It selects facts as they stood when the
case opened, so unrelated earlier/later rows cannot authorize a verdict.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from . import OUTPUT_SCHEMA_VERSION
from .policy_engine import PARTIES, PolicyError, amount, date, numeric, valid_id

if TYPE_CHECKING:
    from .policy_engine import CaseRequest, Source


def _object(source: Source | None) -> dict[str, Any]:
    return source.data if source is not None and isinstance(source.data, dict) else {}


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    rows = data if isinstance(data, list) else data.get(key, []) if isinstance(data, dict) else []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PolicyError("OFFICIAL_EVIDENCE_ROWS_INVALID")
    return rows


def _bounded_events(data: Any, start: Any, end: Any) -> list[dict[str, Any]]:
    if start is None or end is None:
        return []
    result = []
    for event in _rows(data, "events"):
        observed = date(event.get("event_at"))
        if observed is not None and start <= observed <= end:
            result.append(event)
    return result


def _policy_rule(policy: dict[str, Any], issue: str) -> dict[str, Any]:
    rules = policy.get("rules")
    if not isinstance(rules, dict) or issue not in rules:
        raise PolicyError("OFFICIAL_POLICY_RULE_MISSING")
    rule = rules[issue]
    if not isinstance(rule, dict) or not {
        "case_status", "recommended_action", "refund_brl", "responsible_parties"
    } <= rule.keys():
        raise PolicyError("OFFICIAL_POLICY_RULE_INVALID")
    status = rule["case_status"]
    action = rule["recommended_action"]
    parties = rule["responsible_parties"]
    refund_value = amount(rule["refund_brl"])
    if (
        status not in {"action_required", "no_action", "needs_investigation"}
        or not isinstance(action, str)
        or not 1 <= len(action) <= 80
        or not isinstance(parties, list)
        or not 1 <= len(parties) <= 5
        or any(
            not isinstance(party, dict)
            or party.get("party_type") not in PARTIES[issue]
            or party.get("party_id") is not None
            and (not isinstance(party["party_id"], str) or len(party["party_id"]) > 128)
            for party in parties
        )
        or (status != "action_required" and refund_value > 0)
    ):
        raise PolicyError("OFFICIAL_POLICY_RULE_INVALID")
    return rule


def evaluate_official(
    request: CaseRequest, sources: list[Source], policy_source: Source
) -> dict[str, Any]:
    if len(request.orders) != 1 or request.as_of is None:
        raise PolicyError("OFFICIAL_CASE_SCOPE_UNSUPPORTED")
    policy = _object(policy_source)
    if policy.get("policy_version") != request.policy_version or policy.get("currency") != "BRL":
        raise PolicyError("OFFICIAL_POLICY_VERSION_MISMATCH")
    by_tool = {source.tool: source for source in sources}
    order_source = by_tool.get("get_order")
    order = _object(order_source)
    if order.get("order_id") != request.orders[0]:
        raise PolicyError("OFFICIAL_ORDER_EVIDENCE_MISSING")
    purchase = date(order.get("order_purchase_timestamp"))
    if purchase is None or purchase > request.as_of:
        raise PolicyError("OFFICIAL_ORDER_TIMELINE_INVALID")
    payment_source = by_tool.get("get_payment_timeline")
    payment = _object(payment_source)
    payment_events = _bounded_events(payment, purchase, request.as_of)
    captures = [
        event
        for event in payment_events
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"
    ]
    captured_total = sum((amount(event.get("amount_brl")) for event in captures), Decimal(0))
    refund_source = by_tool.get("get_refund_timeline")
    refund_events = _bounded_events(_object(refund_source), purchase, request.as_of)
    shipment_source = by_tool.get("get_shipment_summary")
    shipment = _object(shipment_source)
    item_source = by_tool.get("get_order_items")
    items = _rows(item_source.data, "items") if item_source is not None else []
    payment_rows_source = by_tool.get("get_order_payments")
    payment_rows = (
        _rows(payment_rows_source.data, "payments")
        if payment_rows_source is not None
        else []
    )

    # Order and shipment timestamps must agree before attributing a late delivery.
    order_delivered = date(order.get("order_delivered_customer_date"))
    order_estimated = date(order.get("order_estimated_delivery_date"))
    shipment_delivered = date(shipment.get("delivered_customer_at"))
    shipment_estimated = date(shipment.get("estimated_delivery_at"))
    dates_agree = (
        shipment_source is not None
        and order_delivered == shipment_delivered
        and order_estimated == shipment_estimated
    )
    observed_delivery = (
        order_delivered
        if order_delivered is not None and order_delivered <= request.as_of
        else request.as_of
    )
    is_late = (
        dates_agree
        and order_estimated is not None
        and observed_delivery > order_estimated
    )
    carrier_at = date(shipment.get("delivered_carrier_at"))
    shipping_limits = [
        date(row.get("shipping_limit_at"))
        for row in _rows(shipment, "shipping_limits")
    ]
    eligible_limits = [
        limit
        for limit in shipping_limits
        if limit is not None and purchase <= limit <= request.as_of
    ]
    seller_handoff_late = (
        bool(eligible_limits)
        and carrier_at is not None
        and carrier_at <= request.as_of
        and carrier_at > min(eligible_limits)
    )
    logistics_handoff_on_time = (
        bool(eligible_limits)
        and carrier_at is not None
        and carrier_at <= request.as_of
        and carrier_at <= min(eligible_limits)
    )
    statuses = {
        event.get("status")
        for event in refund_events
        if event.get("event_type") == "refund_requested"
    }
    issue = next(
        (claim["issue"] for claim in request.claims if claim["issue"] in PARTIES),
        "insufficient_evidence",
    )
    rule = _policy_rule(policy, issue) if issue != "insufficient_evidence" else None
    policy_refund = amount(rule["refund_brl"]) if rule is not None else Decimal(0)
    refs = {policy_source.ref, order_source.ref}
    if payment_source is not None:
        refs.add(payment_source.ref)
    if item_source is not None:
        refs.add(item_source.ref)
    if payment_rows_source is not None:
        refs.add(payment_rows_source.ref)
    if shipment_source is not None:
        refs.add(shipment_source.ref)
    if refund_source is not None and issue in {"refund_pending", "refund_failed"}:
        refs.add(refund_source.ref)

    supported = False
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        expected = "canceled" if issue == "canceled_order_paid" else "unavailable"
        supported = order.get("order_status") == expected and captured_total >= policy_refund > 0
    elif issue in {"late_delivery_seller", "late_delivery_logistics"}:
        handoff_matches = (
            seller_handoff_late
            if issue == "late_delivery_seller"
            else logistics_handoff_on_time
        )
        supported = bool(is_late and handoff_matches and captured_total >= policy_refund)
    elif issue == "payment_mismatch":
        supported = any(
            event.get("event_type") == "reconciliation_mismatch"
            and amount(event.get("amount_brl")) == policy_refund
            for event in payment_events
        )
    elif issue == "duplicate_charge":
        supported = (
            len(captures) >= 2
            and sum(amount(event["amount_brl"]) == policy_refund for event in captures) >= 2
            and captured_total >= policy_refund * 2
        )
    elif issue == "valid_split_payment":
        sequences = {row.get("payment_sequential") for row in payment_rows}
        item_totals = {
            amount(item.get("price")) + amount(item.get("freight_value")) for item in items
        }
        supported = (
            len(captures) >= 2
            and len(sequences) >= 2
            and captured_total in item_totals
            and policy_refund == 0
        )
    elif issue in {"refund_pending", "refund_failed"}:
        status = "pending" if issue == "refund_pending" else "failed"
        supported = status in statuses and (
            policy_refund == 0
            or any(
                event.get("status") == status
                and amount(event.get("amount_brl")) == policy_refund
                for event in refund_events
            )
        )
    elif issue == "unsupported_claim":
        supported = (
            order.get("order_status") == "delivered"
            and dates_agree
            and order_delivered is not None
            and order_estimated is not None
            and order_delivered <= order_estimated
            and not any(
                event.get("event_type") == "reconciliation_mismatch"
                for event in payment_events
            )
            and policy_refund == 0
        )

    if policy_refund > captured_total or not supported:
        issue = "insufficient_evidence"
        rule = None
        policy_refund = Decimal(0)
    if issue == "insufficient_evidence":
        status = "needs_investigation"
        action = "investigate_missing_or_conflicting_evidence"
        parties = [{"party_type": "unknown", "party_id": None}]
        confidence = 0.4
    else:
        status = rule["case_status"]
        action = rule["recommended_action"]
        parties = [
            {"party_type": party["party_type"], "party_id": party["party_id"]}
            for party in rule["responsible_parties"]
        ]
        confidence = 0.82 if issue == "unsupported_claim" else 0.9
    if any(source.warnings for source in sources):
        confidence = min(confidence, 0.75)

    item_ids = sorted({valid_id(row["order_item_id"]) for row in items if row.get("order_item_id")})
    seller_ids = sorted({valid_id(row["seller_id"]) for row in items if row.get("seller_id")})
    seller_ids = sorted(
        set(seller_ids)
        | {
            party["party_id"]
            for party in parties
            if party["party_type"] == "seller" and party["party_id"]
        }
    )
    payment_references = sorted(
        {valid_id(row["payment_reference"]) for row in payment_rows if row.get("payment_reference")}
    )
    shipment_ids = sorted(
        {
            valid_id(row["shipment_id"])
            for row in _rows(shipment, "shipments")
            if row.get("shipment_id")
        }
    )
    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": request.case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": {
            "order_ids": [request.orders[0]],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": sorted(refs),
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": numeric(policy_refund),
            "refund_lines": [
                {
                    "reason_code": issue.upper(),
                    "amount_brl": numeric(policy_refund),
                    "entity_id": request.orders[0],
                }
            ]
            if policy_refund > 0
            else [],
        },
        "resolution_actions": [action],
    }
    if request.claims:
        primary_claim = next(
            (claim for claim in request.claims if claim["issue"] != "requested_full_refund"),
            None,
        )
        output["claim_assessments"] = []
        for claim in request.claims:
            if claim["issue"] == "requested_full_refund":
                verdict = (
                    "insufficient_evidence"
                    if issue == "insufficient_evidence" or captured_total == 0
                    else "supported"
                    if policy_refund >= captured_total
                    else "partially_supported"
                    if policy_refund > 0
                    else "unsupported"
                )
            else:
                verdict = (
                    "supported" if claim is primary_claim and supported else "insufficient_evidence"
                )
            output["claim_assessments"].append(
                {
                    "claim_id": claim["claim_id"],
                    "verdict": verdict,
                    "confidence": min(confidence, 0.45)
                    if verdict == "insufficient_evidence"
                    else confidence,
                    "evidence_refs": sorted(refs),
                }
            )
    return output
