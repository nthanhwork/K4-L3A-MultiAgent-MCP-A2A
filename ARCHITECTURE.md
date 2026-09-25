# L3A Architecture Record

## 1. System overview

Bản triển khai đầu tiên sử dụng các agent theo vai trò, chạy bằng Python/asyncio và
quy tắc xác định. Không gọi LLM, không cần model API key. Quy tắc phân loại dựa trên
bằng chứng MCP; customer message và claim topic chỉ là đầu vào điều tra.

```text
Input → Coordinator ─┬→ Order/item agent ──→ get_order, get_order_items
                     ├→ Payment agent ─────→ payment rows + timeline [+ refund]
                     └→ Shipment agent ────→ shipment summary
                               ↓ AgentReport / evidence refs
                          Policy agent ────→ get_policy
                               ↓ quyết định [+ xác minh seller]
                            Verifier
                               ↓
                   outputs/<case_id>.json + traces/trace.jsonl
```

Điểm vào: `src/student_agent/workflow.py:solve_case`. `evidence.py` quản lý thu
thập và handoff; `reasoning.py` tổng hợp nghiệp vụ và áp dụng policy; `verifier.py`
kiểm tra độc lập output. CLI chịu trách nhiệm lifecycle nhận/hoàn tất case và ghi
output nguyên tử bằng file tạm rồi rename.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case, claimed order, policy version | Phân công, kiểm tra correlation, điều phối fallback | Task assignment, chuyển evidence tới policy/verifier |
| Order/item | Case/order ID | Lấy order/items; kiểm tra domain và entity scope; lấy seller khi quyết định quy trách nhiệm seller | AgentReport tới coordinator |
| Payment | Case/order ID, yêu cầu kiểm tra refund | Lấy payment rows, lifecycle; lấy refund timeline cho claim refund_pending/refund_failed | AgentReport tới coordinator |
| Shipment | Case/order ID | Lấy shipment summary và kiểm tra phạm vi | AgentReport tới coordinator |
| Policy | Evidence đã thu thập, policy version | Truy vấn policy, đối chiếu timeline, phân loại, tính phương án xử lý | policy_decided tới verifier |
| Verifier | Output đề xuất, evidence của case | Kiểm tra schema, linkage, entity, money và policy consistency | verification_completed hoặc DECISION_REJECTED |

Quyền tool được khai báo trong `OWNERSHIP`. Coordinator và verifier không có tool
nghiệp vụ riêng. Không truy vấn customer history/product context khi không phục vụ
kết luận. Tool phải được discovery trước khi gọi; tên/schema được cache trong một
session, response evidence không được cache giữa các case.

## 3. A2A protocol

A2A hiện là giao tiếp nội bộ tiến trình; chưa triển khai server/Agent Card hay giao
thức A2A qua mạng. Envelope `AgentReport` bất biến gồm `case_id`, `sender`, `target`,
`evidence_refs`, `failures`. Dữ liệu đầy đủ nằm trong kho evidence riêng của case.

- Coordinator phát `task_assigned`; specialist phát `tool_result_consumed` rồi
  `handoff`, kể cả khi có lỗi. Coordinator xác minh case/target của report.
- Ba specialist thu thập ban đầu chạy đồng thời; mỗi specialist gọi tool tuần tự.
  Khi `MCP_REQUEST_INTERVAL_SECONDS > 0`, gateway tuần tự hóa mọi tool call và
  chờ khoảng nghỉ đã cấu hình sau mỗi call. Đây là chế độ giảm tải server dùng
  chung; không khẳng định lỗi application từ server là do nghẽn tải.
- Policy chạy sau thu thập. Seller được xác minh thêm nếu có trách nhiệm seller.
- Verifier chạy cuối, được phép hạ kết quả xuống `insufficient_evidence`.
- Luồng là DAG hữu hạn, không có vòng hội thoại/replanning vô hạn.
- Mỗi tool call có timeout 45 giây; lỗi timeout/transport được retry một lần sau
  max(0,25 giây, khoảng nghỉ cấu hình), với cùng case và arguments. INTERNAL_ERROR
  hoặc REQUEST_TIMEOUT từ SDK MCP cũng được retry tối đa một lần, sau ít nhất hai
  giây; lỗi tham số và session không được retry ở tầng tool. Đây đều là truy vấn chỉ đọc. Mỗi lần retry
  vẫn có thể được server audit; không giả định audit chỉ ghi một lần.
