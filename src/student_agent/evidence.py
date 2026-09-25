from __future__ import annotations

import asyncio
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .cases import CASE_ID_PATTERN
from .mcp_gateway import EvidenceGateway, GatewayError, transport_error
from .trace import TraceWriter

# Explicit mappings reviewed against live discovery, not inferred from name prefixes.
# New tools remain unavailable until their ownership and arguments are reviewed.
TOOL_ACCESS = MappingProxyType(
    {
        "get_order": ("order-agent", "order", "order_id"),
        "get_order_items": ("order-agent", "item", "order_id"),
        "get_sellers": ("order-agent", "seller", "order_id"),
        "get_product_context": ("order-agent", "product", "order_id"),
        "get_customer_history": ("order-agent", "customer", "customer_unique_id"),
        "get_order_payments": ("payment-agent", "payment", "order_id"),
        "get_payment_timeline": ("payment-agent", "payment", "order_id"),
        "get_refund_timeline": ("payment-agent", "refund", "order_id"),
        "get_shipment_summary": ("shipment-agent", "shipment", "order_id"),
        "get_policy": ("policy-agent", "policy", "policy_version"),
    }
)


@dataclass(frozen=True)
class CaseScope:
    """Lookup scope explicitly supplied by the coordinator, never extracted from prose."""

    case_id: str
    order_ids: tuple[str, ...] = ()
    policy_versions: tuple[str, ...] = ()
    customer_unique_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not CASE_ID_PATTERN.fullmatch(self.case_id):
            raise ValueError("invalid case_id")
        for field in ("order_ids", "policy_versions", "customer_unique_ids"):
            if isinstance(getattr(self, field), str):
                raise ValueError(f"{field} must be a sequence of IDs, not a string")
            values = tuple(getattr(self, field))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"invalid {field}")
            object.__setattr__(self, field, tuple(dict.fromkeys(values)))

    def allows(self, argument: str, value: str) -> bool:
        allowed = {
            "order_id": self.order_ids,
            "policy_version": self.policy_versions,
            "customer_unique_id": self.customer_unique_ids,
        }
        return value in allowed.get(argument, ())


@dataclass(frozen=True)
class CollectionLimits:
    attempt_timeout: float = 30.0
    case_timeout: float = 180.0
    max_attempts: int = 3
    concurrency: int = 3
    backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 1 <= value <= 3
            for value in (self.max_attempts, self.concurrency)
        ):
            raise ValueError("max_attempts and concurrency must be between 1 and 3")
        if (
            any(
                not math.isfinite(value)
                for value in (self.attempt_timeout, self.case_timeout, self.backoff_seconds)
            )
            or self.attempt_timeout <= 0
            or self.case_timeout <= 0
            or self.backoff_seconds < 0
        ):
            raise ValueError("timeouts must be positive and backoff non-negative")


@dataclass(frozen=True, eq=False)
class EvidenceReceipt:
    """Opaque, collector-owned receipt. A ref string alone cannot be consumed."""

    evidence_ref: str
    tool_name: str
    actor: str
    task_id: str


@dataclass(frozen=True)
class Observation:
    case_id: str
    task_id: str
    actor: str
    tool_name: str
    domain: str
    evidence_ref: str
    result_hash: str
    fields: dict[str, Any]
    warnings: tuple[str, ...]


