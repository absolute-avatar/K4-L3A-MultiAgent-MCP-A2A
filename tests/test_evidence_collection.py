from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from student_agent import cli
from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.evidence import (
    TOOL_ACCESS,
    CaseEvidenceCollector,
    CaseScope,
    CollectionLimits,
)
from student_agent.mcp_gateway import EvidenceGateway, GatewayError, transport_error
from student_agent.specialists import SpecialistTask
from student_agent.trace import TraceWriter
from student_agent.workflow import collect_case_evidence


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def fixture_evidence(domain: str = "order", *, data: object = None) -> dict:
    """Synthetic responses for offline tests ONLY, never submission evidence."""
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_" + domain + "_fixture_01234567890123456789",
        "result_hash": "sha256:" + "a" * 64,
        "domain": domain,
        "data": {"order_id": "order-1", "status": "delivered"} if data is None else data,
    }


def discovered_tool(name: str) -> Tool:
    argument = TOOL_ACCESS[name][2]
    return Tool(
        name=name,
        description=f"Test fixture for {name}",
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string"}, argument: {"type": "string"}},
            "required": ["case_id", argument],
        },
    )


class FakeSession:
    def __init__(self, responses: list | None = None) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.discovery_calls = 0
        self.active = 0
        self.max_active = 0
        self.delay = 0.0
        self.cancelled = 0

    async def list_tools(self, *, params=None):
        self.discovery_calls += 1
        return ListToolsResult(tools=[discovered_tool(name) for name in TOOL_ACCESS])

    async def call_tool(self, name, *, arguments):
        self.calls.append((name, deepcopy(arguments)))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.responses is not None:
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
            data = {key: value for key, value in arguments.items() if key != "case_id"}
            return CallToolResult(
                content=[], structured_content=fixture_evidence(TOOL_ACCESS[name][1], data=data)
            )
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_discovery_keeps_schemas_caches_and_validates_before_call(contracts):
    async def scenario():
        session = FakeSession()
        gateway = EvidenceGateway(session, contracts)
        catalog = await gateway.discover_tools()
        catalog["get_order"].input_schema.clear()
        assert (await gateway.discover_tools())["get_order"].input_schema["required"]
        for name, args in [
            ("unknown", {"order_id": "order-1"}),
            ("get_order", {}),
            ("get_order", {"order_id": 4}),
            ("get_order", {"order_id": "order-1", "typo": "x"}),
        ]:
            with pytest.raises(GatewayError):
                await gateway.call(name, case_id="CASE_001", **args)
        assert session.calls == []
        assert session.discovery_calls == 1
        await gateway.call("get_order", case_id="CASE_001", order_id="order-1")
        assert session.calls == [("get_order", {"case_id": "CASE_001", "order_id": "order-1"})]

    asyncio.run(scenario())


def test_discovery_pagination_and_cursor_loop(contracts):
    class Pages(FakeSession):
        async def list_tools(self, *, params=None):
            if params is None:
                return ListToolsResult(tools=[discovered_tool("get_order")], next_cursor="next")
            assert params.cursor == "next"
            return ListToolsResult(tools=[discovered_tool("get_policy")])

    class Loop(FakeSession):
        async def list_tools(self, *, params=None):
            return ListToolsResult(tools=[], next_cursor="same")

    async def scenario():
        assert await EvidenceGateway(Pages(), contracts).list_tools() == ["get_order", "get_policy"]
        with pytest.raises(GatewayError, match="CURSOR_LOOP"):
            await EvidenceGateway(Loop(), contracts).list_tools()

    asyncio.run(scenario())


