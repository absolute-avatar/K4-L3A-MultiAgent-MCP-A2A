from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from .evidence import AgentEvidenceClient, Observation
from .mcp_gateway import GatewayError
from .trace import TraceWriter


@dataclass(frozen=True)
class SpecialistTask:
    task_id: str
    actor: str
    tool_name: str
    arguments: dict[str, Any]
    pointers: tuple[str, ...] = ("",)


@dataclass(frozen=True)
class SpecialistResult:
    case_id: str
    task_id: str
    actor: str
    status: str
    observation: Observation | None = None
    error_code: str | None = None


class SpecialistAgent:
    """Read authoritative fields; business verdicts belong to the policy stage.

    Each returned field is associated with the unchanged ref of its source.
    Missing selected fields fail the task instead of yielding invented defaults.
    """

    actor: ClassVar[str]

    def __init__(self, client: AgentEvidenceClient, case_id: str, trace: TraceWriter) -> None:
        if client.actor != self.actor:
            raise ValueError("specialist requires its own actor-bound evidence client")
        if case_id != client.case_id:
            raise ValueError("specialist case_id must match its evidence client")
        self._client = client
        self._case_id = case_id
        self._trace = trace

    async def run(self, task: SpecialistTask) -> SpecialistResult:
        if task.actor != self.actor:
            raise ValueError("task assigned to the wrong specialist")
        try:
            receipt = await self._client.fetch(
                task.tool_name, task_id=task.task_id, **task.arguments
            )
            observation = self._client.consume(receipt, pointers=task.pointers)
        except GatewayError as exc:
            self._trace.emit(
                case_id=self._case_id,
                event_type="handoff",
                actor=self.actor,
                target="coordinator",
                tool_name=task.tool_name,
                decision_code=exc.code,
                attributes={"task_id": task.task_id, "status": "failed"},
            )
            if exc.code == "MCP_AUTH_FAILED":
                raise  # Authorization failure stops the run, including sibling tasks.
            return SpecialistResult(
                self._case_id, task.task_id, self.actor, "failed", error_code=exc.code
            )
        self._trace.emit(
            case_id=self._case_id,
            event_type="handoff",
            actor=self.actor,
            target="coordinator",
            tool_name=task.tool_name,
            evidence_refs=[observation.evidence_ref],
        )
        return SpecialistResult(
            self._case_id, task.task_id, self.actor, "completed", observation=observation
        )


class OrderItemAgent(SpecialistAgent):
    """Order/item/seller facts; customer/product context only on explicit assignment."""

    actor = "order-agent"


class PaymentAgent(SpecialistAgent):
    """Payment rows, lifecycle and refund facts without inferring duplicate charges."""

    actor = "payment-agent"


class ShipmentAgent(SpecialistAgent):
    """Shipment timestamps/events without inferring responsibility from missing fields."""

    actor = "shipment-agent"


class PolicyAgent(SpecialistAgent):
    """Load the explicitly requested policy version; decision logic is a later stage."""

    actor = "policy-agent"


SPECIALIST_TYPES = {
    agent.actor: agent for agent in (OrderItemAgent, PaymentAgent, ShipmentAgent, PolicyAgent)
}
