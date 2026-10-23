# invoice-module — quy tắc cho agent

Package Python `invoice-module`: xuất hoá đơn VAT qua SePay eInvoice cho mỗi `PaymentSettled` của `payment-module`. Lý do thiết kế nằm ở `plans/reports/advise-260923-1226-vat-einvoice-module.md`; trạng thái tiếp nối ở `plans/handoffs/` mới nhất. Đọc hai chỗ đó trước khi lập plan hay code.

## Không được đảo (quyết định người dùng đã chốt)

- Không sửa `/Users/trieuphan/source_code/payment_modules/payment_modules_v1` (chỉ đọc). Cần móc nối mới ở payment-module thì đề xuất bản 0.1.1 riêng, có review, hỏi người dùng trước.
- Tiêu thụ `PaymentSettled` qua outbox consumer, không qua `SettlementHandler` (handler cấm gọi mạng, exception rollback settlement).
- Mọi settlement đều có hoá đơn, trừ settlement trùng đơn mà operator waive (xem dòng dưới); thiếu người mua dùng chuỗi "người mua không lấy hoá đơn" từ cấu hình. Tổng hoá đơn gốc = `amount_vnd` đã settle.
- `reference_code` gốc = `settlement_id`, ghi DB trước mọi lần gọi mạng; 409 thì tra theo reference, không đổi `reference_code` để thử lại.
- Settlement thứ hai cho cùng đơn: không tự xuất; giữ ở `blocked` và mở review `DUPLICATE_SETTLEMENT`, operator chọn retry (xuất) hoặc waive (hoàn tiền/cấn trừ). Người dùng đổi ngày 07/10/2026 theo ý kế toán.
- Hai chế độ xuất theo draft của host: `auto` phát hành ngay khi tiền về; `manual` giữ nháp trong module khi tiền về (không gọi SePay), kế toán duyệt qua use case/CLI (ghi actor), duyệt xong module mới tạo và phát hành một lần; ngày lập là ngày gửi (ghi cả ngày duyệt để đối soát). Người dùng đổi ngày 20/10/2026 sau khi sandbox cho thấy SePay giữ ngày nháp và bắt ngày lập không giảm theo ký hiệu. Nháp sai thì operator giữ lại sang review; draft sửa phải duyệt lại. Người dùng chốt ngày 07/10/2026.
- Module không tự bịa dòng hàng hay thuế suất. Chưa có draft thì ở `needs_draft`.
- Tiền: `Decimal`, không dùng `float`.
- Bảng SQLAlchemy Core prefix `inv_`, không FK sang `pm_*`.
- Các câu hỏi kế toán ở mục 9 của bản đúc kết là gate trước P3: không tự chọn thuế suất, mẫu số, ký hiệu hay ngày lập hoá đơn.

Muốn đổi một dòng ở trên: trình bày quyết định gốc, lý do, đánh đổi, rồi chờ người dùng.

## SePay và secret

- Chỉ gọi sandbox `https://einvoice-api-sandbox.sepay.vn`. Không gọi endpoint Live.
- Credential chỉ nằm trong file env ngoài repo, mode 600 (ví dụ `~/.config/invoice-module/sepay-einvoice-sandbox.env`). Không đọc giá trị, không in, không dán vào chat, orca, log, fixture hay commit. Code nạp secret qua `credential_ref` kiểu `env:NAME`.
- Fixture từ sandbox phải xoá PII thật trước khi lưu vào `tests/fixtures/`.

## Test

- Test ở tầng thấp nhất chứng minh được bất biến. Hành vi SePay (idempotency, poll, 409) phải có bằng chứng từ sandbox, không chỉ mock.
- Không xoá test, nới assertion hay skip để có CI xanh. Test lỗi: tìm nguyên nhân, chạy lại test đó, rồi chạy các test lân cận.

## Git

- Conventional commit. Không trailer `Co-Authored-By`, không nhắc AI. Author là tài khoản GitHub của người dùng.
- Không commit `plans/`, `.claude/`, `.agentkit/`, `*.env`, `.evidence/`.
- `git push` và `git tag` bị chặn cho agent; người dùng tự push và release.

## Vai trò

| Vai trò | Ai | Được sửa | Trách nhiệm |
|---|---|---|---|
| Người dùng | chủ dự án | mọi thứ | Chốt quyết định, cấp credential, chuyển câu hỏi kế toán, xử lý review case (SLO 1 ngày làm việc), push và release. |
| Coordinator | phiên Claude chính | `plans/`, file cấu hình repo, merge nhánh phase | Lập plan, chia file ownership, dispatch worker qua orca (run riêng cho module này), gom kết quả, hỏi người dùng. Không tự code phase đã giao cho worker. |
| Worker | Claude qua orca, model `claude-opus-5-5` effort `high` (người dùng chốt 07/10/2026; trước đó là codex `gpt-6-luna`) | chỉ các file được giao trong phase, trong worktree riêng | Thực thi plan: code và test một phase, ghi report phase vào `plans/reports/`. Không merge, không sửa file ngoài ownership, không sửa plan. Copy `.claude/settings.json` vào worktree; brief vẫn phải ghi lại đủ các ràng buộc ở mục "SePay và secret" và "Git". |
| Reviewer | agent Claude `code-reviewer` độc lập, không phải worker vừa code | không sửa code | Review khắt khe theo plan: chạy lại mọi Verify, đối chiếu từng dòng với spec và ý định của spec, thử làm hỏng code để chứng minh test bắt được lỗi, kiểm bất biến trên Postgres thật, kiểm hard rule. Trả PASS hoặc CHANGES_REQUESTED kèm bằng chứng. Bắt buộc sau mỗi phase và sau mỗi vòng sửa. |
| Kongming gate | agent `kongming` | không sửa code | Tư vấn go/no-go sau reviewer ở mỗi phase và khi bế tắc. |
| Review trước release | agent Claude `code-reviewer` độc lập rồi `kongming` go/no-go (người dùng chốt 23/10/2026; trước đó là codex) | không sửa gì | Review khắt khe toàn nhánh trước release hoặc khi người dùng yêu cầu. Không dùng codex. |

Một phase chỉ xong khi reviewer PASS, kongming không chặn, và coordinator đã đối chiếu với tiêu chí chấp nhận trong plan.