@pytest.mark.parametrize("representation", ["sdk2", "legacy", "text"])
def test_envelope_preserved_for_supported_response_forms(contracts, representation):
    envelope = fixture_evidence()
    if representation == "sdk2":
        response = CallToolResult(content=[], structured_content=envelope)
    elif representation == "legacy":
        response = SimpleNamespace(isError=False, structuredContent=envelope, content=[])
    else:
        response = CallToolResult(content=[TextContent(type="text", text=json.dumps(envelope))])

    async def scenario():
        gateway = EvidenceGateway(FakeSession([response]), contracts)
        received = await gateway.call("get_order", case_id="CASE_001", order_id="order-1")
        assert received == envelope
        received["data"]["status"] = "tampered"
        assert envelope["data"]["status"] == "delivered"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        CallToolResult(content=[], is_error=True),
        CallToolResult(content=[TextContent(type="text", text="not JSON")]),
        CallToolResult(content=[]),
        CallToolResult(content=[], structured_content={**fixture_evidence(), "extra": True}),
        CallToolResult(
            content=[], structured_content={**fixture_evidence(), "evidence_ref": "fake"}
        ),
        CallToolResult(
            content=[], structured_content=fixture_evidence(data={"value": float("nan")})
        ),
    ],
)
def test_gateway_rejects_invalid_results(contracts, response):
    async def scenario():
        with pytest.raises(GatewayError):
            await EvidenceGateway(FakeSession([response]), contracts).call(
                "get_order", case_id="CASE_001", order_id="order-1"
            )

    asyncio.run(scenario())


def test_consumption_requires_owned_receipt_and_actual_field_read(contracts, tmp_path):
    async def scenario():
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        session = FakeSession([CallToolResult(content=[], structured_content=fixture_evidence())])
        gateway = EvidenceGateway(session, contracts)
        collector = CaseEvidenceCollector(CaseScope("CASE_001", ("order-1",)), gateway, trace)
        agent = collector.for_actor("order-agent")
        receipt = await agent.fetch("get_order", task_id="task_1", order_id="order-1")
        assert not trace.path.exists()  # Fetch is not consumption.
        with pytest.raises(GatewayError, match="FIELD_MISSING"):
            agent.consume(receipt, pointers=("/missing",))
        with pytest.raises(GatewayError, match="NOT_OWNED"):
            agent.consume(replace(receipt, evidence_ref="ev_" + "x" * 24))
        with pytest.raises(GatewayError, match="NOT_OWNED"):
            collector.for_actor("payment-agent").consume(receipt)
        other = CaseEvidenceCollector(CaseScope("CASE_002", ("order-1",)), gateway, trace)
        with pytest.raises(GatewayError, match="NOT_OWNED"):
            other.for_actor("order-agent").consume(receipt)
        assert not trace.path.exists()
        observed = agent.consume(receipt, pointers=("/status",))
        assert observed.fields == {"/status": "delivered"}
        assert observed.evidence_ref == fixture_evidence()["evidence_ref"]
        written = events(trace.path)
        assert len(written) == 1
        assert written[0]["event_type"] == "tool_result_consumed"
        assert written[0]["case_id"] == "CASE_001"
        assert written[0]["evidence_refs"] == [receipt.evidence_ref]
        contracts.validate_trace(written[0], "test trace")
        snapshot = collector.snapshot()
        snapshot[receipt.evidence_ref]["data"]["status"] = "tampered"
        assert collector.snapshot()[receipt.evidence_ref]["data"]["status"] == "delivered"
        collector.close()
        with pytest.raises(GatewayError, match="CASE_CLOSED"):
            agent.consume(receipt)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("get_order_payments", {"order_id": "order-1"}),
        ("get_order", {"order_id": "outside"}),
        ("get_order", {"order_id": "order-1", "case_id": "CASE_002"}),
        ("get_customer_history", {"customer_unique_id": "outside"}),
    ],
)
def test_scope_and_actor_permissions_block_calls(contracts, tmp_path, tool, arguments):
    async def scenario():
        session = FakeSession()
        collector = CaseEvidenceCollector(
            CaseScope("CASE_001", ("order-1",)),
            EvidenceGateway(session, contracts),
            TraceWriter(tmp_path / "trace.jsonl", contracts),
        )
        with pytest.raises(GatewayError):
            await collector.for_actor("order-agent").fetch(tool, task_id="t", **arguments)
        assert session.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "envelope",
    [
        fixture_evidence("payment"),
        fixture_evidence(data={"order_id": "outside"}),
        fixture_evidence(data={"case_id": "CASE_002"}),
    ],
)
def test_wrong_domain_or_scope_never_stored_or_consumed(contracts, tmp_path, envelope):
    async def scenario():
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        collector = CaseEvidenceCollector(
            CaseScope("CASE_001", ("order-1",)),
            EvidenceGateway(
                FakeSession([CallToolResult(content=[], structured_content=envelope)]), contracts
            ),
            trace,
        )
        with pytest.raises(GatewayError):
            await collector.for_actor("order-agent").fetch(
                "get_order", task_id="t", order_id="order-1"
            )
        assert collector.snapshot() == {}
        assert not trace.path.exists()

    asyncio.run(scenario())


