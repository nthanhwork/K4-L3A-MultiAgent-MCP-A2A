"""Deterministic specialists: decisions use MCP facts, never the claimed topic."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .evidence import CaseEvidence

ZERO = Decimal("0.00")


class InvestigationError(ValueError):
    """A validation failure with public source conflicts to retain in the output."""

    def __init__(self, message: str, conflicts: list[dict]) -> None:
        super().__init__(message)
        self.conflicts = conflicts


def money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid monetary amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("Amount must be finite and nonnegative")
    try:
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Monetary amount exceeds supported precision") from exc


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result


def rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected a list of records")
    return value


def conflict(field: str, source: str, code: str, selected: str | None = "get_order") -> dict:
    return {
        "field": field,
        "sources": [source, "get_order"],
        "selected_source": selected,
        "resolution_code": code,
    }


def scoped_events(
    value: Any,
    start: datetime,
    end: datetime,
    source: str,
    conflicts: list[dict],
) -> list[dict[str, Any]]:
    original = rows(value)
    selected = [event for event in original if start <= timestamp(event.get("event_at")) <= end]
    if len(original) != len(selected):
        conflicts.append(conflict(f"{source}.event_at", source, "OUTSIDE_CASE_TIMELINE"))
    return sorted(selected, key=lambda event: timestamp(event["event_at"]))


def scoped_items(
    original: list[dict],
    start: datetime,
    end: datetime,
    conflicts: list[dict],
    *,
    identity_only: bool = False,
) -> list[dict]:
    by_id: dict[str, list[dict]] = {}
    for item in original:
        if not item.get("order_item_id") or not item.get("seller_id"):
            raise ValueError("Item identity is missing")
        by_id.setdefault(str(item["order_item_id"]), []).append(item)
    selected = []
    for variants in by_id.values():
        unique = [row for i, row in enumerate(variants) if row not in variants[:i]]
        if len(unique) > 1:
            unique = [
                row for row in unique if start <= timestamp(row.get("shipping_limit_date")) <= end
            ]
            if len(unique) != 1:
                identity_fields = ("order_id", "order_item_id", "seller_id")
                identities = {tuple(row.get(key) for key in identity_fields) for row in variants}
                if identity_only and len(identities) == 1:
                    # Preserve only shared identity. No disputed price, freight or deadline
                    # is selected or used to authorize the policy's order-level refund.
                    selected.append({key: variants[0][key] for key in identity_fields})
                    entry = conflict(
                        "items.details", "get_order_items", "ITEM_DETAILS_NOT_USED", selected=None
                    )
                    if entry not in conflicts:
                        conflicts.append(entry)
                    continue
                raise InvestigationError(
                    "Conflicting item versions cannot be resolved",
                    [
                        *conflicts,
                        conflict(
                            "items.details",
                            "get_order_items",
                            "UNRESOLVED_ITEM_VERSIONS",
                            selected=None,
                        ),
                    ],
                )
            entry = conflict("items.shipping_limit_date", "get_order_items", "ORDER_TIMELINE_MATCH")
            if entry not in conflicts:
                conflicts.append(entry)
        selected.extend(unique)
    return selected


def investigate(case: dict[str, Any], evidence: CaseEvidence) -> dict[str, Any]:
    required = (
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    )
    if any(name not in evidence.records for name in required):
        raise ValueError("Required evidence unavailable")
    order = evidence.data("get_order")
    shipment = evidence.data("get_shipment_summary")
    policy = evidence.data("get_policy")
    if not isinstance(policy.get("rules"), dict):
        raise ValueError("Policy rules must be an object")
    if policy.get("currency") != "BRL":
        raise ValueError("Unsupported policy currency")
    start, end = timestamp(order.get("order_purchase_timestamp")), timestamp(case.get("opened_at"))
    if start > end:
        raise ValueError("Order postdates the complaint")
    conflicts: list[dict] = []
    deadline = timestamp(order.get("order_estimated_delivery_date"))
    canceled = order.get("order_status") in {"canceled", "unavailable"}
    refund_events = []
    if "get_refund_timeline" in evidence.failures:
        raise ValueError("Refund history unavailable; absence is not established")
    if "get_refund_timeline" in evidence.records:
        refund_events = scoped_events(
            evidence.data("get_refund_timeline").get("events"),
            start,
            end,
            "get_refund_timeline",
            conflicts,
        )
    latest_refund = refund_events[-1] if refund_events else None
    order_level_resolution = canceled or (
        latest_refund is not None and latest_refund.get("status") in {"pending", "failed"}
    )
    items = scoped_items(
        rows(evidence.data("get_order_items")),
        start,
        max(end, deadline),
        conflicts,
        identity_only=order_level_resolution,
    )
    if not items:
        raise ValueError("No authoritative items")
    events = scoped_events(
        evidence.data("get_payment_timeline").get("events"),
        start,
        end,
        "get_payment_timeline",
        conflicts,
    )
    captures = [
        event
        for event in events
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"
    ]
    captured = sum((money(event.get("amount_brl")) for event in captures), ZERO)
    invoice = (
        ZERO
        if order_level_resolution
        else sum((money(row.get("price")) + money(row.get("freight_value")) for row in items), ZERO)
    )
    if not captures:
        raise ValueError("No confirmed payment evidence")
    payment_rows = rows(evidence.data("get_order_payments"))
    timeline_payments = rows(evidence.data("get_payment_timeline").get("payments"))
    # Row ordering is not a source conflict. Preserve multiplicity when comparing.
    if Counter(json.dumps(row, sort_keys=True) for row in payment_rows) != Counter(
        json.dumps(row, sort_keys=True) for row in timeline_payments
    ):
        raise ValueError("Payment sources disagree")
    shipment_events = scoped_events(
        shipment.get("events"), start, end, "get_shipment_summary", conflicts
    )
    for order_key, shipment_key in (
        ("order_status", "order_status"),
        ("order_delivered_carrier_date", "delivered_carrier_at"),
        ("order_delivered_customer_date", "delivered_customer_at"),
        ("order_estimated_delivery_date", "estimated_delivery_at"),
    ):
        if order.get(order_key) != shipment.get(shipment_key):
            raise ValueError("Order and shipment sources disagree")

    refunded = sum(
        (
            money(event.get("amount_brl"))
            for event in refund_events
            if event.get("status") in {"completed", "succeeded"}
        ),
        ZERO,
    )

    delivered_raw = order.get("order_delivered_customer_date")
    delivered = timestamp(delivered_raw) if delivered_raw else None
    late = (min(delivered, end) if delivered else end) > deadline
    issue = "unsupported_claim"
    confidence = 0.92
    late_sellers: list[str] = []
    if latest_refund and latest_refund.get("status") in {"failed", "pending"}:
        issue = "refund_" + latest_refund["status"]
    elif order.get("order_status") in {"canceled", "unavailable"}:
        issue = order["order_status"] + "_order_paid"
    elif late:
        handoff_raw = order.get("order_delivered_carrier_date")
        handoff = timestamp(handoff_raw) if handoff_raw else None
        for item in items:
            limit = timestamp(item.get("shipping_limit_date"))
            if (handoff is not None and min(handoff, end) > limit) or (
                handoff is None and end > limit
            ):
                late_sellers.append(str(item["seller_id"]))
        if late_sellers:
            issue = "late_delivery_seller"
        elif handoff and handoff <= end:
            issue = "late_delivery_logistics"
        else:
            raise ValueError("Cannot assign delivery responsibility")
        expected_actor = "seller" if late_sellers else "logistics_provider"
        if any(
            event.get("event_type") == "delivered_late"
            and event.get("status") == "confirmed"
            and event.get("actor") != expected_actor
            for event in shipment_events
        ):
            raise ValueError("Shipment events contradict handoff responsibility")
    elif any(
        event.get("event_type") == "reconciliation_mismatch"
        and event.get("status") in {"open", "confirmed"}
        for event in events
    ):
        issue = "payment_mismatch"
    elif any(
        event.get("event_type") == "duplicate_charge" and event.get("status") == "confirmed"
        for event in events
    ):
        issue = "duplicate_charge"
    elif (
        captured > invoice
        and len(captures) > 1
        and len({money(e["amount_brl"]) for e in captures}) == 1
    ):
        # An overpayment with repeated equal captures supports a suspected duplicate,
        # not certainty: explicit transaction IDs are absent in the public payload.
        issue, confidence = "duplicate_charge", 0.70
    elif captured == invoice and len(captures) > 1:
        issue = "valid_split_payment"
    elif captured != invoice:
        issue, confidence = "payment_mismatch", 0.80
    elif not delivered or delivered > end:
        raise ValueError("Delivery is not yet confirmed as of the complaint")

    rule = policy.get("rules", {}).get(issue)
    if not isinstance(rule, dict):
        raise ValueError("No policy rule for the established issue")
    refund = money(rule.get("refund_brl"))
    if refund > max(ZERO, captured - refunded):
        raise ValueError("Policy refund exceeds captured funds remaining")
    if (
        issue == "refund_failed"
        and latest_refund
        and refund > money(latest_refund.get("amount_brl"))
    ):
        raise ValueError("Policy retry exceeds the failed refund")
    parties = []
    seller_ids = sorted({str(row["seller_id"]) for row in items})
    for party in rows(rule.get("responsible_parties")):
        if party.get("party_type") == "seller":
            parties.extend(
                {"party_type": "seller", "party_id": seller}
                for seller in sorted(set(late_sellers or seller_ids))
            )
        else:
            parties.append(
                {"party_type": party.get("party_type"), "party_id": party.get("party_id")}
            )
    if len(conflicts) > 5:
        raise ValueError("Too many unresolved source conflicts")
    confidence = round(max(0.5, confidence - 0.04 * len(conflicts)), 2)
    relevant = [
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ]
    if refund_events:
        relevant.append("get_refund_timeline")
    if any(p["party_type"] == "seller" for p in parties) and "get_sellers" in evidence.records:
        valid_sellers = {row.get("seller_id") for row in rows(evidence.data("get_sellers"))}
        if not set(seller_ids) <= valid_sellers:
            raise ValueError("Seller registry does not support item ownership")
        relevant.append("get_sellers")
    refs = evidence.refs(*relevant)
    payment_counts = Counter(money(event["amount_brl"]) for event in captures)
    payment_refs = set()
    for row in payment_rows:
        amount = money(row.get("payment_value"))
        if payment_counts[amount] > 0:
            ref = row.get("payment_reference")
            if ref:
                payment_refs.add(str(ref))
            payment_counts[amount] -= 1
    shipment_ids = sorted({str(e["shipment_id"]) for e in shipment_events if e.get("shipment_id")})
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": evidence.case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": rule.get("case_status"),
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [evidence.order_id],
            "item_ids": sorted({str(row["order_item_id"]) for row in items}),
            "seller_ids": seller_ids,
            "payment_references": sorted(payment_refs),
            "shipment_ids": shipment_ids,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": (
                [
                    {
                        "reason_code": issue.upper(),
                        "amount_brl": float(refund),
                        "entity_id": evidence.order_id,
                    }
                ]
                if refund
                else []
            ),
        },
        "resolution_actions": [rule.get("recommended_action")],
    }
    claims = []
    for claim in case.get("customer_request", {}).get("claims", []):
        topic = claim.get("topic")
        verdict = "unsupported"
        if topic == issue:
            verdict = "supported"
        elif topic == "requested_full_refund" and refund:
            full = issue in {"canceled_order_paid", "unavailable_order_paid"} and refund == captured
            verdict = "supported" if full else "partially_supported"
        elif topic not in policy.get("rules", {}) and topic != "requested_full_refund":
            verdict = "insufficient_evidence"
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    output["claim_assessments"] = claims
    return output
