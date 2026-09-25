from __future__ import annotations

import asyncio
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .evidence import CaseEvidenceCollector, CaseScope, CollectionLimits
from .mcp_gateway import EvidenceGateway, transport_error
from .policy_engine import CaseRequest, PolicyEngine, PolicyError
from .specialists import SPECIALIST_TYPES, SpecialistResult, SpecialistTask
from .trace import TraceWriter
from .verifier import VerificationError, VerifierAgent


@dataclass(frozen=True)
class EvidenceCollection:
    case_id: str
    results: tuple[SpecialistResult, ...]
    evidence: dict[str, dict[str, Any]]

    @property
    def complete(self) -> bool:
        return all(result.status == "completed" for result in self.results)


async def collect_case_evidence(
    scope: CaseScope,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    tasks: Sequence[SpecialistTask],
    *,
    limits: CollectionLimits | None = None,
) -> EvidenceCollection:
    """Phase 3 entry point: scoped collection, not a final competition answer.

    The caller supplies lookup IDs and selects tools/fields needed by the case.
    It owns case_received/finalization; no claim or evidence ref is fabricated here.
    """
    tasks = deepcopy(tuple(tasks))
    if not tasks or len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("provide at least one task with unique task IDs")
    collector = CaseEvidenceCollector(scope, gateway, trace, limits)
    results: dict[str, SpecialistResult] = {}
    try:
        async with asyncio.timeout_at(collector.deadline):
            catalog = await gateway.discover_tools()
            # Fail preflight before making any evidence calls if routing is invalid.
            for task in tasks:
                if not isinstance(task.task_id, str) or not task.task_id:
                    raise ValueError("task_id must be non-empty")
                if not task.pointers or any(not isinstance(p, str) for p in task.pointers):
                    raise ValueError("pointers must be a non-empty sequence of strings")
                if len(set(task.pointers)) != len(task.pointers):
                    raise ValueError("duplicate evidence pointers")
                collector.validate_request(task.actor, task.tool_name, task.arguments)
                if task.tool_name not in catalog:
                    raise ValueError(f"tool not discovered: {task.tool_name}")
                catalog[task.tool_name].validate_arguments(
                    {"case_id": scope.case_id, **task.arguments}
                )
            agents = {
                actor: agent_type(collector.for_actor(actor), scope.case_id, trace)
                for actor, agent_type in SPECIALIST_TYPES.items()
            }

            async def run_task(task: SpecialistTask) -> None:
                trace.emit(
                    case_id=scope.case_id,
                    event_type="task_assigned",
                    actor="coordinator",
                    target=task.actor,
                    tool_name=task.tool_name,
                    attributes={"task_id": task.task_id},
                )
                results[task.task_id] = await agents[task.actor].run(task)

            # Policy reads only after the specialist barrier. No policy decision
            # or verification event is emitted until those stages are implemented.
            for policy_stage in (False, True):
                async with asyncio.TaskGroup() as group:
                    for task in tasks:
                        if (task.actor == "policy-agent") == policy_stage:
                            group.create_task(run_task(task))
    except TimeoutError:
        trace.emit(
            case_id=scope.case_id,
            event_type="handoff",
            actor="coordinator",
            target="coordinator",
            decision_code="CASE_TIMEOUT",
            attributes={"status": "failed"},
        )
        raise RuntimeError("CASE_TIMEOUT") from None
    except ExceptionGroup as exc:
        error = transport_error(exc)
        if error is None:
            raise
        raise error from None
    finally:
        collector.close()
    return EvidenceCollection(
        scope.case_id, tuple(results[task.task_id] for task in tasks), collector.snapshot()
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run specialist reads, policy decision, then independent verification.

    Unknown case/policy formats fail before a final output is returned. The CLI
    owns writing outputs and emits case_finalized only after this function passes.
    """
    request = CaseRequest.parse(case)
    if not trace.events(request.case_id):
        trace.emit(case_id=request.case_id, event_type="case_received", actor="coordinator")
    deadline = asyncio.get_running_loop().time() + CollectionLimits().case_timeout
    try:
        async with asyncio.timeout_at(deadline):
            tasks = []
            baseline = [
                ("order-agent", "get_order"),
                ("order-agent", "get_order_items"),
                ("payment-agent", "get_order_payments"),
                ("payment-agent", "get_payment_timeline"),
                ("shipment-agent", "get_shipment_summary"),
                ("payment-agent", "get_refund_timeline"),
            ]
            for order_id in request.orders:
                tools = list(baseline)
                for actor, tool in tools:
                    tasks.append(
                        SpecialistTask(
                            f"task_{len(tasks) + 1}", actor, tool, {"order_id": order_id}
                        )
                    )
            tasks.append(
                SpecialistTask(
                    "policy_read",
                    "policy-agent",
                    "get_policy",
                    {"policy_version": request.policy_version},
                )
            )
            collection = await collect_case_evidence(
                CaseScope(request.case_id, request.orders, (request.policy_version,)),
                gateway,
                trace,
                tasks,
                limits=CollectionLimits(
                    case_timeout=max(0.001, deadline - asyncio.get_running_loop().time())
                ),
            )
            trace.emit(
                case_id=request.case_id,
                event_type="task_assigned",
                actor="coordinator",
                target="policy-agent",
                decision_code="APPLY_POLICY",
            )
            trace.emit(
                case_id=request.case_id,
                event_type="handoff",
                actor="coordinator",
                target="policy-agent",
                decision_code="SPECIALIST_SNAPSHOT_READY",
            )
            try:
                output = PolicyEngine().evaluate(case, collection)
            except PolicyError:
                trace.emit(
                    case_id=request.case_id,
                    event_type="handoff",
                    actor="policy-agent",
                    target="coordinator",
                    decision_code="POLICY_EVALUATION_FAILED",
                )
                raise
            by_ref = {
                result.observation.evidence_ref: result.observation
                for result in collection.results
                if result.observation is not None
            }
            for ref in output["evidence_refs"]:
                observation = by_ref[ref]
                trace.emit(
                    case_id=request.case_id,
                    event_type="tool_result_consumed",
                    actor="policy-agent",
                    tool_name=observation.tool_name,
                    evidence_refs=[ref],
                )
            trace.emit(
                case_id=request.case_id,
                event_type="policy_decided",
                actor="policy-agent",
                decision_code=output["assessment"]["primary_issue"].upper(),
                evidence_refs=output["evidence_refs"][:20],
            )
            trace.emit(
                case_id=request.case_id,
                event_type="task_assigned",
                actor="coordinator",
                target="verifier",
                decision_code="VERIFY_CANDIDATE",
            )
            trace.emit(
                case_id=request.case_id,
                event_type="handoff",
                actor="policy-agent",
                target="verifier",
                decision_code="CANDIDATE_READY",
            )
            await asyncio.sleep(0)
            try:
                VerifierAgent(trace.contracts).verify(
                    case, collection, output, trace.events(request.case_id)
                )
            except (VerificationError, ValueError):
                trace.emit(
                    case_id=request.case_id,
                    event_type="verification_completed",
                    actor="verifier",
                    target="coordinator",
                    decision_code="VERIFY_FAILED",
                )
                raise
            await asyncio.sleep(0)
            trace.emit(
                case_id=request.case_id,
                event_type="verification_completed",
                actor="verifier",
                target="coordinator",
                decision_code="VERIFY_PASS",
            )
            return output
    except TimeoutError:
        trace.emit(
            case_id=request.case_id,
            event_type="handoff",
            actor="coordinator",
            target="coordinator",
            decision_code="CASE_TIMEOUT",
        )
        raise RuntimeError("CASE_TIMEOUT") from None
