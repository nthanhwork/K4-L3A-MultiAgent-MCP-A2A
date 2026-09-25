"""Case-local evidence collection and observable in-process A2A handoffs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway, ToolCallError
from .trace import TraceWriter

TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}
OWNERSHIP = {
    "order-agent": {"get_order", "get_order_items", "get_sellers"},
    "payment-agent": {"get_order_payments", "get_payment_timeline", "get_refund_timeline"},
    "shipment-agent": {"get_shipment_summary"},
    "policy-agent": {"get_policy"},
}


def check_scope(data: Any, order_id: str) -> None:
    if isinstance(data, dict):
        if "order_id" in data and data["order_id"] != order_id:
            raise ValueError("MCP evidence belongs to a different order")
        for value in data.values():
            check_scope(value, order_id)
    elif isinstance(data, list):
        for value in data:
            check_scope(value, order_id)


@dataclass(frozen=True)
class AgentReport:
    case_id: str
    sender: str
    target: str
    evidence_refs: tuple[str, ...]
    failures: tuple[str, ...]


@dataclass
class CaseEvidence:
    case_id: str
    order_id: str
    policy_version: str
    gateway: EvidenceGateway
    trace: TraceWriter
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    async def collect(self, actor: str, tool_names: list[str]) -> AgentReport:
        self.trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code="COLLECT_EVIDENCE",
            attributes={"tool_count": len(tool_names)},
        )
        refs = []
        failed = []
        for name in tool_names:
            if name not in OWNERSHIP.get(actor, set()):
                raise ValueError(f"{actor} cannot call {name}")
            args = (
                {"policy_version": self.policy_version}
                if name == "get_policy"
                else {"order_id": self.order_id}
            )
            try:
                result = await self.gateway.call(name, case_id=self.case_id, **args)
                self.trace.contracts.validate_evidence(result)
                if result["domain"] != TOOL_DOMAINS[name]:
                    raise ValueError("Unexpected evidence domain")
                check_scope(result["data"], self.order_id)
                if name == "get_policy":
                    if not isinstance(result["data"], dict):
                        raise ValueError("Policy must be an object")
                    if result["data"].get("policy_version") != self.policy_version:
                        raise ValueError("Policy version mismatch")
                elif name in {"get_order_items", "get_order_payments", "get_sellers"}:
                    if not isinstance(result["data"], list):
                        raise ValueError("Expected evidence rows")
                elif not isinstance(result["data"], dict):
                    raise ValueError("Expected an evidence object")
                if (
                    name
                    not in {
                        "get_policy",
                        "get_order_items",
                        "get_order_payments",
                        "get_sellers",
                    }
                    and result["data"].get("order_id") != self.order_id
                ):
                    raise ValueError("Missing authoritative order scope")
                self.records[name] = result
                refs.append(result["evidence_ref"])
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=name,
                    evidence_refs=[result["evidence_ref"]],
                )
            except (
                ToolCallError,
                MCPError,
                TimeoutError,
                httpx2.TransportError,
                ValueError,
            ) as exc:
                # Keep diagnostic codes only: server messages can contain sensitive data.
                code = "TIMEOUT" if isinstance(exc, TimeoutError) else type(exc).__name__.upper()
                self.failures[name] = code
                failed.append(name)
                diagnostic = {"error_code": code}
                if isinstance(exc, MCPError):
                    diagnostic["mcp_error_code"] = exc.code
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="handoff",
                    actor=actor,
                    target="coordinator",
                    decision_code="EVIDENCE_UNAVAILABLE",
                    tool_name=name,
                    attributes=diagnostic,
                )
        report = AgentReport(self.case_id, actor, "coordinator", tuple(refs), tuple(failed))
        self.trace.emit(
            case_id=report.case_id,
            event_type="handoff",
            actor=report.sender,
            target=report.target,
            evidence_refs=list(report.evidence_refs),
            decision_code="SPECIALIST_COMPLETED" if not failed else "SPECIALIST_INCOMPLETE",
        )
        return report

    def data(self, tool: str, default: Any = None) -> Any:
        return self.records.get(tool, {}).get("data", default)

    def refs(self, *tools: str) -> list[str]:
        return list(
            dict.fromkeys(
                self.records[name]["evidence_ref"] for name in tools if name in self.records
            )
        )
