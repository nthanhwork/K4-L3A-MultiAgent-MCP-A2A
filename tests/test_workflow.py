"""Offline tests use synthetic evidence only; fixtures are never submission artifacts."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.evidence import TOOL_DOMAINS, CaseEvidence
from student_agent.mcp_gateway import ToolCallError
from student_agent.trace import TraceWriter
from student_agent.verifier import verify
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]


def fixture(issue="unsupported_claim"):
    case = {
        "case_id": "TEST_CASE_001",
        "opened_at": "2018-01-12T09:00:00-03:00",
        "policy_version": "TEST_POLICY",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": issue}],
        },
    }
    status = {"canceled_order_paid": "canceled", "unavailable_order_paid": "unavailable"}.get(
        issue, "delivered"
    )
    order = {
        "order_id": "order-1",
        "order_status": status,
        "order_purchase_timestamp": "2018-01-01T09:00:00-03:00",
        "order_delivered_carrier_date": "2018-01-02T09:00:00-03:00",
        "order_delivered_customer_date": "2018-01-09T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-01-10T09:00:00-03:00",
    }
    if status in {"canceled", "unavailable"}:
        order["order_delivered_customer_date"] = None
    if issue.startswith("late_delivery"):
        order["order_delivered_customer_date"] = "2018-01-11T09:00:00-03:00"
    if issue == "late_delivery_seller":
        order["order_delivered_carrier_date"] = "2018-01-06T09:00:00-03:00"
    item = {
        "order_id": "order-1",
        "order_item_id": "item-1",
        "seller_id": "seller-1",
        "price": "90.00",
        "freight_value": "10.00",
        "shipping_limit_date": "2018-01-03T09:00:00-03:00",
    }
    amounts = {
        "valid_split_payment": [50, 50],
        "duplicate_charge": [100, 100],
        "payment_mismatch": [20],
    }.get(issue, [100])
    payments = [
        {
            "order_id": "order-1",
            "payment_reference": f"pay-{i}",
            "payment_sequential": str(i),
            "payment_value": str(amount),
        }
        for i, amount in enumerate(amounts, 1)
    ]
    events = [
        {
            "order_id": "order-1",
            "event_at": f"2018-01-01T{9 + i:02d}:00:00-03:00",
            "event_type": "captured",
            "amount_brl": str(amount),
            "status": "confirmed",
        }
        for i, amount in enumerate(amounts, 1)
    ]
    if issue == "payment_mismatch":
        events.append(
            {
                "order_id": "order-1",
                "event_at": "2018-01-02T09:00:00-03:00",
                "event_type": "reconciliation_mismatch",
                "amount_brl": "20",
                "status": "open",
            }
        )
    refunds = []
    if issue in {"refund_pending", "refund_failed"}:
        refunds = [
            {
                "order_id": "order-1",
                "event_at": "2018-01-11T09:00:00-03:00",
                "event_type": "refund_requested",
                "amount_brl": "100",
                "status": issue.removeprefix("refund_"),
            }
        ]
    rules = {}
    for key, amount, action, party in (
        ("canceled_order_paid", 100, "issue_refund", "platform"),
        ("unavailable_order_paid", 100, "issue_refund", "seller"),
        ("late_delivery_seller", 10, "refund_freight", "seller"),
        ("late_delivery_logistics", 10, "refund_freight", "logistics_provider"),
        ("valid_split_payment", 0, "document_no_action", "customer"),
        ("payment_mismatch", 20, "reconcile_payment", "payment_provider"),
        ("duplicate_charge", 100, "refund_duplicate_charge", "payment_provider"),
        ("refund_pending", 0, "monitor_refund", "payment_provider"),
        ("refund_failed", 100, "retry_refund", "payment_provider"),
        ("unsupported_claim", 0, "document_no_action", "customer"),
    ):
        rules[key] = {
            "case_status": (
                "needs_investigation"
                if key == "refund_pending"
                else "action_required"
                if amount
                else "no_action"
            ),
            "refund_brl": amount,
            "recommended_action": action,
            "responsible_parties": [
                {
                    "party_type": party,
                    "party_id": "wrong-policy-seller" if party == "seller" else None,
                }
            ],
        }
    data = {
        "get_order": order,
        "get_order_items": [item],
        "get_order_payments": payments,
        "get_payment_timeline": {"order_id": "order-1", "payments": payments, "events": events},
        "get_refund_timeline": {"order_id": "order-1", "events": refunds},
        "get_shipment_summary": {
            "order_id": "order-1",
            "order_status": status,
            "delivered_carrier_at": order["order_delivered_carrier_date"],
            "delivered_customer_at": order["order_delivered_customer_date"],
            "estimated_delivery_at": order["order_estimated_delivery_date"],
            "events": [],
        },
        "get_sellers": [{"seller_id": "seller-1"}],
        "get_policy": {"policy_version": "TEST_POLICY", "currency": "BRL", "rules": rules},
    }
    return case, data


class FakeGateway:
    def __init__(self, data, *, fail=None):
        self.data = data
        self.fail = fail
        self.calls = []
        self.results = {}

    async def list_tools(self):
        return sorted(self.data)

    async def call(self, name, *, case_id, **arguments):
        self.calls.append((case_id, name, arguments))
        if name == self.fail:
            raise ToolCallError("Synthetic tool failure")
        result = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_offline_{case_id}_{name}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": TOOL_DOMAINS[name],
            "data": copy.deepcopy(self.data[name]),
        }
        self.results[name] = result
        return result


def run_case(tmp_path, case, data, *, fail=None):
    gateway = FakeGateway(data, fail=fail)
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts/schemas"))
    output = asyncio.run(solve_case(case, gateway, trace))
    return output, gateway, trace


@pytest.mark.parametrize(
    "issue",
    [
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
    ],
)
def test_business_decisions_and_trace(tmp_path, issue):
    case, data = fixture(issue)
    output, gateway, trace = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == issue
    trace.contracts.validate_output(output, "test")
    assert all(call[0] == case["case_id"] for call in gateway.calls)
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    assert {"task_assigned", "handoff", "verification_completed", "policy_decided"} <= {
        event["event_type"] for event in events
    }
    if issue in {"unavailable_order_paid", "late_delivery_seller"}:
        assert output["root_cause_analysis"]["responsible_parties"] == [
            {"party_type": "seller", "party_id": "seller-1"}
        ]


def test_customer_topic_does_not_override_evidence(tmp_path):
    case, data = fixture("canceled_order_paid")
    case["customer_request"]["claims"][0]["topic"] = "duplicate_charge"
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


@pytest.mark.parametrize(
    "failed_tool", ["get_policy", "get_payment_timeline", "get_refund_timeline"]
)
def test_tool_errors_require_investigation(tmp_path, failed_tool):
    case, data = fixture("refund_failed")
    output, gateway, _ = run_case(tmp_path, case, data, fail=failed_tool)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert set(output["evidence_refs"]) == {v["evidence_ref"] for v in gateway.results.values()}


def test_cross_order_response_is_rejected(tmp_path):
    case, data = fixture("canceled_order_paid")
    data["get_order"]["order_id"] = "another-order"
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["affected_entities"]["order_ids"] == []
    assert not any(ref.endswith("get_order") for ref in output["evidence_refs"])


def test_future_and_pre_purchase_events_do_not_change_decision(tmp_path):
    case, data = fixture("valid_split_payment")
    for date in ["2017-12-01T09:00:00-03:00", "2018-02-01T09:00:00-03:00"]:
        data["get_payment_timeline"]["events"].append(
            {
                "order_id": "order-1",
                "event_at": date,
                "event_type": "reconciliation_mismatch",
                "amount_brl": "9999",
                "status": "open",
            }
        )
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["data_conflicts"][0]["resolution_code"] == "OUTSIDE_CASE_TIMELINE"


def test_ambiguous_items_are_not_summed(tmp_path):
    case, data = fixture("unsupported_claim")
    item = copy.deepcopy(data["get_order_items"][0])
    item["price"] = "123.00"
    data["get_order_items"].append(item)
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


def test_refund_cannot_exceed_captured_amount(tmp_path):
    case, data = fixture("canceled_order_paid")
    data["get_policy"]["rules"]["canceled_order_paid"]["refund_brl"] = 1000
    output, _, _ = run_case(tmp_path, case, data)
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["assessment"]["case_status"] == "needs_investigation"


def test_verifier_rejects_foreign_refs_and_money_inconsistency(tmp_path):
    case, data = fixture("canceled_order_paid")
    output, gateway, trace = run_case(tmp_path, case, data)
    evidence = CaseEvidence(
        case["case_id"], "order-1", "TEST_POLICY", gateway, trace, records=gateway.results
    )
    altered = copy.deepcopy(output)
    altered["evidence_refs"].append("ev_foreign_case_reference_000000")
    with pytest.raises(ValueError, match="outside this case"):
        verify(altered, evidence)
    altered = copy.deepcopy(output)
    altered["financial_resolution"]["refund_lines"][0]["amount_brl"] = 99
    with pytest.raises(ValueError, match="do not sum"):
        verify(altered, evidence)


def test_two_cases_do_not_share_evidence(tmp_path):
    case, data = fixture("canceled_order_paid")
    first, _, _ = run_case(tmp_path, case, data)
    case["case_id"] = "TEST_CASE_002"
    second, _, _ = run_case(tmp_path, case, data)
    assert not set(first["evidence_refs"]) & set(second["evidence_refs"])


def test_canceled_order_refund_does_not_depend_on_disputed_item_details(tmp_path):
    case, data = fixture("canceled_order_paid")
    disputed = copy.deepcopy(data["get_order_items"][0])
    disputed["freight_value"] = "99.00"
    disputed["shipping_limit_date"] = "2018-01-10T09:00:00-03:00"
    data["get_order_items"].append(disputed)
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
    assert output["affected_entities"]["item_ids"] == ["item-1"]
    assert output["data_conflicts"][0]["resolution_code"] == "ITEM_DETAILS_NOT_USED"
    assert output["data_conflicts"][0]["selected_source"] is None


def test_disputed_seller_identity_still_requires_investigation(tmp_path):
    case, data = fixture("canceled_order_paid")
    disputed = copy.deepcopy(data["get_order_items"][0])
    disputed["seller_id"] = "different-seller"
    data["get_order_items"].append(disputed)
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["data_conflicts"][0]["resolution_code"] == "UNRESOLVED_ITEM_VERSIONS"


def test_payment_row_order_is_not_a_conflict(tmp_path):
    case, data = fixture("valid_split_payment")
    data["get_payment_timeline"]["payments"] = list(reversed(data["get_order_payments"]))
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "valid_split_payment"


def test_uncited_entity_evidence_is_rejected(tmp_path):
    case, data = fixture("canceled_order_paid")
    output, gateway, trace = run_case(tmp_path, case, data)
    output["evidence_refs"].remove(gateway.results["get_order_items"]["evidence_ref"])
    output["claim_assessments"] = []
    evidence = CaseEvidence(
        case["case_id"], "order-1", "TEST_POLICY", gateway, trace, records=gateway.results
    )
    with pytest.raises(ValueError, match="Ungrounded entity"):
        verify(output, evidence)


def test_unrepresentable_money_requires_investigation(tmp_path):
    case, data = fixture("canceled_order_paid")
    data["get_policy"]["rules"]["canceled_order_paid"]["refund_brl"] = "1e100"
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


@pytest.mark.parametrize("issue", ["refund_pending", "refund_failed"])
@pytest.mark.parametrize("different_seller", [False, True])
def test_refund_resolution_uses_shared_identity_only(tmp_path, issue, different_seller):
    case, data = fixture(issue)
    alternate = copy.deepcopy(data["get_order_items"][0])
    alternate["price"] = "9999.00"
    alternate["shipping_limit_date"] = "2018-01-04T09:00:00-03:00"
    if different_seller:
        alternate["seller_id"] = "another-seller"
    data["get_order_items"].append(alternate)
    output, _, _ = run_case(tmp_path, case, data)
    if different_seller:
        assert output["assessment"]["primary_issue"] == "insufficient_evidence"
        assert output["financial_resolution"]["recommended_refund_brl"] == 0
    else:
        assert output["assessment"]["primary_issue"] == issue
        assert output["financial_resolution"]["recommended_refund_brl"] == (
            100 if issue == "refund_failed" else 0
        )
        assert any(
            conflict["resolution_code"] == "ITEM_DETAILS_NOT_USED"
            for conflict in output["data_conflicts"]
        )


def test_claimed_refund_without_timeline_cannot_bypass_item_conflict(tmp_path):
    case, data = fixture("refund_pending")
    data["get_refund_timeline"]["events"] = []
    alternate = copy.deepcopy(data["get_order_items"][0])
    alternate["price"] = "9999.00"
    data["get_order_items"].append(alternate)
    output, _, _ = run_case(tmp_path, case, data)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
