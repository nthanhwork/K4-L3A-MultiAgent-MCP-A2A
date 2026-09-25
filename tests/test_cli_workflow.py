from __future__ import annotations

import asyncio
import copy
import json
import zipfile
from contextlib import asynccontextmanager

import pytest

from student_agent import cli
from student_agent.cases import CaseSet, load_case_set
from student_agent.contracts import Contracts
from student_agent.submission import package_submission, validate_artifacts
from test_workflow import ROOT, FakeGateway, fixture


def prepare_root(tmp_path, monkeypatch):
    case, data = fixture("canceled_order_paid")
    ids = [f"TEST_CASE_{number:03d}" for number in range(1, 101)]
    (tmp_path / "inputs").mkdir()
    (tmp_path / "case-set.json").write_text(
        json.dumps(
            {
                "case_set_version": "offline-test-v1",
                "variant_id": "l3a",
                "case_ids": ids,
            }
        )
    )
    for case_id in ids:
        item = copy.deepcopy(case)
        item["case_id"] = case_id
        (tmp_path / "inputs" / f"{case_id}.json").write_text(json.dumps(item))
    (tmp_path / "contracts").symlink_to(ROOT / "contracts", target_is_directory=True)
    monkeypatch.setenv("COMPETITION_API_URL", "http://offline.invalid")
    monkeypatch.setenv("MCP_ENDPOINT", "http://offline.invalid/mcp")
    monkeypatch.setenv("COMPETITION_TEAM_API_KEY", "sk-team-offline_test_key_0000")

    @asynccontextmanager
    async def connect(*args):
        yield FakeGateway(data)

    monkeypatch.setattr(cli, "connect_gateway", connect)
    return ids


def test_full_cli_run_lifecycle_validation_and_package(tmp_path, monkeypatch):
    ids = prepare_root(tmp_path, monkeypatch)
    asyncio.run(cli._run(tmp_path))
    contracts = Contracts(ROOT / "contracts/schemas")
    outputs, lines = validate_artifacts(tmp_path, load_case_set(tmp_path), contracts)
    assert len(outputs) == 100
    events = [json.loads(line) for line in lines]
    for case_id in ids:
        case_events = [event for event in events if event["case_id"] == case_id]
        assert case_events[0]["event_type"] == "case_received"
        assert case_events[-1]["event_type"] == "case_finalized"
        assert any(event["event_type"] == "verification_completed" for event in case_events)
    archive = package_submission(tmp_path, tmp_path / "dist/submission.zip")
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {
            "manifest.json",
            "trace.jsonl",
            *(f"outputs/{case_id}.json" for case_id in ids),
        }
        assert b"sk-team-" not in b"".join(package.read(name) for name in package.namelist())


def test_smoke_run_is_not_a_complete_submission(tmp_path, monkeypatch):
    ids = prepare_root(tmp_path, monkeypatch)
    asyncio.run(cli._run(tmp_path, case_id=ids[0]))
    assert len(list((tmp_path / "outputs").glob("*.json"))) == 1
    with pytest.raises(ValueError, match="outputs do not match"):
        package_submission(tmp_path, tmp_path / "submission.zip")


def test_unknown_case_preserves_existing_artifacts(tmp_path, monkeypatch):
    prepare_root(tmp_path, monkeypatch)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    sentinel = outputs / "previous.json"
    sentinel.write_text("previous")
    with pytest.raises(ValueError, match="Unknown case ID"):
        asyncio.run(cli._run(tmp_path, case_id="MISSING_CASE"))
    assert sentinel.read_text() == "previous"


def test_failed_case_does_not_stop_remaining_cases(tmp_path, monkeypatch):
    ids = prepare_root(tmp_path, monkeypatch)
    real_cases = load_case_set(tmp_path)
    selected = CaseSet(real_cases.version, "l3a", tuple(ids[:2]), real_cases.cases)
    monkeypatch.setattr(cli, "load_case_set", lambda root: selected)
    solver = cli.solve_case

    async def solve(case, gateway, trace):
        if case["case_id"] == ids[0]:
            raise ValueError("Synthetic invalid input")
        return await solver(case, gateway, trace)

    monkeypatch.setattr(cli, "solve_case", solve)
    with pytest.raises(RuntimeError, match="1 cases failed"):
        asyncio.run(cli._run(tmp_path))
    assert not (tmp_path / "outputs" / f"{ids[0]}.json").exists()
    assert not (tmp_path / "outputs" / f"{ids[1]}.json").exists()
    attempts = list((tmp_path / "dist/runs").iterdir())
    assert len(attempts) == 1
    assert (attempts[0] / "outputs" / f"{ids[1]}.json").exists()


