# K4 L3A — Multi-Agent MCP + A2A

## Mục tiêu

Xây dựng hệ thống multi-agent điều tra khiếu nại thương mại điện tử.

Agent phải:

- đọc yêu cầu của khách hàng;
- lấy dữ liệu có thẩm quyền qua MCP Evidence Gateway;
- phối hợp giữa các agent để đưa ra kết luận;
- tạo output và trace đúng public contract.

Customer message không phải ground truth. Không được tự đoán dữ liệu hoặc tạo `evidence_ref` giả.

## Dữ liệu

Tham khảo dữ liệu tại: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce

## Quy tắc đặt tên

Làm nhóm hoặc cá nhân, khi fork về các bạn giữ nguyên tên gốc repo, không đổi tên

## 1. Cài đặt

Yêu cầu Python 3.11 trở lên.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Kiểm tra:

```bash
pytest -q
day09 --help
```

Trên Windows PowerShell, có thể dùng trực tiếp executable trong venv (không cần đổi execution policy):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\day09.exe --help
.\.venv\Scripts\day09.exe mcp-tools
```

Nếu `.venv` chưa tồn tại, tạo bằng Python >=3.11 rồi chạy `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`. Chỉ sao chép `.env.example` khi chưa có `.env`; không ghi đè cấu hình/key đang dùng.

## 2. Đăng ký team

1. Mở `/register` trên Competition Workspace.
2. Điền tên team, mã học viên và các thành viên.
3. Nhập registration code của lớp.
4. Lưu Team API Key dạng `sk-team-...` được hiển thị sau khi đăng ký.

Điền thông tin thật vào `.env`:

```dotenv
COMPETITION_API_URL=http://127.0.0.1:8081
COMPETITION_TEAM_API_KEY=sk-team-your_key
MCP_ENDPOINT=http://127.0.0.1:8001/mcp
```

## 3. Tải input

Tải ZIP input **L3A** từ GitHub Release và giải nén vào root repo:

```bash
unzip l3a-inputs-<version>.zip -d .
day09 validate-inputs
```

Cấu trúc đúng:

```text
case-set.json
inputs/
├── L3A_CASE_001.json
├── ...
└── L3A_CASE_100.json
```

## 4. Sử dụng MCP

MCP Gateway cung cấp evidence về order, item, payment, shipment, seller và policy. Mọi call sẽ được server audit nên mọi người lưu ý config đúng để đảm bảo quyền lợi

Xem các tool hiện có:

```bash
day09 mcp-tools
```

Xem thêm mô tả và input schema thực tế của từng tool:

```bash
day09 mcp-tools --json
```

Ví dụ gọi tool trong `workflow.py`:

```python
evidence = await gateway.call(
    "get_order",
    case_id=case["case_id"],
    order_id=order_id,
)

evidence_ref = evidence["evidence_ref"]
order_data = evidence["data"]
```

Khi dùng evidence để đưa ra kết luận, ghi lại trong trace:

```python
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",
    evidence_refs=[evidence_ref],
)
```

Quy tắc quan trọng:

- luôn truyền đúng `case_id`;
- dùng tool discovery, không đoán tên tool;
- không sửa hoặc tự tạo `evidence_ref`;
- không dùng evidence chéo case;
- chỉ trích dẫn evidence thật sự hỗ trợ kết luận.

### Pha 3: chạy specialist để thu thập evidence

Đã triển khai Order/Item, Payment, Shipment và phần đọc policy của Policy Agent. Gateway discovery và validate arguments trước khi gọi; collector cố định scope của case, phân quyền theo actor, giữ nguyên evidence envelope, retry lỗi kết nối/timeout/429/5xx có giới hạn và chặn evidence sai domain/scope. Không retry lỗi tool nghiệp vụ không xác định hoặc lỗi schema/auth.

| Agent | Tool đã ánh xạ quyền sau discovery |
| --- | --- |
| `order-agent` | `get_order`, `get_order_items`, `get_sellers`, `get_product_context`, `get_customer_history` |
| `payment-agent` | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| `shipment-agent` | `get_shipment_summary` |
| `policy-agent` | `get_policy` |

Chỉ chọn tool cần thiết cho case. Customer/product context và refund timeline không được gọi tự động. Tool mới hoặc chưa discovery bị từ chối. Bảng ánh xạ tên không thay thế việc kiểm tra schema do server trả về.

Sau khi tải case-set chính thức, dùng các ID thật từ case để chạy. Các biến dưới đây do người chạy điền; không tự tạo case/order ID để thử gateway:

```powershell
.\.venv\Scripts\day09.exe collect-evidence --case-id $caseId --order-id $orderId --tool get_order --tool get_order_items --tool get_order_payments --tool get_shipment_summary
.\.venv\Scripts\day09.exe collect-evidence --case-id $caseId --policy-version $policyVersion --tool get_policy
```

Có thể lặp `--order-id` để tra nhiều order thuộc cùng case, hoặc dùng `--customer-unique-id` với `get_customer_history`. CLI kiểm tra `case_id` có trong case-set đã cài; lookup IDs được khai báo tường minh, quyền truy cập cuối cùng vẫn do server xác nhận. Không tự parse/đoán ID từ nội dung khách hàng.

Kết quả mỗi lần chạy lưu riêng tại `traces/collection_<id>/evidence.json` và `trace.jsonl`, không ghi đè output/trace nộp bài. Báo cáo này là dữ liệu nội bộ, không phải output L3A và không được thêm vào submission ZIP. Task thất bại được ghi mã lỗi và khiến CLI trả exit code 1. Lỗi auth/deadline toàn lượt dừng collection, trace đã ghi vẫn được giữ.

Mặc định specialist đọc toàn bộ `data` làm observation có nguồn, không tự suy diễn issue/refund. Có thể chỉ đọc field cụ thể bằng `--pointer` (JSON Pointer tương đối với `data`, ví dụ `/order_id` khi response có field đó); tùy chọn này áp dụng cho mọi tool trong lệnh. Field không tồn tại thì task thất bại, không ghi `tool_result_consumed`. Khi dùng API Python, mỗi task có thể chọn các pointer khác nhau:

```python
from student_agent.evidence import CaseScope
from student_agent.specialists import SpecialistTask
from student_agent.workflow import collect_case_evidence

