# L3A Architecture Record

## 1. System overview

```text
Input (case) → Coordinator → Specialists (Order, Policy, Shipment, Payment) → Verifier → Output
                                  │                                              │
                                  └─────────────── MCP Gateway ──────────────────┴── Trace
```

Luồng điều phối:
1. `cli.py` nhận case từ `inputs/<case_id>.json`, phát sự kiện `case_received` (actor: `coordinator`).
2. `coordinator` phân tích yêu cầu khách hàng (`customer_request`), trích xuất `order_id` và danh sách `claims`, phát sự kiện `task_assigned`.
3. Nhóm Specialist Agents truy vấn MCP Evidence Gateway với đúng `case_id` và `order_id`:
   - `policy_agent`: tra cứu chính sách qua `get_policy`.
   - `order_agent`: tra cứu đơn hàng qua `get_order`, danh sách hàng qua `get_order_items`.
   - `shipment_agent`: tra cứu tình trạng giao hàng qua `get_shipment_summary`, thông tin người bán qua `get_sellers`.
   - `payment_agent`: tra cứu thanh toán qua `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`.
4. Mỗi khi tiêu thụ bằng chứng từ MCP tool, agent phát sự kiện `tool_result_consumed` với `evidence_refs=[ref]`.
5. Specialists bàn giao dữ liệu cho `verifier` kèm sự kiện `handoff`.
6. `verifier` đối soát tính nhất quán, xác định `primary_issue`, kiểm tra logic các khiếu nại (`claim_assessments`), tính toán `financial_resolution`, phát sự kiện `verification_completed`.
7. `cli.py` ghi kết quả `outputs/<case_id>.json` và phát sự kiện `case_finalized`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| `coordinator` | `case` object | Phân tích khiếu nại, giao việc cho specialists | `task_assigned` event |
| `policy_agent` | `policy_version`, `case_id` | Tra cứu điều khoản bồi thường, trạng thái | Policy rules, `policy_ref` |
| `order_agent` | `order_id`, `case_id` | Gọi `get_order`, `get_order_items` | Trạng thái đơn, mã items |
| `shipment_agent` | `order_id`, `case_id` | Gọi `get_shipment_summary`, `get_sellers` | Timeline vận chuyển, sellers |
| `payment_agent` | `order_id`, `case_id` | Gọi `get_order_payments`, timeline, refund | Giao dịch, trạng thái hoàn tiền |
| `verifier` | Dữ liệu tổng hợp từ các agents | Kiểm tra invariants, tính toán bồi thường | Final case output |

## 3. A2A protocol

- Định dạng tương tác: Giao tiếp có định danh thông qua `case_id`, correlation theo từng case cụ thể.
- Lifecycle events: `case_received` -> `task_assigned` -> `tool_result_consumed` -> `handoff` -> `verification_completed` -> `case_finalized`.
- Không trace prompt riêng tư hoặc suy luận ẩn; chỉ trace observable events theo schema `day09-trace-event-v1`.

## 4. Evidence lifecycle

- Xác thực: Mọi response từ MCP Gateway đều được kiểm tra đúng schema `day09-mcp-evidence-v1`.
- Quản lý ref: `evidence_ref` được lưu trữ duy nhất theo case đang xử lý, không bao giờ dùng chéo giữa các case.
- Map vào output: Mỗi ref hợp lệ được liên kết vào `claim_assessments` và mảng `evidence_refs` của output.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có (1 lần) | Ghi nhận null cho tool đó | Log error |
| Not found / Tool error | Không | Coi như không có dữ liệu thực thể tương ứng (vd: chưa có refund) | Tiếp tục các tool khác |
| Source conflict | Không | Ưu tiên dữ liệu từ MCP Gateway thay vì customer message | `data_conflicts` |
| Invalid specialist result | Không | Verifier gán verdict `insufficient_evidence` | `verification_completed` |

## 6. Verification invariants

- Schema: Tuân thủ nghiêm ngặt `day09-l3a-output-v2`.
- Entities: `order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids` không vượt quá giới hạn schema và loại bỏ trùng lặp.
- Consistency:
  - Nếu `case_status == "no_action"`, `recommended_refund_brl == 0.0`, `refund_lines == []`.
  - Nếu `case_status == "action_required"`, số tiền hoàn khớp với quy định trong policy.
  - Seller responsibility: Nếu bên chịu trách nhiệm là `seller`, `party_id` được gán theo `seller_id` thực tế của đơn hàng.
- Confidence: Được hiệu chuẩn ở mức `0.95`.

## 7. Reproducibility

- Python >= 3.11
- Thư viện: `mcp>=2,<3`, `httpx2>=2,<3`, `jsonschema>=4.25,<5`, `python-dotenv>=1.1,<2`, `pytest>=8.4`.
- Lệnh chạy: `day09 run`
- Lệnh kiểm tra: `day09 validate`
- Lệnh đóng gói: `day09 package --output dist/submission.zip`
