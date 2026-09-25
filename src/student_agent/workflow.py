from __future__ import annotations

import asyncio
from typing import Any

from .evidence import CaseEvidence
from .mcp_gateway import EvidenceGateway
from .reasoning import InvestigationError, investigate
from .trace import TraceWriter
from .verifier import verify


def _incomplete(
    case: dict[str, Any],
    evidence: CaseEvidence,
    *,
    conflicts: list[dict] | None = None,
) -> dict[str, Any]:
    refs = evidence.refs(*evidence.records)
    order = evidence.data("get_order", {})
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": evidence.case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.45,
        },
        "affected_entities": {
            "order_ids": [order["order_id"]] if order.get("order_id") else [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",
                "confidence": 0.45,
                "evidence_refs": refs,
            }
            for claim in case.get("customer_request", {}).get("claims", [])
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "EVIDENCE_INCOMPLETE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts or [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["investigate_missing_evidence"],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate scoped specialists, apply MCP policy, and independently verify.

    The CLI owns case_received/case_finalized. No evidence survives this call.
    Customer topics determine what to investigate, never the resulting diagnosis.
    """
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    order_id, policy_version = request.get("claimed_order_id"), case.get("policy_version")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("Input must contain a claimed_order_id")
    if not isinstance(policy_version, str) or not policy_version:
        raise ValueError("Input must contain a policy_version")
    evidence = CaseEvidence(case_id, order_id, policy_version, gateway, trace)
    await gateway.list_tools()
    # Explicit ownership and bounded fan-out: at most three read-only calls in flight.
    payment_tools = ["get_order_payments", "get_payment_timeline"]
    refund_claim = any(
        claim.get("topic") in {"refund_pending", "refund_failed"}
        for claim in request.get("claims", [])
    )
    if refund_claim:
        payment_tools.append("get_refund_timeline")
    reports = await asyncio.gather(
        evidence.collect("order-agent", ["get_order", "get_order_items"]),
        evidence.collect("payment-agent", payment_tools),
        evidence.collect("shipment-agent", ["get_shipment_summary"]),
    )
    if any(report.case_id != case_id or report.target != "coordinator" for report in reports):
        raise ValueError("Specialist handoff crossed case scope")
    await evidence.collect("policy-agent", ["get_policy"])
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        decision_code="ASSESS_COLLECTED_FACTS",
        evidence_refs=evidence.refs(*evidence.records),
    )
    try:
        output = investigate(case, evidence)
        parties = output["root_cause_analysis"]["responsible_parties"]
        if any(party["party_type"] == "seller" for party in parties):
            await evidence.collect("order-agent", ["get_sellers"])
            if "get_sellers" in evidence.failures:
                raise ValueError("Seller ownership cannot be verified")
            output = investigate(case, evidence)
    except (ValueError, KeyError, TypeError) as exc:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="policy-agent",
            target="coordinator",
            decision_code="ASSESSMENT_INCOMPLETE",
            attributes={"validation_error": str(exc)[:160]},
        )
        output = _incomplete(
            case, evidence, conflicts=exc.conflicts if isinstance(exc, InvestigationError) else None
        )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=output["assessment"]["primary_issue"].upper(),
        evidence_refs=output["evidence_refs"],
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier",
        decision_code="VERIFY_DECISION",
    )
    try:
        verify(output, evidence)
    except ValueError:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="verifier",
            target="coordinator",
            decision_code="DECISION_REJECTED",
        )
        output = _incomplete(case, evidence)
        verify(output, evidence)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        evidence_refs=output["evidence_refs"],
        decision_code=(
            "NEEDS_INVESTIGATION"
            if output["assessment"]["primary_issue"] == "insufficient_evidence"
            else "VERIFIED"
        ),
    )
    return output
