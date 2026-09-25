from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from dataclasses import asdict
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .evidence import TOOL_ACCESS, CaseScope
from .mcp_gateway import connect_gateway
from .specialists import SpecialistTask
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import collect_case_evidence, solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path, *, as_json: bool = False) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        if as_json:
            catalog = await gateway.discover_tools()
            print(json.dumps([asdict(catalog[name]) for name in sorted(catalog)], indent=2))
            return
        for tool in await gateway.list_tools():
            print(tool)


async def _collect(root: Path, args: argparse.Namespace) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    if args.case_id not in case_set.case_ids:
        raise ValueError("case_id is outside the installed case-set")
    scope = CaseScope(
        args.case_id,
        tuple(args.order_id or ()),
        (args.policy_version,) if args.policy_version else (),
        (args.customer_unique_id,) if args.customer_unique_id else (),
    )
    values = {
        "order_id": scope.order_ids,
        "policy_version": scope.policy_versions,
        "customer_unique_id": scope.customer_unique_ids,
    }
    tasks = []
    for tool_name in dict.fromkeys(args.tool):
        actor, _, argument = TOOL_ACCESS[tool_name]
        if not values[argument]:
            raise ValueError(f"{tool_name} requires --{argument.replace('_', '-')}")
        for value in values[argument]:
            tasks.append(
                SpecialistTask(
                    f"task_{len(tasks) + 1}",
                    actor,
                    tool_name,
                    {argument: value},
                    tuple(args.pointer) if args.pointer else ("",),
                )
            )
    contracts = Contracts(root / "contracts" / "schemas")
    # An isolated diagnostic run: never overwrite submission outputs or trace.
    destination = root / "traces" / f"collection_{secrets.token_hex(8)}"
    trace = TraceWriter(destination / "trace.jsonl", contracts)
    trace.emit(case_id=scope.case_id, event_type="case_received", actor="coordinator")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        collection = await collect_case_evidence(scope, gateway, trace, tasks)
    report = destination / "evidence.json"
    report.write_text(
        json.dumps(asdict(collection), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"Evidence collection: {report}")
    if not collection.complete:
        raise RuntimeError("some evidence tasks failed; inspect the collection report")
    print(f"OK: {len(collection.results)} tasks; no final submission output generated")


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for case_id in case_set.case_ids:
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    discovery = commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    discovery.add_argument(
        "--json", action="store_true", help="include descriptions and input schemas"
    )
    collect = commands.add_parser("collect-evidence", help="run scoped Phase 3 specialist tasks")
    collect.add_argument("--case-id", required=True, help="case ID from the installed case-set")
    collect.add_argument("--tool", action="append", required=True, choices=sorted(TOOL_ACCESS))
    collect.add_argument("--order-id", action="append", help="explicit order lookup scope")
    collect.add_argument("--policy-version", help="policy version from the case")
    collect.add_argument("--customer-unique-id", help="explicit customer lookup scope")
    collect.add_argument(
        "--pointer", action="append", help="JSON pointer into data (default: root)"
    )
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root, as_json=args.json))
        elif args.command == "collect-evidence":
            asyncio.run(_collect(root, args))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