- HTTP transport retry kết nối tối đa hai lần để tránh lỗi handshake làm hỏng
  background task của MCP SDK. CLI tự kết nối lại tối đa một lần, sau năm giây,
  trong cùng thư mục `dist/runs/<run_id>/`. Checkpoint giữ case hoàn tất; case có
  lỗi tool được truy vấn lại từ đầu, trace lỗi được cách ly trong `attempt-traces/`.
  `--resume-run` cho phép tiếp tục sau khi lệnh đã dừng, chỉ khi hash team key,
  endpoint, toàn bộ input và danh sách case khớp. Không ghép các thư mục run.
  Session transport không đồng nghĩa với run chấm thi; checkpoint không xác nhận
  server run ID (envelope không có trường này). Nếu ban tổ chức reset run chấm thi,
  phải chạy mới. MCP audit vẫn là nguồn xác nhận cuối cùng về quyền sở hữu refs.
  Chỉ công bố khi mọi case được chọn đều xử lý xong, có evidence và không còn lỗi tool.
- Trace chỉ ghi sự kiện, mã quyết định và lỗi validation quan sát được, không ghi
  prompt, suy luận riêng hoặc credentials.

## 4. Evidence lifecycle

1. MCP gateway xác thực bằng Team API Key từ `.env`, discovery tool và giải mã
   structured content (hoặc một text block JSON).
2. Validate evidence envelope theo contract. Specialist kiểm tra domain, hình
   dạng dữ liệu, mọi `order_id` xuất hiện trong response và policy version.
3. Lưu nguyên `evidence_ref`/`result_hash` trong kho của case. Không tự tạo reference.
   Hash hiện được validate định dạng; repo chưa có đặc tả canonicalization để tự
   tính lại hash. Quyền sở hữu team/run được xác nhận cuối cùng bởi MCP audit.
4. Phát `tool_result_consumed` khi specialist chấp nhận response. Evidence có scope
   sai hoặc không hợp lệ bị loại và không được trích dẫn.
5. Liên kết evidence với output và claim assessments. Verifier chỉ chấp nhận ref
   đã thu được trong case hiện tại. ID thanh toán/vận chuyển chỉ đưa ra khi nguồn
   có identifier tường minh; không suy diễn reference từ số thứ tự payment.

Quy ước thời gian của bản này: đánh giá tại `opened_at`, chỉ dùng event trong
khoảng từ `order_purchase_timestamp` đến `opened_at`. Event ngoài khoảng được ghi
`OUTSIDE_CASE_TIMELINE`. Các phiên bản item trùng ID chỉ được chọn nếu đúng một
phiên bản có shipping limit thuộc khoảng mua hàng đến max(opened_at, estimated
delivery). Nếu còn mơ hồ, kết quả cần điều tra và giữ conflict trong output.
Riêng order canceled/unavailable hoặc refund timeline có trạng thái pending/failed
trong khoảng đánh giá, nếu các phiên bản đồng nhất order/item/seller ID, chỉ dùng
định danh chung; không chọn price/freight/deadline đang tranh chấp. Nhánh này dựa
trên captured payment, refund timeline (nếu có) và policy, kèm conflict
ITEM_DETAILS_NOT_USED. Lời khai refund của khách không đủ để bỏ qua xung đột item.
Đây là giả định nghiệp vụ được
công khai, cần đối chiếu lại nếu đề bài yêu cầu đánh giá theo thời điểm khác.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/transport/SDK INTERNAL_ERROR hoặc REQUEST_TIMEOUT | Tối đa một lần | Thiếu evidence bắt buộc → cần điều tra | handoff / EVIDENCE_UNAVAILABLE |
| Tool thiếu, not found hoặc application error | Không | Không coi là dữ liệu rỗng hoặc bằng chứng phủ định | handoff / EVIDENCE_UNAVAILABLE |
| Sai schema/domain/order/policy version | Không | Loại response; cần điều tra nếu bắt buộc | handoff / EVIDENCE_UNAVAILABLE |
| Source conflict có thể giải quyết bằng timeline | Không | Ghi data_conflicts, giảm confidence | policy_decided + output data_conflicts |
| Source conflict không giải quyết được | Không | insufficient_evidence; refund 0 | handoff / ASSESSMENT_INCOMPLETE |
| Verifier từ chối | Không | Chuyển sang cần điều tra rồi validate lại | handoff / DECISION_REJECTED |
| Lỗi riêng của case ngoài fallback | Không | CLI tiếp tục case khác, kết thúc với exit code lỗi | FAILED trên stderr |
| Transport làm hỏng session MCP hoặc evidence tạm không khả dụng | Kết nối lại tối đa một lần mỗi lệnh | Giữ case hoàn tất cùng client run; chạy lại toàn bộ case lỗi | Thông báo reconnect trên stderr; attempt-traces lưu lần thử cũ |