def test_conflicting_ref_does_not_replace_original(contracts, tmp_path):
    async def scenario():
        original = fixture_evidence()
        changed = deepcopy(original)
        changed["data"]["status"] = "canceled"
        session = FakeSession(
            [CallToolResult(content=[], structured_content=value) for value in (original, changed)]
        )
        collector = CaseEvidenceCollector(
            CaseScope("CASE_001", ("order-1",)),
            EvidenceGateway(session, contracts),
            TraceWriter(tmp_path / "trace.jsonl", contracts),
        )
        client = collector.for_actor("order-agent")
        await client.fetch("get_order", task_id="t1", order_id="order-1")
        with pytest.raises(GatewayError, match="REF_CONFLICT"):
            await client.fetch("get_order", task_id="t2", order_id="order-1")
        assert collector.snapshot()[original["evidence_ref"]] == original

    asyncio.run(scenario())


def test_transient_retry_then_real_consumption(contracts, tmp_path):
    async def scenario():
        session = FakeSession(
            [
                httpx2.ConnectError("private diagnostic"),
                CallToolResult(content=[], structured_content=fixture_evidence()),
            ]
        )
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        result = await collect_case_evidence(
            CaseScope("CASE_001", ("order-1",)),
            EvidenceGateway(session, contracts),
            trace,
            [SpecialistTask("t", "order-agent", "get_order", {"order_id": "order-1"})],
            limits=CollectionLimits(backoff_seconds=0),
        )
        assert result.complete
        assert len(session.calls) == 2 and session.calls[0] == session.calls[1]
        written = events(trace.path)
        assert sum(e["event_type"] == "tool_result_consumed" for e in written) == 1
        assert any(e.get("decision_code") == "MCP_RETRY" for e in written)
        assert "private diagnostic" not in trace.path.read_text()
        for event in written:
            contracts.validate_trace(event, "test trace")

    asyncio.run(scenario())


@pytest.mark.parametrize("transient,expected_calls", [(True, 3), (False, 1)])
def test_retry_bounded_and_tool_errors_not_retried(contracts, tmp_path, transient, expected_calls):
    async def scenario():
        failure = (
            httpx2.ConnectError("offline")
            if transient
            else CallToolResult(
                content=[TextContent(type="text", text="secret server detail")], is_error=True
            )
        )
        session = FakeSession([failure] * 4)
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        result = await collect_case_evidence(
            CaseScope("CASE_001", ("order-1",)),
            EvidenceGateway(session, contracts),
            trace,
            [SpecialistTask("t", "order-agent", "get_order", {"order_id": "order-1"})],
            limits=CollectionLimits(backoff_seconds=0),
        )
        assert not result.complete
        assert len(session.calls) == expected_calls
        assert result.evidence == {}
        assert not any(e["event_type"] == "tool_result_consumed" for e in events(trace.path))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,retryable",
    [(401, False), (403, False), (404, False), (429, True), (503, True), (400, False)],
)
def test_http_classification(status, retryable):
    response = httpx2.Response(status, headers={"Retry-After": "2"})
    failure = httpx2.HTTPStatusError(
        "raw", request=httpx2.Request("GET", "http://test"), response=response
    )
    error = transport_error(failure)
    assert error is not None and error.retryable is retryable
    if retryable:
        assert error.retry_after == 2