def test_transport_session_failure_restarts_whole_run(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    import httpx2

    runner = AsyncMock(
        side_effect=[ExceptionGroup("MCP background", [httpx2.ConnectError("offline")]), None]
    )
    monkeypatch.setattr(cli, "_run_once", runner)
    asyncio.run(cli._run(tmp_path))
    assert runner.await_count == 2
    assert runner.await_args_list[0] == runner.await_args_list[1]


def test_application_exception_group_does_not_restart(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    runner = AsyncMock(side_effect=ExceptionGroup("application", [ValueError("invalid case")]))
    monkeypatch.setattr(cli, "_run_once", runner)
    with pytest.raises(ExceptionGroup):
        asyncio.run(cli._run(tmp_path))
    assert runner.await_count == 1


def test_session_restart_is_bounded(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    import httpx2

    runner = AsyncMock(side_effect=ExceptionGroup("network", [httpx2.ConnectError("offline")]))
    monkeypatch.setattr(cli, "_run_once", runner)
    with pytest.raises(RuntimeError, match="run is incomplete"):
        asyncio.run(cli._run(tmp_path))
    assert runner.await_count == 2


def test_service_outage_stops_after_three_empty_cases_and_preserves_outputs(tmp_path, monkeypatch):
    from student_agent.mcp_gateway import ToolCallError

    prepare_root(tmp_path, monkeypatch)
    _, data = fixture()
    calls = []

    class OfflineGateway(FakeGateway):
        async def call(self, name, *, case_id, **arguments):
            calls.append(case_id)
            raise ToolCallError("Synthetic unavailable service")

    @asynccontextmanager
    async def connect(*args):
        yield OfflineGateway(data)

    monkeypatch.setattr(cli, "connect_gateway", connect)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    previous = tmp_path / "outputs/previous.json"
    previous.write_text("previous output")
    trace = tmp_path / "traces/trace.jsonl"
    trace.write_text("previous trace")
    with pytest.raises(RuntimeError, match="3 consecutive cases without evidence"):
        asyncio.run(cli._run(tmp_path))
    assert len(set(calls)) == 3
    assert previous.read_text() == "previous output"
    assert trace.read_text() == "previous trace"
    attempts = list((tmp_path / "dist/runs").iterdir())
    assert len(list((attempts[0] / "outputs").glob("*.json"))) == 3


def test_outage_closes_sdk_task_groups_before_raising(tmp_path, monkeypatch):
    import anyio

    from student_agent.mcp_gateway import ToolCallError

    ids = prepare_root(tmp_path, monkeypatch)
    _, data = fixture()

    class OfflineGateway(FakeGateway):
        async def call(self, name, *, case_id, **arguments):
            raise ToolCallError("Synthetic unavailable service")

    @asynccontextmanager
    async def connect(*args):
        async with anyio.create_task_group():
            yield OfflineGateway(data)

    monkeypatch.setattr(cli, "connect_gateway", connect)
    # Smoke runs must also preserve published artifacts when evidence is absent.
    (tmp_path / "outputs").mkdir()
    previous = tmp_path / "outputs/previous.json"
    previous.write_text("previous")
    with pytest.raises(RuntimeError, match="1 cases have no evidence"):
        asyncio.run(cli._run(tmp_path, case_id=ids[0]))
    assert previous.read_text() == "previous"


def test_reconnect_preserves_successful_cases_and_replaces_failed_case_trace(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from student_agent.mcp_gateway import ToolCallError

    ids = prepare_root(tmp_path, monkeypatch)
    cases = load_case_set(tmp_path)
    selected = CaseSet(cases.version, "l3a", tuple(ids[:2]), cases.cases)
    monkeypatch.setattr(cli, "load_case_set", lambda root: selected)
    monkeypatch.setattr(cli.asyncio, "sleep", AsyncMock())
    _, data = fixture("canceled_order_paid")
    calls = []
    connections = 0

    class Gateway(FakeGateway):
        def __init__(self, connection):
            super().__init__(data)
            self.connection = connection

        async def call(self, name, *, case_id, **arguments):
            calls.append((self.connection, case_id, name))
            if self.connection == 1 and case_id == ids[1]:
                raise ToolCallError("Synthetic expired session")
            return await super().call(name, case_id=case_id, **arguments)

    @asynccontextmanager
    async def connect(*args):
        nonlocal connections
        connections += 1
        yield Gateway(connections)

    monkeypatch.setattr(cli, "connect_gateway", connect)
    asyncio.run(cli._run(tmp_path))
    assert connections == 2
    assert not any(connection == 2 and case_id == ids[0] for connection, case_id, _ in calls)
    events = [
        json.loads(line) for line in (tmp_path / "traces/trace.jsonl").read_text().splitlines()
    ]
    assert not any(e.get("decision_code") == "EVIDENCE_UNAVAILABLE" for e in events)
    for case_id in ids[:2]:
        case_events = [e for e in events if e["case_id"] == case_id]
        assert sum(e["event_type"] == "case_received" for e in case_events) == 1
        assert sum(e["event_type"] == "case_finalized" for e in case_events) == 1
    assert len(list((tmp_path / "dist/runs").iterdir())) == 1
    run = next((tmp_path / "dist/runs").iterdir())
    assert list((run / "attempt-traces").glob("*.jsonl"))
    assert "sk-team-" not in (run / "context.json").read_text()


@pytest.mark.parametrize("change", ["team", "input", "endpoint"])
def test_resume_rejects_changed_context(tmp_path, monkeypatch, change):
    ids = prepare_root(tmp_path, monkeypatch)
    asyncio.run(cli._run(tmp_path, case_id=ids[0]))
    run = next((tmp_path / "dist/runs").iterdir())
    if change == "team":
        monkeypatch.setenv("COMPETITION_TEAM_API_KEY", "sk-team-other_team_key_0000")
    elif change == "endpoint":
        monkeypatch.setenv("MCP_ENDPOINT", "http://another.invalid/mcp")
    else:
        path = tmp_path / "inputs" / f"{ids[0]}.json"
        case = json.loads(path.read_text())
        case["opened_at"] = "2018-02-12T09:00:00-03:00"
        path.write_text(json.dumps(case))
    with pytest.raises(ValueError, match="Run context changed"):
        asyncio.run(cli._run(tmp_path, case_id=ids[0], resume_run=run))