`insufficient_evidence` là trạng thái thiếu dữ liệu thật, không phải câu trả lời
thay thế cho mọi case. Không bịa evidence để vượt hard gate; một case thiếu evidence
bắt buộc vẫn có thể bị bộ chấm cho 0 dù output đúng schema.

## 6. Verification invariants and business rules

- Case ID và output schema đúng contract; confidence thuộc [0, 1].
- Mọi cited ref có trong kho case; claim refs là tập con của output refs.
- Order/item/seller/payment/shipment IDs có trong bằng chứng nghiệp vụ được output
  trích dẫn; chỉ tồn tại trong kho evidence chưa đủ.
- Payment rows được đối chiếu theo nội dung và số lần xuất hiện, không phụ thuộc
  thứ tự dòng trả về.
- Seller chịu trách nhiệm phải thuộc affected sellers; seller ID trong policy
  dùng chung không được sao chép sang order khác. Xác minh qua item và get_sellers.
- Tổng refund lines bằng recommended refund; số tiền không âm và không vượt số
  captured còn lại sau refund completed/succeeded đã quan sát được.
- Status/action/refund phải khớp rule do MCP policy cung cấp. Dùng Decimal để tính
  tiền; chỉ chuyển sang số JSON ở bước xuất.
- Thiếu evidence → needs_investigation, refund 0, không gán trách nhiệm xác định.

Thứ tự phân loại: refund pending/failed đã xác minh → canceled/unavailable đã thu
tiền → giao trễ theo deadline/handoff → payment mismatch tường minh → duplicate
capture → split payment hợp lệ → sai lệch tổng tiền → unsupported claim. Đây là
thứ tự chọn primary issue; không khẳng định loại trừ mọi vấn đề phụ.

Duplicate charge không được suy ra chỉ từ hai payment rows giống nhau. Bản này
ưu tiên event duplicate_charge xác nhận; nếu chỉ có nhiều capture cùng số tiền và
tổng vượt invoice, đây là giả thuyết với confidence cơ sở 0,70. Cần thêm transaction
identity để xác nhận chắc chắn. Quyết định tài chính vẫn phải có policy hỗ trợ.

Confidence cơ sở 0,92, giảm cho suy luận thiếu định danh và mỗi xung đột timeline
đã giải quyết; đây là heuristic, chưa được hiệu chỉnh bằng dữ liệu chấm chính thức.
Refund timeline được truy vấn khi claim yêu cầu điều tra refund; chưa đối soát toàn
bộ lịch sử refund cho mọi case. Output chỉ là khuyến nghị, không thực hiện giao dịch.

## 7. Reproducibility

- Runtime: Python >= 3.11; môi trường thực nghiệm hiện tại Python 3.13.
- Dependencies: `pyproject.toml`, lockfile `uv.lock`; dùng `uv sync --extra dev --locked`.
- Không có LLM, model temperature hoặc random seed nghiệp vụ. Event IDs dùng
  randomness để duy nhất; timestamps/evidence refs phụ thuộc mỗi lần chạy MCP.
- Một case tại một thời điểm, tối đa ba MCP calls đang chạy. Không chia sẻ evidence
  giữa case. Không dùng CSV cục bộ thay cho MCP evidence được audit.
- `day09 run` tạo bộ riêng tại `dist/runs/<run_id>/`. Lỗi riêng của case, mất session,
  hoặc thiếu hoàn toàn evidence không ghi đè artifact đã công bố. Dừng sớm sau ba
  case liên tiếp không có evidence. Lỗi được báo sau khi đóng các task group MCP.
- Chỉ công bố lượt hoàn tất và mỗi case có evidence. Đây chưa phải kiểm tra mọi
  hard gate của scorer. `--case-id` cũng chạy riêng; smoke test thành công chỉ công
  bố một case, nên phải chạy toàn bộ trước đóng gói.
- Chỉ đóng gói manifest, trace và output. Không đưa input/source/key vào ZIP.

```bash
uv sync --extra dev --locked
uv run pytest -q
uv run ruff check .
uv run day09 validate-inputs
uv run day09 mcp-tools --json
uv run day09 run --case-id L3A_CASE_001
uv run day09 run
uv run day09 validate
uv run day09 package --output dist/submission.zip
```

Tests offline dùng dữ liệu tổng hợp và FakeGateway, không sử dụng API key. Fixtures
không được tái sử dụng cho submission. Test gồm các nhóm nghiệp vụ, lời khai sai,
evidence khác order/case, lỗi tool, xung đột item, event sai thời điểm, vượt hạn mức
refund, policy mapping và verifier. Gateway tests dùng chữ ký ClientSession thực tế
để kiểm tra discovery phân trang và retry. Điểm semantic/provenance chính thức chỉ
có thể xác nhận bằng bộ chấm Competition Workspace.