def test_specialist_barrier_concurrency_and_public_trace(contracts, tmp_path):
    async def scenario():
        session = FakeSession()
        session.delay = 0.005
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        tasks = [
            SpecialistTask(
                name, actor, name, {argument: "v1" if argument == "policy_version" else "order-1"}
            )
            for name, (actor, _, argument) in TOOL_ACCESS.items()
            if argument != "customer_unique_id"
        ]
        result = await collect_case_evidence(
            CaseScope("CASE_001", ("order-1",), ("v1",)),
            EvidenceGateway(session, contracts),
            trace,
            tasks,
        )
        assert result.complete
        assert 1 < session.max_active <= 3
        assert session.calls[-1][0] == "get_policy"
        assert all(arguments["case_id"] == "CASE_001" for _, arguments in session.calls)
        assert {item.actor for item in result.results} == {
            "order-agent",
            "payment-agent",
            "shipment-agent",
            "policy-agent",
        }
        written = events(trace.path)
        for event in written:
            contracts.validate_trace(event, "trace")
        assert sum(e["event_type"] == "tool_result_consumed" for e in written) == len(tasks)
        assert not any(
            e["event_type"] in {"case_finalized", "policy_decided", "verification_completed"}
            for e in written
        )

    asyncio.run(scenario())


def test_deadline_cancels_active_calls(contracts, tmp_path):
    async def scenario():
        session = FakeSession()
        session.delay = 60
        gateway = EvidenceGateway(session, contracts)
        await gateway.discover_tools()  # Exercise in-flight cancellation, not discovery timeout.
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        with pytest.raises(RuntimeError, match="CASE_TIMEOUT"):
            await collect_case_evidence(
                CaseScope("CASE_001", ("order-1",)),
                gateway,
                trace,
                [SpecialistTask("t", "order-agent", "get_order", {"order_id": "order-1"})],
                limits=CollectionLimits(case_timeout=0.5),
            )
        assert session.active == 0 and session.cancelled == 1
        assert not any(e["event_type"] == "tool_result_consumed" for e in events(trace.path))

    asyncio.run(scenario())


def test_missing_selected_field_is_failed_not_consumed(contracts, tmp_path):
    async def scenario():
        session = FakeSession()
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        result = await collect_case_evidence(
            CaseScope("CASE_001", ("order-1",)),
            EvidenceGateway(session, contracts),
            trace,
            [
                SpecialistTask(
                    "t", "order-agent", "get_order", {"order_id": "order-1"}, ("/missing",)
                )
            ],
        )
        assert not result.complete
        assert result.results[0].error_code == "EVIDENCE_FIELD_MISSING"
        assert result.results[0].observation is None
        assert not any(e["event_type"] == "tool_result_consumed" for e in events(trace.path))

    asyncio.run(scenario())


def test_auth_failure_cancels_siblings_and_skips_policy(contracts, tmp_path):
    class AuthSession(FakeSession):
        async def call_tool(self, name, *, arguments):
            if name == "get_order":
                await asyncio.sleep(0.005)
                raise httpx2.HTTPStatusError(
                    "secret",
                    request=httpx2.Request("POST", "http://test"),
                    response=httpx2.Response(403),
                )
            return await super().call_tool(name, arguments=arguments)

    async def scenario():
        session = AuthSession()
        session.delay = 1
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        tasks = [
            SpecialistTask("o", "order-agent", "get_order", {"order_id": "order-1"}),
            SpecialistTask("p", "payment-agent", "get_order_payments", {"order_id": "order-1"}),
            SpecialistTask("policy", "policy-agent", "get_policy", {"policy_version": "v1"}),
        ]
        with pytest.raises(GatewayError, match="MCP_AUTH_FAILED") as caught:
            await collect_case_evidence(
                CaseScope("CASE_001", ("order-1",), ("v1",)),
                EvidenceGateway(session, contracts),
                trace,
                tasks,
            )
        assert caught.value.code == "MCP_AUTH_FAILED"
        assert session.cancelled == 1 and session.active == 0
        assert not any(name == "get_policy" for name, _ in session.calls)
        assert not any(e["event_type"] == "tool_result_consumed" for e in events(trace.path))

    asyncio.run(scenario())


def test_retry_after_cannot_extend_case_deadline(contracts, tmp_path):
    async def scenario():
        failure = httpx2.HTTPStatusError(
            "rate limited",
            request=httpx2.Request("POST", "http://test"),
            response=httpx2.Response(429, headers={"Retry-After": "120"}),
        )
        session = FakeSession([failure])
        result = await collect_case_evidence(
            CaseScope("CASE_001", ("order-1",)),
            EvidenceGateway(session, contracts),
            TraceWriter(tmp_path / "trace.jsonl", contracts),
            [SpecialistTask("o", "order-agent", "get_order", {"order_id": "order-1"})],
            limits=CollectionLimits(case_timeout=1),
        )
        assert not result.complete
        assert result.results[0].error_code == "CASE_TIMEOUT"
        assert len(session.calls) == 1

    asyncio.run(scenario())


