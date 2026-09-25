from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from mcp import ClientSession, types

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, ToolCallError


def make_gateway():
    session = create_autospec(ClientSession, instance=True)
    tool = types.Tool(name="get_order", input_schema={"type": "object"})
    session.list_tools.return_value = SimpleNamespace(tools=[tool], next_cursor=None)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
    return session, EvidenceGateway(session, contracts)


def test_discovery_uses_installed_sdk_signature_and_paginates():
    session, gateway = make_gateway()
    session.list_tools.side_effect = [
        SimpleNamespace(tools=[types.Tool(name="get_order", input_schema={})], next_cursor="page2"),
        SimpleNamespace(tools=[types.Tool(name="get_policy", input_schema={})], next_cursor=None),
    ]

    async def run():
        assert await gateway.list_tools() == ["get_order", "get_policy"]
        assert await gateway.list_tools() == ["get_order", "get_policy"]

    asyncio.run(run())
    assert session.list_tools.await_count == 2
    assert session.list_tools.await_args_list[1].kwargs["params"].cursor == "page2"


def test_unknown_tool_is_not_called():
    session, gateway = make_gateway()
    with pytest.raises(ToolCallError, match="unavailable"):
        asyncio.run(gateway.call("invented_tool", case_id="TEST_CASE_001"))
    session.call_tool.assert_not_awaited()


def test_application_error_is_not_retried():
    session, gateway = make_gateway()
    session.call_tool.return_value = SimpleNamespace(is_error=True, content=[])
    with pytest.raises(ToolCallError):
        asyncio.run(gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1"))
    assert session.call_tool.await_count == 1


def test_transport_timeout_retries_with_identical_case_scope():
    session, gateway = make_gateway()
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_synthetic_gateway_test_0001",
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": {"order_id": "order-1"},
    }
    session.call_tool.side_effect = [
        TimeoutError(),
        SimpleNamespace(is_error=False, structured_content=evidence, content=[]),
    ]
    assert (
        asyncio.run(gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1"))
        == evidence
    )
    assert session.call_tool.await_count == 2
    assert session.call_tool.await_args_list[0] == session.call_tool.await_args_list[1]


def test_paced_gateway_serializes_calls_and_waits_after_failure():
    session, original = make_gateway()
    gateway = EvidenceGateway(session, original._contracts, request_interval=0.01)
    starts = []
    finishes = []
    active = 0
    peak = 0

    async def call(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0)
        finishes.append(asyncio.get_running_loop().time())
        active -= 1
        return SimpleNamespace(is_error=True, content=[])

    session.call_tool.side_effect = call

    async def run():
        await gateway.list_tools()
        return await asyncio.gather(
            *(gateway.call("get_order", case_id=f"TEST_{i}", order_id="order-1") for i in range(3)),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    assert all(isinstance(result, ToolCallError) for result in results)
    assert peak == 1
    assert len(starts) == 3
    assert all(starts[i + 1] - finishes[i] >= 0.01 for i in range(2))


@pytest.mark.parametrize("interval", [-1, float("nan"), float("inf")])
def test_pacing_rejects_invalid_intervals(interval):
    session, original = make_gateway()
    with pytest.raises(ValueError, match="finite and nonnegative"):
        EvidenceGateway(session, original._contracts, request_interval=interval)


@pytest.mark.parametrize("code", [types.INTERNAL_ERROR, types.REQUEST_TIMEOUT])
def test_transient_mcp_protocol_error_retries_once(code, monkeypatch):
    from unittest.mock import AsyncMock

    from mcp.shared.exceptions import MCPError

    session, gateway = make_gateway()
    monkeypatch.setattr("student_agent.mcp_gateway.asyncio.sleep", AsyncMock())
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_synthetic_protocol_test_0001",
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": {"order_id": "order-1"},
    }
    session.call_tool.side_effect = [
        MCPError(code, "Transient upstream error"),
        SimpleNamespace(is_error=False, structured_content=evidence, content=[]),
    ]
    assert (
        asyncio.run(gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1"))
        == evidence
    )
    assert session.call_tool.await_count == 2
    assert session.call_tool.await_args_list[0] == session.call_tool.await_args_list[1]


def test_mcp_parameter_error_is_not_retried():
    from mcp.shared.exceptions import MCPError

    session, gateway = make_gateway()
    session.call_tool.side_effect = MCPError(types.INVALID_PARAMS, "Invalid parameters")
    with pytest.raises(MCPError):
        asyncio.run(gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1"))
    assert session.call_tool.await_count == 1


def test_mcp_protocol_retries_are_bounded(monkeypatch):
    from unittest.mock import AsyncMock

    from mcp.shared.exceptions import MCPError

    session, gateway = make_gateway()
    monkeypatch.setattr("student_agent.mcp_gateway.asyncio.sleep", AsyncMock())
    session.call_tool.side_effect = MCPError(types.INTERNAL_ERROR, "Upstream unavailable")
    with pytest.raises(MCPError):
        asyncio.run(gateway.call("get_order", case_id="TEST_CASE_001", order_id="order-1"))
    assert session.call_tool.await_count == 2
