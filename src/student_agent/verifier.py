"""Independent checks on the proposed decision before it leaves the workflow."""

from __future__ import annotations

from typing import Any

from .evidence import CaseEvidence
from .reasoning import money


def _values(data: Any, key: str) -> set[str]:
    found: set[str] = set()
    if isinstance(data, dict):
        if data.get(key) is not None:
            found.add(str(data[key]))
        for value in data.values():
            found.update(_values(value, key))
    elif isinstance(data, list):
        for value in data:
            found.update(_values(value, key))
    return found


def verify(output: dict[str, Any], evidence: CaseEvidence) -> None:
    evidence.trace.contracts.validate_output(output, "verifier")
    if output["case_id"] != evidence.case_id:
        raise ValueError("Case ID changed during handoff")
    owned = set(evidence.refs(*evidence.records))
    cited = set(output["evidence_refs"])
    if not cited <= owned:
        raise ValueError("Output cites evidence outside this case")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= cited:
            raise ValueError("Claim evidence is not linked to the output")
        if claim["verdict"] != "insufficient_evidence" and not claim["evidence_refs"]:
            raise ValueError("A claim verdict requires evidence")
    data = [
        record["data"]
        for name, record in evidence.records.items()
        if name != "get_policy" and record["evidence_ref"] in cited
    ]
    for field, key in (
        ("order_ids", "order_id"),
        ("item_ids", "order_item_id"),
        ("seller_ids", "seller_id"),
        ("payment_references", "payment_reference"),
        ("shipment_ids", "shipment_id"),
    ):
        if not set(output["affected_entities"][field]) <= _values(data, key):
            raise ValueError(f"Ungrounded entity in {field}")
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if (
            party["party_type"] == "seller"
            and party["party_id"] not in output["affected_entities"]["seller_ids"]
        ):
            raise ValueError("Responsible seller is outside affected entities")
    finance = output["financial_resolution"]
    total = money(finance["recommended_refund_brl"])
    if total != sum((money(line["amount_brl"]) for line in finance["refund_lines"]), money(0)):
        raise ValueError("Refund lines do not sum to the recommendation")
    for line in finance["refund_lines"]:
        ids = set().union(*(set(v) for v in output["affected_entities"].values()))
        if line["entity_id"] is not None and line["entity_id"] not in ids:
            raise ValueError("Refund line targets an unknown entity")
    status = output["assessment"]["case_status"]
    issue = output["assessment"]["primary_issue"]
    if total and status != "action_required":
        raise ValueError("A positive refund requires action_required")
    if issue == "insufficient_evidence":
        if status != "needs_investigation" or total:
            raise ValueError("Missing evidence cannot authorize a refund")
    else:
        if not cited:
            raise ValueError("A business decision requires evidence")
        policy = evidence.data("get_policy", {}).get("rules", {}).get(issue, {})
        if status != policy.get("case_status"):
            raise ValueError("Decision status contradicts policy")
        if output["resolution_actions"] != [policy.get("recommended_action")]:
            raise ValueError("Actions contradict policy")
        if total != money(policy.get("refund_brl")):
            raise ValueError("Refund contradicts policy")
