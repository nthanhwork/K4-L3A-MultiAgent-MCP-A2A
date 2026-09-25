from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .run_state import RetryableRunError, prepare_attempt, run_context
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path, *, as_json: bool = False) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        if as_json:
            print(json.dumps(await gateway.describe_tools(), ensure_ascii=False, indent=2))
        else:
            for tool in await gateway.list_tools():
                print(tool)


def _transport_failure(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_transport_failure(item) for item in exc.exceptions)
    return isinstance(exc, (httpx2.TransportError, TimeoutError))


async def _run(
    root: Path,
    *,
    case_id: str | None = None,
    resume_run: Path | None = None,
) -> None:
    run_root = (resume_run or root / "dist" / "runs" / uuid4().hex).resolve()
    for attempt in range(2):
        try:
            await _run_once(root, case_id=case_id, run_root=run_root)
            return
        except (RetryableRunError, ExceptionGroup, httpx2.TransportError, TimeoutError) as exc:
            if not isinstance(exc, RetryableRunError) and not _transport_failure(exc):
                raise
            if attempt:
                detail = f" {exc}" if isinstance(exc, RetryableRunError) else ""
                raise RuntimeError(
                    f"MCP recovery exhausted; run is incomplete. Checkpoint: {run_root}.{detail}"
                ) from exc
            print(
                "MCP unavailable; reconnecting to retry unfinished cases (1/1).",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(5.0)


async def _run_once(
    root: Path,
    *,
    case_id: str | None = None,
    run_root: Path | None = None,
) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    if case_id is not None and case_id not in case_set.case_ids:
        raise ValueError(f"Unknown case ID: {case_id}")
    selected_ids = (case_id,) if case_id else case_set.case_ids
    contracts = Contracts(root / "contracts" / "schemas")
    run_root = run_root or root / "dist" / "runs" / uuid4().hex
    completed = prepare_attempt(run_root, run_context(settings, case_set, selected_ids), contracts)
    output_root = run_root / "outputs"
    trace_path = run_root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(trace_path, contracts)

    discovered_tools = None
    total_cases = len(case_set.case_ids)
    for i, case_id in enumerate(case_set.case_ids, 1):
        target = output_root / f"{case_id}.json"
        if target.exists():
            try:
                existing_data = json.loads(target.read_text(encoding="utf-8"))
                if (
                    len(existing_data.get("evidence_refs", [])) >= 3
                    and existing_data.get("affected_entities", {}).get("item_ids")
                    and existing_data.get("affected_entities", {}).get("seller_ids")
                ):
                    print(f"[{i}/{total_cases}] {case_id} (already completed)")
                    continue
            except Exception:
                pass

        case = case_set.cases[case_id]
        for attempt in range(5):
            try:
                async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
                    if discovered_tools is None:
                        discovered_tools = await gateway.list_tools()
                        if not discovered_tools:
                            raise RuntimeError("MCP Gateway returned no tools")
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case, gateway, trace)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    print(f"[{i}/{total_cases}] {case_id} -> {output['assessment']['primary_issue']}")
                break
            except BaseException as exc:
                if attempt == 4:
                    raise
                print(f"[{i}/{total_cases}] Retrying {case_id} after connection issue (attempt {attempt + 1}/5): {exc}")
                await asyncio.sleep(2.0 * (attempt + 1))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    tools = commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    tools.add_argument("--json", action="store_true", help="include tool descriptions and schemas")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--case-id", help="smoke test one case; publishes a fresh output/trace set on completion"
    )
    run.add_argument("--resume-run", type=Path, help="resume a saved run with matching context")
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
        elif args.command == "run":
            asyncio.run(_run(root, case_id=args.case_id, resume_run=args.resume_run))
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
