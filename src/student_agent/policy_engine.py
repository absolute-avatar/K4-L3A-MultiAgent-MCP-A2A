from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from . import OUTPUT_SCHEMA_VERSION
from .evidence import TOOL_ACCESS

if TYPE_CHECKING:
    from .workflow import EvidenceCollection


class PolicyError(ValueError):
    pass


PARTIES = {
    "canceled_order_paid": {"platform", "seller"},
    "unavailable_order_paid": {"platform", "seller"},
    "late_delivery_seller": {"seller"},
    "late_delivery_logistics": {"logistics_provider"},
    "payment_mismatch": {"platform", "payment_provider"},
    "duplicate_charge": {"platform", "payment_provider"},
    "refund_pending": {"platform", "payment_provider"},
    "refund_failed": {"platform", "payment_provider"},
    "valid_split_payment": {"unknown", "customer"},
    "unsupported_claim": {"unknown", "customer"},
    "insufficient_evidence": {"unknown"},
}
MODES = {"none", "unrefunded", "overpayment", "duplicate", "failed", "freight"}
CENT = Decimal("0.01")


def amount(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PolicyError("MONEY_INVALID")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result.quantize(CENT) != result:
            raise PolicyError("MONEY_INVALID")
        return result.quantize(CENT)
    except InvalidOperation:
        raise PolicyError("MONEY_INVALID") from None


def numeric(value: Decimal) -> float:
    result = float(value)
    if amount(result) != value:
        raise PolicyError("MONEY_NOT_REPRESENTABLE")
    return result


def date(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyError("TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PolicyError("TIMESTAMP_INVALID") from None
    if parsed.tzinfo is None:
        raise PolicyError("TIMESTAMP_TIMEZONE_REQUIRED")
    return parsed


def valid_id(value: Any, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length or not value.strip():
        raise PolicyError("ENTITY_ID_INVALID")
    return value


@dataclass(frozen=True)
class CaseRequest:
    case_id: str
    orders: tuple[str, ...]
    policy_version: str
    claims: tuple[dict[str, str], ...]
    as_of: datetime | None

    @classmethod
    def parse(cls, case: dict[str, Any]) -> CaseRequest:
        from .cases import CASE_ID_PATTERN

        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise PolicyError("CASE_ID_INVALID")
        customer_request = case.get("customer_request")
        if customer_request is not None and not isinstance(customer_request, dict):
            raise PolicyError("CASE_REQUEST_INVALID")
        nested_order = (
            customer_request.get("claimed_order_id") if customer_request is not None else None
        )
        raw_orders = case.get(
            "order_ids",
            [case["order_id"]]
            if "order_id" in case
            else [nested_order]
            if nested_order is not None
            else None,
        )
        if not isinstance(raw_orders, list) or not 1 <= len(raw_orders) <= 20:
            raise PolicyError("CASE_ORDER_SCOPE_UNSUPPORTED")
        orders = tuple(valid_id(value) for value in raw_orders)
        if len(set(orders)) != len(orders):
            raise PolicyError("CASE_ORDER_DUPLICATE")
        if "order_id" in case and valid_id(case["order_id"]) not in orders:
            raise PolicyError("CASE_ORDER_SCOPE_CONFLICT")
        if nested_order is not None and valid_id(nested_order) not in orders:
            raise PolicyError("CASE_ORDER_SCOPE_CONFLICT")
        version = valid_id(case.get("policy_version"))
        raw_claims = (
            customer_request.get("claims", [])
            if customer_request is not None
            else case.get("claims", [])
        )
        if not isinstance(raw_claims, list) or len(raw_claims) > 5:
            raise PolicyError("CASE_CLAIMS_UNSUPPORTED")
        claims = []
        for claim in raw_claims:
            if not isinstance(claim, dict):
                raise PolicyError("CASE_CLAIMS_UNSUPPORTED")
            linked_order = claim.get("order_id", orders[0] if len(orders) == 1 else None)
            if linked_order not in orders:
                raise PolicyError("CLAIM_ORDER_SCOPE_INVALID")
            claim_id = valid_id(claim.get("claim_id"), max_length=64)
            issue = claim.get("issue", claim.get("topic", ""))
            claims.append(
                {
                    "claim_id": claim_id,
                    "order_id": linked_order,
                    "issue": issue if isinstance(issue, str) else "",
                }
            )
        if len({claim["claim_id"] for claim in claims}) != len(claims):
            raise PolicyError("CLAIM_ID_DUPLICATE")
        return cls(
            case_id,
            orders,
            version,
            tuple(claims),
            date(case.get("opened_at", case.get("as_of"))),
        )


@dataclass(frozen=True)
class Rule:
    status: str
    cause: str
    party: str
    mode: str
    reason: str | None
    actions: tuple[str, ...]


@dataclass(frozen=True)
class ArbitrationPolicy:
    version: str
    priority: tuple[str, ...]
    rules: dict[str, Rule]
    tolerance: Decimal
    sources: dict[str, tuple[str, ...]]

    @classmethod
    def parse(cls, data: Any, version: str) -> ArbitrationPolicy:
        # scoring-policy-v2.json defines grading, not refund law. Only the
        # explicitly supported MCP policy shape may authorize a decision.
        import re

        if (
            not isinstance(data, dict)
            or not {
                "policy_version",
                "currency",
                "rules",
                "issue_priority",
                "payment_tolerance_brl",
            }
            <= data.keys()
        ):
            raise PolicyError("POLICY_FORMAT_UNSUPPORTED")
        if data["policy_version"] != version or data["currency"] != "BRL":
            raise PolicyError("POLICY_VERSION_OR_CURRENCY_MISMATCH")
        raw_rules = data["rules"]
        priority = data["issue_priority"]
        if (
            not isinstance(raw_rules, dict)
            or set(raw_rules) != set(PARTIES)
            or not isinstance(priority, list)
            or len(priority) != len(PARTIES)
            or set(priority) != set(PARTIES)
            or set(priority[-3:])
            != {"valid_split_payment", "unsupported_claim", "insufficient_evidence"}
            or priority[-1] != "insufficient_evidence"
        ):
            raise PolicyError("POLICY_RULES_INCOMPLETE")
        rules = {}
        for issue, raw in raw_rules.items():
            if (
                not isinstance(raw, dict)
                or not {"case_status", "cause_code", "responsible_party", "refund_mode", "actions"}
                <= raw.keys()
            ):
                raise PolicyError("POLICY_RULE_INVALID")
            mode = raw["refund_mode"]
            party = raw["responsible_party"]
            status = raw["case_status"]
            actions = raw["actions"]
            reason = raw.get("refund_reason_code")
            if (
                mode not in MODES
                or party not in PARTIES[issue]
                or status not in {"action_required", "no_action", "needs_investigation"}
                or not isinstance(raw["cause_code"], str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", raw["cause_code"])
                or not isinstance(actions, list)
                or len(actions) > 8
                or any(not isinstance(a, str) or not 1 <= len(a) <= 80 for a in actions)
                or len(actions) != len(set(actions))
                or mode != "none"
                and (not isinstance(reason, str) or not 1 <= len(reason) <= 80)
                or status == "no_action"
                and mode != "none"
            ):
                raise PolicyError("POLICY_RULE_INVALID")
            if issue in {"valid_split_payment", "unsupported_claim", "insufficient_evidence"}:
                expected = (
                    "needs_investigation" if issue == "insufficient_evidence" else "no_action"
                )
                if mode != "none" or status != expected:
                    raise PolicyError("POLICY_RULE_INVALID")
            rules[issue] = Rule(status, raw["cause_code"], party, mode, reason, tuple(actions))
        sources = data.get("source_priority", {})
        if not isinstance(sources, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value)
            for key, value in sources.items()
        ):
            raise PolicyError("POLICY_SOURCE_PRIORITY_INVALID")
        return cls(
            version,
            tuple(priority),
            rules,
            amount(data["payment_tolerance_brl"]),
            {key: tuple(value) for key, value in sources.items()},
        )


@dataclass(frozen=True)
class Source:
    tool: str
    ref: str
    data: Any
    warnings: tuple[str, ...]


def sources_from(collection: EvidenceCollection, request: CaseRequest) -> list[Source]:
    if collection.case_id != request.case_id:
        raise PolicyError("EVIDENCE_CASE_MISMATCH")
    sources = []
    seen = {}
    for result in collection.results:
        obs = result.observation
        if result.status != "completed" or obs is None:
            continue
        envelope = collection.evidence.get(obs.evidence_ref)
        rule = TOOL_ACCESS.get(obs.tool_name)
        if (
            envelope is None
            or rule is None
            or result.case_id != request.case_id
            or result.actor != obs.actor
            or result.task_id != obs.task_id
            or obs.case_id != request.case_id
            or obs.actor != rule[0]
            or obs.domain != rule[1]
            or envelope["domain"] != obs.domain
            or obs.result_hash != envelope["result_hash"]
            or obs.fields.get("") != envelope["data"]
        ):
            raise PolicyError("EVIDENCE_OBSERVATION_MISMATCH")
        data = envelope["data"]
        if not isinstance(data, (dict, list)):
            raise PolicyError("EVIDENCE_DATA_UNSUPPORTED")
        if rule[2] == "order_id":
            rows = data if isinstance(data, list) else [data]
            if any(
                not isinstance(row, dict) or row.get("order_id") not in request.orders
                for row in rows
            ):
                raise PolicyError("EVIDENCE_ORDER_SCOPE_MISMATCH")
        key = (obs.tool_name, data.get("order_id") if isinstance(data, dict) else None)
        if key in seen and seen[key] != data:
            raise PolicyError("EVIDENCE_SOURCE_CONFLICT")
        seen[key] = data
        sources.append(
            Source(obs.tool_name, obs.evidence_ref, data, tuple(envelope.get("warnings", [])))
        )
    return sources


def records(source: Source, key: str, id_key: str) -> list[dict[str, Any]] | None:
    raw = source.data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise PolicyError("EVIDENCE_ROWS_INVALID")
    seen: dict[str, dict] = {}
    for row in raw:
        identity = valid_id(row.get(id_key))
        if identity in seen and seen[identity] != row:
            raise PolicyError("EVIDENCE_ROWS_CONFLICT")
        seen[identity] = row
    return list(seen.values())


@dataclass
class OrderFacts:
    order_id: str
    values: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, set[str]] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    conflict_refs: set[str] = field(default_factory=set)
    items: list[dict] | None = None
    payments: list[dict] | None = None
    refunds: list[dict] | None = None
    shipments: list[dict] | None = None
    sources: list[Source] = field(default_factory=list)

    def choose(
        self, name: str, candidates: list[tuple[Any, Source]], policy: ArbitrationPolicy
    ) -> Source | None:
        if not candidates:
            self.values[name] = None
            return None
        selected = candidates[0]
        if any(value != selected[0] for value, _ in candidates[1:]):
            tools = list(dict.fromkeys(source.tool for _, source in candidates))
            preferred = next((tool for tool in policy.sources.get(name, ()) if tool in tools), None)
            matching = [(value, source) for value, source in candidates if source.tool == preferred]
            resolved = bool(matching) and all(value == matching[0][0] for value, _ in matching)
            selected = matching[0] if resolved else (None, candidates[0][1])
            self.conflicts.append(
                {
                    "field": f"{self.order_id}.{name}"[:100],
                    "sources": tools,
                    "selected_source": preferred if resolved else None,
                    "resolution_code": "POLICY_SOURCE_PRIORITY" if resolved else "UNRESOLVED",
                }
            )
            self.conflict_refs.update(source.ref for _, source in candidates)
        self.values[name] = selected[0]
        self.refs[name] = {source.ref for value, source in candidates if value == selected[0]}
        return selected[1] if selected[0] is not None else None


def build_facts(order_id: str, sources: list[Source], policy: ArbitrationPolicy) -> OrderFacts:
    facts = OrderFacts(order_id, sources=[s for s in sources if s.data.get("order_id") == order_id])
    statuses, totals, ledgers = [], [], []
    for source in facts.sources:
        data = source.data
        if source.tool == "get_order":
            if "order_status" in data:
                if not isinstance(data["order_status"], str):
                    raise PolicyError("ORDER_STATUS_INVALID")
                statuses.append((data["order_status"], source))
            if "total_brl" in data:
                totals.append((amount(data["total_brl"]), source))
        elif source.tool == "get_order_items":
            facts.items = records(source, "items", "item_id")
            if facts.items is not None:
                total = freight = Decimal(0)
                for item in facts.items:
                    valid_id(item.get("seller_id"))
                    freight += amount(item.get("freight_value"))
                    total += amount(item.get("price")) + amount(item.get("freight_value"))
                totals.append((total, source))
                facts.values["freight"] = freight
                facts.refs["freight"] = {source.ref}
        elif source.tool in {"get_order_payments", "get_payment_timeline"}:
            payments = records(source, "payments", "payment_reference")
            if payments is not None:
                for payment in payments:
                    if payment.get("status") not in {"captured", "pending", "failed", "canceled"}:
                        raise PolicyError("PAYMENT_STATUS_UNSUPPORTED")
                    amount(payment.get("amount_brl"))
                ledgers.append((sorted(payments, key=lambda row: row["payment_reference"]), source))
        elif source.tool == "get_refund_timeline":
            facts.refunds = records(source, "refunds", "refund_id")
            if facts.refunds is not None:
                for refund in facts.refunds:
                    if refund.get("status") not in {"completed", "pending", "failed"}:
                        raise PolicyError("REFUND_STATUS_UNSUPPORTED")
                    valid_id(refund.get("payment_reference"))
                    amount(refund.get("amount_brl"))
                for status in ("completed", "pending", "failed"):
                    facts.values[f"refund_{status}"] = sum(
                        (
                            amount(row["amount_brl"])
                            for row in facts.refunds
                            if row["status"] == status
                        ),
                        Decimal(0),
                    )
                    facts.refs[f"refund_{status}"] = {source.ref}
        elif source.tool == "get_shipment_summary":
            facts.shipments = records(source, "shipments", "shipment_id")
            facts.refs["shipments"] = {source.ref}
            if "order_status" in data:
                statuses.append((data["order_status"], source))
    facts.choose("status", statuses, policy)
    facts.choose("total", totals, policy)
    selected = facts.choose("payments", ledgers, policy)
    if selected is not None:
        facts.payments = facts.values["payments"]
        facts.values["paid"] = sum(
            (amount(row["amount_brl"]) for row in facts.payments if row["status"] == "captured"),
            Decimal(0),
        )
        facts.refs["paid"] = facts.refs["payments"]
        captured = {
            row["payment_reference"]: row for row in facts.payments if row["status"] == "captured"
        }
        facts.values["payment_count"] = len(captured)
        facts.refs["payment_count"] = facts.refs["payments"]
        duplicate = Decimal(0)
        for row in captured.values():
            original = row.get("duplicate_of")
            if original is not None:
                if (
                    original not in captured
                    or original == row["payment_reference"]
                    or captured[original].get("duplicate_of") is not None
                    or amount(captured[original]["amount_brl"]) != amount(row["amount_brl"])
                ):
                    raise PolicyError("DUPLICATE_LINK_INVALID")
                duplicate += amount(row["amount_brl"])
        facts.values["duplicate"] = duplicate
        facts.refs["duplicate"] = facts.refs["payments"]
        if facts.refunds is not None:
            for refund in facts.refunds:
                if refund["payment_reference"] not in captured:
                    raise PolicyError("REFUND_PAYMENT_LINK_INVALID")
            for reference, payment in captured.items():
                reserved = sum(
                    (
                        amount(row["amount_brl"])
                        for row in facts.refunds
                        if row["payment_reference"] == reference and row["status"] != "failed"
                    ),
                    Decimal(0),
                )
                if reserved > amount(payment["amount_brl"]):
                    raise PolicyError("REFUND_EXCEEDS_CAPTURE")
    facts.values["shipments"] = facts.shipments
    return facts


def delivery_class(facts: OrderFacts, as_of: datetime | None) -> tuple[bool | None, bool | None]:
    if not facts.shipments:
        return None, None
    seller = logistics = False
    complete = True
    for shipment in facts.shipments:
        promised = date(shipment.get("promised_at"))
        delivered = date(shipment.get("delivered_at"))
        handoff = date(shipment.get("seller_handoff_at"))
        due = date(shipment.get("seller_handoff_due_at"))
        if delivered is not None and handoff is not None and handoff > delivered:
            raise PolicyError("SHIPMENT_TIMELINE_CONFLICT")
        observed = delivered or as_of
        if promised is None or observed is None:
            complete = False
            continue
        if observed <= promised:
            continue
        if handoff is None or due is None:
            complete = False
        elif handoff > due:
            valid_id(shipment.get("seller_id"))
            seller = True
        else:
            valid_id(shipment.get("logistics_provider_id"))
            logistics = True
    return (
        True if seller else False if complete else None,
        True if logistics else False if complete else None,
    )


def predicates(
    facts: OrderFacts, request: CaseRequest, policy: ArbitrationPolicy
) -> dict[str, bool | None]:
    values = facts.values
    result: dict[str, bool | None] = {}
    for issue, status in (
        ("canceled_order_paid", "canceled"),
        ("unavailable_order_paid", "unavailable"),
    ):
        result[issue] = (
            None
            if values.get("status") is None
            else False
            if values["status"] != status
            else None
            if values.get("paid") is None
            else values["paid"] > policy.tolerance
        )
    result["late_delivery_seller"], result["late_delivery_logistics"] = delivery_class(
        facts, request.as_of
    )
    for issue, fact_name in (
        ("refund_pending", "refund_pending"),
        ("refund_failed", "refund_failed"),
        ("duplicate_charge", "duplicate"),
    ):
        result[issue] = (
            None if values.get(fact_name) is None else values[fact_name] > policy.tolerance
        )
    ready = values.get("paid") is not None and values.get("total") is not None
    result["payment_mismatch"] = (
        abs(values["paid"] - values["total"]) > policy.tolerance if ready else None
    )
    split_ready = (
        ready and values.get("payment_count") is not None and values.get("duplicate") is not None
    )
    result["valid_split_payment"] = (
        values["payment_count"] >= 2 and values["duplicate"] == 0 and not result["payment_mismatch"]
        if split_ready
        else None
    )
    claims = [claim for claim in request.claims if claim["order_id"] == facts.order_id]
    result["unsupported_claim"] = bool(claims) and all(
        claim["issue"] in result and result[claim["issue"]] is False for claim in claims
    )
    result["insufficient_evidence"] = True
    return result


def refund(facts: OrderFacts, mode: str) -> Decimal:
    if mode == "none":
        return Decimal(0)
    values = facts.values
    required = {"paid", "refund_completed", "refund_pending"}
    required.update(
        {
            "overpayment": {"total"},
            "duplicate": {"duplicate"},
            "failed": {"refund_failed"},
            "freight": {"freight"},
        }.get(mode, set())
    )
    if any(values.get(field) is None for field in required):
        raise PolicyError("REFUND_EVIDENCE_INCOMPLETE")
    available = max(
        Decimal(0), values["paid"] - values["refund_completed"] - values["refund_pending"]
    )
    if mode == "unrefunded":
        return available
    if mode == "overpayment":
        return max(Decimal(0), available - values["total"])
    if mode == "freight":
        return max(
            Decimal(0),
            min(values["paid"], values["freight"])
            - values["refund_completed"]
            - values["refund_pending"],
        )
    if mode == "failed":
        return min(available, values["refund_failed"])
    if mode == "duplicate":
        if facts.payments is None or facts.refunds is None:
            raise PolicyError("REFUND_EVIDENCE_INCOMPLETE")
        remaining = Decimal(0)
        for payment in facts.payments:
            if payment["status"] == "captured" and payment.get("duplicate_of") is not None:
                reserved = sum(
                    (
                        amount(row["amount_brl"])
                        for row in facts.refunds
                        if row["payment_reference"] == payment["payment_reference"]
                        and row["status"] in {"completed", "pending"}
                    ),
                    Decimal(0),
                )
                remaining += max(Decimal(0), amount(payment["amount_brl"]) - reserved)
        return min(available, remaining)
    raise PolicyError("REFUND_MODE_UNSUPPORTED")


@dataclass(frozen=True)
class Decision:
    facts: OrderFacts
    issue: str
    money: Decimal
    confidence: float
    refs: frozenset[str]
    truth: dict[str, bool | None]


def decide(facts: OrderFacts, request: CaseRequest, policy: ArbitrationPolicy) -> Decision:
    truth = predicates(facts, request, policy)
    issue, uncertain = "insufficient_evidence", False
    for option in policy.priority:
        if truth[option] is None:
            uncertain = True
        elif truth[option] is True:
            issue = option if not uncertain else "insufficient_evidence"
            break
    try:
        proposed = refund(facts, policy.rules[issue].mode)
    except PolicyError as exc:
        if str(exc) != "REFUND_EVIDENCE_INCOMPLETE":
            raise
        issue, proposed = "insufficient_evidence", Decimal(0)
    if any(conflict["selected_source"] is None for conflict in facts.conflicts):
        issue, proposed = "insufficient_evidence", Decimal(0)
    required = {"status", "paid", "total", "shipments", "refund_pending", "refund_failed"}
    coverage = sum(facts.values.get(name) is not None for name in required) / len(required)
    confidence = 0.45 + 0.5 * coverage - 0.1 * len(facts.conflicts)
    confidence -= 0.15 * sum(c["selected_source"] is None for c in facts.conflicts)
    confidence -= min(0.2, sum(len(source.warnings) for source in facts.sources) * 0.05)
    if issue == "insufficient_evidence":
        confidence = min(confidence, 0.45)
    confidence = round(max(0.05, min(0.95, confidence)), 3)
    refs = set().union(*(facts.refs.values())) | facts.conflict_refs
    refs.update(
        source.ref
        for source in facts.sources
        if source.tool
        in {"get_order", "get_order_items", "get_order_payments", "get_shipment_summary"}
    )
    return Decision(facts, issue, proposed, confidence, frozenset(refs), truth)


def responsible(
    decision: Decision, policy: ArbitrationPolicy, as_of: datetime | None
) -> list[dict]:
    party = policy.rules[decision.issue].party
    if party == "unknown":
        return [{"party_type": party, "party_id": None}]
    if party == "seller":
        if decision.issue == "late_delivery_seller":
            rows = [
                row
                for row in decision.facts.shipments or []
                if date(row.get("seller_handoff_at"))
                and date(row.get("seller_handoff_due_at"))
                and date(row["seller_handoff_at"]) > date(row["seller_handoff_due_at"])
            ]
        else:
            rows = decision.facts.items or []
        ids = sorted({valid_id(row.get("seller_id")) for row in rows})
        if not ids:
            raise PolicyError("RESPONSIBLE_SELLER_MISSING")
        return [{"party_type": party, "party_id": value} for value in ids]
    if party == "logistics_provider":
        late_rows = [
            row
            for row in decision.facts.shipments or []
            if date(row.get("promised_at"))
            and (date(row.get("delivered_at")) or as_of)
            and (date(row.get("delivered_at")) or as_of) > date(row["promised_at"])
            and date(row.get("seller_handoff_at"))
            and date(row.get("seller_handoff_due_at"))
            and date(row["seller_handoff_at"]) <= date(row["seller_handoff_due_at"])
        ]
        ids = sorted({valid_id(row.get("logistics_provider_id")) for row in late_rows})
        if not ids:
            raise PolicyError("RESPONSIBLE_LOGISTICS_MISSING")
        return [{"party_type": party, "party_id": value} for value in ids]
    return [{"party_type": party, "party_id": None}]


def affected(decisions: tuple[Decision, ...]) -> dict[str, list[str]]:
    groups: dict[str, set[str]] = {
        key: set()
        for key in ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")
    }
    for decision in decisions:
        facts = decision.facts
        if facts.sources:
            groups["order_ids"].add(facts.order_id)
        for item in facts.items or []:
            groups["item_ids"].add(valid_id(item.get("item_id")))
            groups["seller_ids"].add(valid_id(item.get("seller_id")))
        for payment in facts.payments or []:
            groups["payment_references"].add(valid_id(payment.get("payment_reference")))
        for shipment in facts.shipments or []:
            groups["shipment_ids"].add(valid_id(shipment.get("shipment_id")))
            if shipment.get("seller_id"):
                groups["seller_ids"].add(valid_id(shipment["seller_id"]))
    return {key: sorted(values) for key, values in groups.items()}


class PolicyEngine:
    def evaluate(self, case: dict[str, Any], collection: EvidenceCollection) -> dict[str, Any]:
        request = CaseRequest.parse(case)
        sources = sources_from(collection, request)
        policies = [source for source in sources if source.tool == "get_policy"]
        if not policies or any(source.data != policies[0].data for source in policies[1:]):
            raise PolicyError("POLICY_EVIDENCE_MISSING_OR_CONFLICTING")
        if "customer_request" in case:
            from .official_adapter import evaluate_official

            return evaluate_official(request, sources, policies[0])
        policy = ArbitrationPolicy.parse(policies[0].data, request.policy_version)
        decisions = tuple(
            decide(build_facts(order, sources, policy), request, policy) for order in request.orders
        )
        if any(not decision.facts.sources for decision in decisions):
            raise PolicyError("ORDER_EVIDENCE_MISSING")
        unresolved = any(decision.issue == "insufficient_evidence" for decision in decisions)
        primary = (
            "insufficient_evidence"
            if unresolved
            else min((decision.issue for decision in decisions), key=policy.priority.index)
        )
        if unresolved:
            decisions = tuple(
                Decision(
                    decision.facts,
                    "insufficient_evidence",
                    Decimal(0),
                    min(decision.confidence, 0.45),
                    decision.refs,
                    decision.truth,
                )
                for decision in decisions
            )
        active = tuple(d for d in decisions if policy.rules[d.issue].status != "no_action")
        represented = active or decisions
        refs = {policies[0].ref}.union(*(decision.refs for decision in decisions))
        causes = list(
            dict.fromkeys(
                policy.rules[d.issue].cause
                for d in sorted(represented, key=lambda item: policy.priority.index(item.issue))
            )
        )
        parties: list[dict] = []
        for decision in represented:
            for item in responsible(decision, policy, request.as_of):
                if item not in parties:
                    parties.append(item)
        lines = [
            {
                "reason_code": policy.rules[d.issue].reason,
                "amount_brl": numeric(d.money),
                "entity_id": d.facts.order_id,
            }
            for d in decisions
            if d.money > 0
        ]
        output: dict[str, Any] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "case_id": request.case_id,
            "assessment": {
                "primary_issue": primary,
                "case_status": policy.rules[primary].status,
                "confidence": min(d.confidence for d in decisions),
            },
            "affected_entities": affected(decisions),
            "root_cause_analysis": {
                "ranked_causes": [
                    {"cause_code": cause, "rank": index} for index, cause in enumerate(causes, 1)
                ],
                "responsible_parties": parties,
            },
            "evidence_refs": sorted(refs),
            "data_conflicts": [conflict for d in decisions for conflict in d.facts.conflicts],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": numeric(sum((d.money for d in decisions), Decimal(0))),
                "refund_lines": lines,
            },
            "resolution_actions": sorted(
                {action for d in represented for action in policy.rules[d.issue].actions}
            ),
        }
        if request.claims:
            by_order = {decision.facts.order_id: decision for decision in decisions}
            output["claim_assessments"] = []
            for claim in request.claims:
                decision = by_order[claim["order_id"]]
                truth = decision.truth.get(claim["issue"])
                unknown = truth is None or decision.issue == "insufficient_evidence"
                output["claim_assessments"].append(
                    {
                        "claim_id": claim["claim_id"],
                        "verdict": "insufficient_evidence"
                        if unknown
                        else "supported"
                        if truth
                        else "unsupported",
                        "confidence": min(decision.confidence, 0.45)
                        if unknown
                        else decision.confidence,
                        "evidence_refs": sorted(decision.refs | {policies[0].ref}),
                    }
                )
        if policies[0].warnings:
            output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.75)
            for claim in output.get("claim_assessments", []):
                claim["confidence"] = min(claim["confidence"], 0.75)
        return output