collection = await collect_case_evidence(
    CaseScope(case_id=case["case_id"], order_ids=(order_id,)),
    gateway,
    trace,
    tasks=[
        SpecialistTask(
            task_id="order_lookup",
            actor="order-agent",
            tool_name="get_order",
            arguments={"order_id": order_id},
        ),
    ],
)
```

Caller của API quản lý `case_received`. Collector phát `task_assigned`, specialist đọc field thành công mới phát `tool_result_consumed`, sau đó `handoff` về Coordinator. Mỗi observation giữ tool, ref, hash, warning và field nguồn; downstream chỉ chọn refs thực sự hỗ trợ từng kết luận, không sao chép mọi ref đã tải vào output. Chính sách mặc định: tối đa 3 calls đồng thời, 30 giây/attempt, tối đa 3 attempts/read call, deadline collection 180 giây; hủy task còn chạy khi hết hạn.

`collect-evidence` chỉ kiểm tra phần đọc dữ liệu. `solve_case()` hiện đã nối Policy Engine và Verifier để tạo output L3A khi evidence và policy đúng định dạng. Bộ test dùng evidence giả lập chỉ trong thư mục `tests/`; không dùng các ref này khi chạy thật.

### Pha 4: policy, verifier và confidence

Policy Agent tính issue, trách nhiệm, hoàn tiền và hành động từ evidence cùng case và quy tắc máy đọc được của `get_policy`. Phép tính tiền dùng `Decimal` theo cent, trừ các khoản hoàn đã hoàn thành/đang chờ và kiểm tra liên kết payment–refund. Xung đột hai nguồn được ghi vào `data_conflicts`; chỉ ưu tiên một nguồn khi policy quy định rõ.

Verifier kiểm tra lại output theo public schema, consistency, entity/evidence scope và tính lại quyết định từ evidence. Confidence có trần 0.95; thiếu hoặc mâu thuẫn bằng chứng làm giảm điểm, trường hợp `insufficient_evidence` tối đa 0.45. Đây là heuristic thận trọng, chưa phải xác suất đã hiệu chuẩn bằng nhãn thực tế. Khi Verifier đạt, `solve_case()` phát `verification_completed/VERIFY_PASS`; CLI ghi output rồi mới phát `case_finalized`. Validation trước khi package cũng kiểm tra tiền, consistency và thứ tự lifecycle.

Xem [POLICY_ADAPTER.md](POLICY_ADAPTER.md) để biết chính xác các field nội bộ mà engine hiện đọc. `contracts/scoring/scoring-policy-v2.json` quy định **cách chấm**, không có luật trọng tài hoặc bảng hoàn tiền. Policy thật và input thật chưa có trong repo; trước khi chạy `day09 run` cần đối chiếu adapter với response có thẩm quyền, nếu không engine sẽ dừng với lỗi định dạng thay vì tự suy đoán.

## 5. Xây dựng multi-agent workflow

Triển khai tại:

```text
src/student_agent/workflow.py
```

Hàm chính:

```python
async def solve_case(case, gateway, trace) -> dict:
    ...
```

Gợi ý có thể tổ chức các vai trò:

- coordinator;
- order/item agent;
- payment agent;
- shipment agent;
- policy agent;
- verifier.

Competition không chấm tên framework hay số lượng class. Scorer đánh giá kết quả, evidence và sự phối hợp thể hiện trong trace.

Hoàn thiện mô tả thiết kế trong `ARCHITECTURE.md`.

## 6. Chạy và kiểm tra

```bash
day09 run
day09 validate
```

Kết quả được tạo tại:

```text
outputs/<case_id>.json
traces/trace.jsonl
```

Nếu output pass schema nhưng điểm thấp, cần kiểm tra lại semantic, evidence, consistency, confidence và workflow — schema chỉ là một phần nhỏ của điểm.

## 7. Đóng gói và nộp bài

```bash
day09 package --output dist/submission.zip
```

ZIP chỉ được chứa:

```text
manifest.json
trace.jsonl
outputs/<case_id>.json
```

Không đưa source, input, `.env`, API key hoặc debug log vào ZIP. Sau đó upload `dist/submission.zip` tại workspace `/l3a`

## Tiêu chí chấm điểm công khai

| Thành phần                                     | Trọng số |
| ---------------------------------------------- | -------: |
| Độ đúng nghiệp vụ (`semantic`)                 |      45% |
| Chất lượng bằng chứng (`evidence`)             |      15% |
| Evidence đúng MCP audit (`provenance`)         |      15% |
| Tính nhất quán giữa các field (`consistency`)  |      10% |
| Đúng JSON Schema (`schema`)                    |       5% |
| Confidence hợp lý (`calibration`)              |       5% |
| Quy trình multi-agent trong trace (`workflow`) |       5% |
| Hiệu quả gọi tool (`efficiency`)               |       0% |

L3A không cộng điểm efficiency trực tiếp, nhưng MCP calls vẫn được audit để kiểm tra tính hợp lệ.

Case có thể nhận 0 điểm nếu:

- sai `case_id` hoặc output không thể chấm theo schema;
- thiếu evidence bắt buộc;
- evidence ref không tồn tại;
- evidence thuộc team, run hoặc case khác.