def _select(data: Any, pointer: str) -> Any:
    """Read a JSON pointer without silently substituting missing data."""
    if pointer == "":
        return deepcopy(data)
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise GatewayError("EVIDENCE_FIELD_MISSING")
    node = data
    for encoded in pointer[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise GatewayError("EVIDENCE_FIELD_MISSING")
        key = encoded.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                node = node[int(key)]
            elif isinstance(node, dict):
                node = node[key]
            else:
                raise GatewayError("EVIDENCE_FIELD_MISSING")
        except (KeyError, IndexError):
            raise GatewayError("EVIDENCE_FIELD_MISSING") from None
    return deepcopy(node)


def _check_response_scope(data: Any, expected: dict[str, str]) -> None:
    # Data schemas are open. Check explicit identifiers when present; do not
    # pretend this replaces server-side scope/audit when identifiers are absent.
    if isinstance(data, dict):
        for key, value in data.items():
            if key in expected and value is not None and value != expected[key]:
                raise GatewayError("EVIDENCE_SCOPE_MISMATCH")
            _check_response_scope(value, expected)
    elif isinstance(data, list):
        for value in data:
            _check_response_scope(value, expected)


class CaseEvidenceCollector:
    def __init__(
        self,
        scope: CaseScope,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        limits: CollectionLimits | None = None,
    ) -> None:
        self._scope = scope
        self._gateway = gateway
        self._trace = trace
        self._limits = limits or CollectionLimits()
        self._deadline = asyncio.get_running_loop().time() + self._limits.case_timeout
        self._semaphore = asyncio.Semaphore(self._limits.concurrency)
        self._evidence: dict[str, dict[str, Any]] = {}
        self._receipts: set[EvidenceReceipt] = set()
        self._closed = False

    @property
    def scope(self) -> CaseScope:
        return self._scope

    @property
    def deadline(self) -> float:
        return self._deadline

    def close(self) -> None:
        self._closed = True

    def _remaining(self) -> float:
        if self._closed:
            raise GatewayError("CASE_CLOSED")
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise GatewayError("CASE_TIMEOUT")
        return remaining

    def for_actor(self, actor: str) -> AgentEvidenceClient:
        if actor not in {rule[0] for rule in TOOL_ACCESS.values()}:
            raise GatewayError("TOOL_PERMISSION_DENIED")
        return AgentEvidenceClient(self, actor)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._evidence)

    def validate_request(self, actor: str, tool_name: str, arguments: dict[str, Any]) -> str:
        rule = TOOL_ACCESS.get(tool_name)
        if rule is None or actor != rule[0]:
            raise GatewayError("TOOL_PERMISSION_DENIED")
        _, domain, argument = rule
        if set(arguments) != {argument}:
            raise GatewayError("TOOL_ARGUMENTS_INVALID")
        if not isinstance(arguments[argument], str) or not self.scope.allows(
            argument, arguments[argument]
        ):
            raise GatewayError("CASE_SCOPE_INVALID")
        return domain

    async def _fetch(
        self, actor: str, task_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> EvidenceReceipt:
        domain = self.validate_request(actor, tool_name, arguments)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be non-empty")
        for attempt in range(1, self._limits.max_attempts + 1):
            try:
                async with asyncio.timeout(self._remaining()), self._semaphore:
                    timeout = min(self._limits.attempt_timeout, self._remaining())
                    async with asyncio.timeout(timeout):
                        envelope = await self._gateway.call(
                            tool_name, case_id=self.scope.case_id, **arguments
                        )
                self._remaining()  # Discard late results, including suppressed cancellation.
                break
            except Exception as exc:
                error = transport_error(exc)
                if error is None:
                    raise
                self._remaining()
                if not error.retryable or attempt == self._limits.max_attempts:
                    raise error from None
                delay = self._limits.backoff_seconds * 2 ** (attempt - 1)
                if error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                if delay >= self._remaining():
                    raise GatewayError("CASE_TIMEOUT") from None
                self._trace.emit(
                    case_id=self.scope.case_id,
                    event_type="handoff",
                    actor=actor,
                    target="coordinator",
                    tool_name=tool_name,
                    decision_code="MCP_RETRY_REQUESTED",
                    attributes={"task_id": task_id, "attempt": attempt, "error_code": error.code},
                )
                await asyncio.sleep(delay)
                self._remaining()
                self._trace.emit(
                    case_id=self.scope.case_id,
                    event_type="task_assigned",
                    actor="coordinator",
                    target=actor,
                    tool_name=tool_name,
                    decision_code="MCP_RETRY",
                    attributes={"task_id": task_id, "attempt": attempt + 1},
                )
        if envelope["domain"] != domain:
            raise GatewayError("EVIDENCE_DOMAIN_MISMATCH")
        expected = {"case_id": self.scope.case_id, **arguments}
        _check_response_scope(envelope["data"], expected)
        ref = envelope["evidence_ref"]
        if ref in self._evidence and self._evidence[ref] != envelope:
            raise GatewayError("EVIDENCE_REF_CONFLICT")
        self._evidence[ref] = deepcopy(envelope)
        receipt = EvidenceReceipt(ref, tool_name, actor, task_id)
        self._receipts.add(receipt)
        return receipt

    def _consume(
        self, actor: str, receipt: EvidenceReceipt, pointers: tuple[str, ...]
    ) -> Observation:
        self._remaining()
        if receipt not in self._receipts or receipt.actor != actor:
            raise GatewayError("EVIDENCE_NOT_OWNED")
        if not pointers or len(set(pointers)) != len(pointers):
            raise ValueError("select at least one unique JSON pointer")
        envelope = self._evidence[receipt.evidence_ref]
        if not isinstance(envelope["data"], (dict, list)):
            raise GatewayError("EVIDENCE_DATA_UNSUPPORTED")
        if not envelope["data"]:
            raise GatewayError("EVIDENCE_DATA_EMPTY")
        fields = {pointer: _select(envelope["data"], pointer) for pointer in pointers}
        observation = Observation(
            self.scope.case_id,
            receipt.task_id,
            actor,
            receipt.tool_name,
            envelope["domain"],
            receipt.evidence_ref,
            envelope["result_hash"],
            fields,
            tuple(envelope.get("warnings", [])),
        )
        self._trace.emit(
            case_id=self.scope.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=receipt.tool_name,
            evidence_refs=[receipt.evidence_ref],
            attributes={"task_id": receipt.task_id},
        )
        return observation


class AgentEvidenceClient:
    """Actor-bound interface. No case_id override or raw gateway is exposed."""

    def __init__(self, collector: CaseEvidenceCollector, actor: str) -> None:
        self._collector = collector
        self._actor = actor

    @property
    def actor(self) -> str:
        return self._actor

    @property
    def case_id(self) -> str:
        return self._collector.scope.case_id

    async def fetch(self, tool_name: str, *, task_id: str, **arguments: Any) -> EvidenceReceipt:
        return await self._collector._fetch(self.actor, task_id, tool_name, arguments)

    def consume(
        self, receipt: EvidenceReceipt, *, pointers: tuple[str, ...] = ("",)
    ) -> Observation:
        return self._collector._consume(self.actor, receipt, pointers)
