"""Checkpoint a client run across transport sessions without crossing input/team scope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .cases import CaseSet
from .config import Settings
from .contracts import Contracts


class RetryableRunError(RuntimeError):
    """A connection/evidence outage left cases to retry within the same client run."""


def run_context(settings: Settings, cases: CaseSet, selected: tuple[str, ...]) -> dict[str, Any]:
    canonical = json.dumps(cases.cases, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "variant_id": cases.variant_id,
        "case_set_version": cases.version,
        "input_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "team_key_sha256": hashlib.sha256(settings.team_api_key.encode()).hexdigest(),
        "mcp_endpoint": settings.mcp_endpoint,
        "competition_api_url": settings.competition_api_url,
        "selected_case_ids": list(selected),
    }


def prepare_attempt(
    run_root: Path,
    context: dict[str, Any],
    contracts: Contracts,
) -> set[str]:
    metadata = run_root / "context.json"
    if metadata.exists():
        previous = json.loads(metadata.read_text(encoding="utf-8"))
        if previous.get("context") != context:
            raise ValueError(
                "Run context changed (team, endpoint, inputs or selection); start fresh"
            )
    else:
        if any((run_root / "outputs").glob("*.json")) or (run_root / "traces/trace.jsonl").exists():
            raise ValueError("Existing run has no context checkpoint; cannot auto-resume")
        run_root.mkdir(parents=True, exist_ok=True)
        metadata.write_text(json.dumps({"version": 1, "context": context}, indent=2) + "\n")
    trace_path = run_root / "traces/trace.jsonl"
    events = []
    if trace_path.exists():
        events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    selected = set(context["selected_case_ids"])
    by_case: dict[str, list[dict]] = {}
    event_ids = set()
    for event in events:
        contracts.validate_trace(event, "checkpoint trace")
        if event["case_id"] not in selected or event["event_id"] in event_ids:
            raise ValueError("Checkpoint trace has an unexpected case or duplicate event ID")
        event_ids.add(event["event_id"])
        by_case.setdefault(event["case_id"], []).append(event)
    complete = set()
    for case_id, case_events in by_case.items():
        target = run_root / "outputs" / f"{case_id}.json"
        if not target.exists() or case_events[-1]["event_type"] != "case_finalized":
            continue
        output = json.loads(target.read_text())
        contracts.validate_output(output, f"checkpoint {case_id}")
        if output["case_id"] != case_id:
            raise ValueError("Checkpoint output case ID mismatch")
        if not output["evidence_refs"] or any(
            event.get("decision_code") == "EVIDENCE_UNAVAILABLE" for event in case_events
        ):
            continue
        consumed = {
            ref
            for event in case_events
            if event["event_type"] == "tool_result_consumed"
            for ref in event.get("evidence_refs", [])
        }
        if not set(output["evidence_refs"]) <= consumed:
            raise ValueError("Checkpoint contains unlinked evidence")
        if case_events[0]["event_type"] != "case_received":
            raise ValueError("Checkpoint lifecycle is incomplete")
        complete.add(case_id)
    if events:
        archive = run_root / "attempt-traces"
        archive.mkdir(exist_ok=True)
        number = len(list(archive.glob("*.jsonl"))) + 1
        (archive / f"attempt-{number:03d}.jsonl").write_text(trace_path.read_text())
        # Retry failed cases from scratch. Their old refs and partial events are
        # quarantined, never combined with the replacement case result.
        retained = [event for event in events if event["case_id"] in complete]
        temporary = trace_path.with_suffix(".jsonl.tmp")
        temporary.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in retained))
        temporary.replace(trace_path)
    return complete