### Kiểm tra tích hợp ban đầu ngày 2026-09-25 (lịch sử)

- `pytest -q`: 36 tests đạt; `ruff check .` và `git diff --check` đạt.
- Đã kiểm tra 10 nhóm nghiệp vụ bằng MCP thật. Lượt chạy đầy đủ gần nhất tạo
  100 output và 2.093 trace event, qua schema validation và kiểm tra linkage,
  case ownership cục bộ, thứ tự receive/finalize.
- 69 case có kết luận nghiệp vụ; 31 case chuyển sang insufficient_evidence khi
  MCP bắt đầu trả application error từ case 070. Case 071–100 không lấy được
  evidence. Truy vấn lại get_order/get_policy cho case 001 từng thành công cũng
  trả `Error executing tool ...`; chưa xác định được nguyên nhân phía dịch vụ.
- Ở lượt chạy trước, case 071 còn có hai phiên bản item mâu thuẫn trong cùng
  khoảng thời gian; vẫn cần xử lý điều tra khi dịch vụ phục hồi.
- `dist/submission-draft.zip` là bản nháp để kiểm tra cấu trúc, chưa upload và chưa
  được chấm chính thức. Không dùng schema pass làm dấu hiệu đủ evidence để nộp.
- Sau khi MCP phục hồi: chạy lại toàn bộ `day09 run`, kiểm tra các dòng REVIEW và
  trace lỗi, rồi `day09 validate` và đóng gói lại. Không ghép refs từ các lượt cũ.


### Đánh giá lại và chạy lại trước khi thêm checkpoint (lịch sử)

Các điểm đã sửa sau rà soát:

- Không để một lượt chạy khi MCP lỗi ghi đè bộ kết quả cũ; lưu riêng từng lượt và
  dừng sớm khi ba case liên tiếp không có evidence.
- Xung đột chi tiết item không tự động chặn kết luận canceled/unavailable nếu
  định danh thống nhất và captured payment/policy đủ hỗ trợ. Không đoán giá hay
  phí vận chuyển đang tranh chấp. Xung đột không giải quyết được được giữ trong
  data_conflicts của fallback.
- Không coi thay đổi thứ tự payment rows là source conflict.
- Verifier từ chối entity chỉ xuất hiện trong evidence chưa được output trích dẫn.
- Số tiền vượt độ chính xác Decimal chuyển sang cần điều tra; không làm vỡ workflow.

Các giới hạn còn lại: A2A là nội bộ tiến trình; duplicate capture và confidence
vẫn có heuristic; refund timeline chưa được lấy cho mọi case; chưa có điểm chấm
chính thức. Test offline chứng minh các invariant và tình huống tổng hợp, không
chứng minh toàn bộ kết luận nghiệp vụ trên input thật.

Kết quả kiểm tra sau sửa: 43 tests đạt, Ruff và diff check đạt. Lượt thực thi
`dist/runs/2a32fcb1424248fe9426ee9d6c011eeb/` dừng có kiểm soát sau case
001–003: 18 tool calls trả application error, không có evidence. Discovery
vẫn trả 10 tool; get_order/get_policy cũng lỗi khi probe riêng. Chưa xác
định nguyên nhân phía dịch vụ hoặc quyền truy cập. Bộ 100 output và trace
trước đó được đối chiếu với ZIP nháp cũ và giữ nguyên; chưa có bộ kết quả
100 case mới hoặc điểm chấm mới. Cần MCP hoạt động trở lại để chạy đủ.


### Lượt chạy với checkpoint ngày 2026-09-25 (trạng thái mới nhất)

60 tests đạt; Ruff và diff check đạt. Checkpoint
`dist/runs/d0c050154b7f41a0ad161ddc3f1be404/` giữ 97 case hoàn tất. Ba case
098–100 vẫn chưa lấy đủ evidence sau reconnect và giãn truy vấn 1,5 giây;
lần thử bổ sung mới nhất có 18 lỗi tool: case 098 lấy được hai refs, case 099–100
không có ref. Bộ công bố trước đó được giữ nguyên, chưa tạo ZIP
mới đủ điều kiện nộp. Case 098 đã được sửa nhánh item/refund và kiểm thử offline,
nhưng chưa chạy xác minh lại trên MCP. Xem `SUBMISSION_REVIEW.md` để tiếp tục.


Theo yêu cầu nộp thử, đã đóng gói `dist/submission.zip` trực tiếp từ checkpoint
mới nhất: 100 output, 2.083 trace events, 102 file ZIP. Case 098–100 giữ nguyên
`insufficient_evidence`. Đã kiểm tra schema, cấu trúc ZIP và nội dung khớp checkpoint;
chưa upload. Bộ output/trace công bố cũ không bị thay thế.