def test_invalid_later_task_fails_preflight_without_any_call(contracts, tmp_path):
    async def scenario():
        session = FakeSession()
        with pytest.raises(GatewayError):
            await collect_case_evidence(
                CaseScope("CASE_001", ("order-1",)),
                EvidenceGateway(session, contracts),
                TraceWriter(tmp_path / "trace.jsonl", contracts),
                [
                    SpecialistTask("ok", "order-agent", "get_order", {"order_id": "order-1"}),
                    SpecialistTask("bad", "payment-agent", "get_order", {"order_id": "order-1"}),
                ],
            )
        assert session.calls == []

    asyncio.run(scenario())


def test_cli_collection_isolated_from_submission_artifacts(contracts, tmp_path, monkeypatch):
    session = FakeSession()

    @asynccontextmanager
    async def fake_connection(*args):
        yield EvidenceGateway(session, contracts)

    monkeypatch.setattr(cli, "connect_gateway", fake_connection)
    monkeypatch.setattr(cli, "Contracts", lambda root: contracts)
    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda root: SimpleNamespace(mcp_endpoint="http://test/mcp", team_api_key="test-only"),
    )
    monkeypatch.setattr(
        cli,
        "load_case_set",
        lambda root: CaseSet(
            "test-v1", "l3a", ("CASE_001",), {"CASE_001": {"case_id": "CASE_001"}}
        ),
    )
    main_trace = tmp_path / "traces" / "trace.jsonl"
    main_trace.parent.mkdir()
    main_trace.write_text("existing trace", encoding="utf-8")
    output = tmp_path / "outputs" / "CASE_001.json"
    output.parent.mkdir()
    output.write_text("existing output", encoding="utf-8")
    args = cli.parser().parse_args(
        [
            "collect-evidence",
            "--case-id",
            "CASE_001",
            "--order-id",
            "order-1",
            "--tool",
            "get_order",
            "--pointer",
            "/order_id",
        ]
    )
    asyncio.run(cli._collect(tmp_path, args))
    assert main_trace.read_text() == "existing trace"
    assert output.read_text() == "existing output"
    reports = list((tmp_path / "traces").glob("collection_*/evidence.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["results"][0]["observation"]["fields"] == {"/order_id": "order-1"}
    written = events(reports[0].with_name("trace.jsonl"))
    assert written[0]["event_type"] == "case_received"
    assert not any(event["event_type"] == "case_finalized" for event in written)


def test_attempt_timeout_is_retried_but_never_consumed(contracts, tmp_path):
    async def scenario():
        session = FakeSession()
        session.delay = 60
        gateway = EvidenceGateway(session, contracts)
        await gateway.discover_tools()
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        result = await collect_case_evidence(
            CaseScope("CASE_001", ("order-1",)),
            gateway,
            trace,
            [SpecialistTask("t", "order-agent", "get_order", {"order_id": "order-1"})],
            limits=CollectionLimits(attempt_timeout=0.1, max_attempts=2, backoff_seconds=0),
        )
        assert not result.complete and result.results[0].error_code == "MCP_TIMEOUT"
        assert len(session.calls) == 2 and session.cancelled == 2
        assert result.evidence == {}
        assert not any(e["event_type"] == "tool_result_consumed" for e in events(trace.path))

    asyncio.run(scenario())


def test_case_deadline_includes_discovery(contracts, tmp_path):
    class SlowDiscovery(FakeSession):
        async def list_tools(self, *, params=None):
            await asyncio.sleep(60)

    async def scenario():
        session = SlowDiscovery()
        with pytest.raises(RuntimeError, match="CASE_TIMEOUT"):
            await collect_case_evidence(
                CaseScope("CASE_001", ("order-1",)),
                EvidenceGateway(session, contracts),
                TraceWriter(tmp_path / "trace.jsonl", contracts),
                [SpecialistTask("t", "order-agent", "get_order", {"order_id": "order-1"})],
                limits=CollectionLimits(case_timeout=0.02),
            )
        assert session.calls == []

    asyncio.run(scenario())
