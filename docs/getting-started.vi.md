# Bắt đầu

[English](getting-started.md) · **Tiếng Việt**

Tài liệu này đưa một host đang dùng `payment-module` tới chỗ có đúng một hoá đơn VAT cho mỗi
settlement: cài `invoice-module`, tạo bảng, đăng ký tài khoản xuất hoá đơn mà kế toán đã lập
trên SePay eInvoice, nộp bản nháp hoá đơn ngay khi tạo payment intent, và chạy worker phát hành
hoá đơn khi tiền về. Nửa sau là runbook cho operator: hàng đợi review, huỷ hoá đơn, tệp hoá
đơn, cảnh báo và duyệt của kế toán.

Host mẫu [`examples/invoice_host`](../examples/invoice_host) chạy trọn luồng trên PostgreSQL
với provider giả và gắn router webhook của payment-module sau một listener HTTP thật.

Phần của module và phần của host: module ghi nhận mọi `PaymentSettled`, đóng băng bản nháp host
đã nộp và phát hành đúng một hoá đơn gốc cho nó trên SePay eInvoice. Host giữ sản phẩm, giá,
thuế suất, mã hàng và thông tin người mua; kế toán giữ cấu hình xuất hoá đơn (mẫu số, ký hiệu,
hình thức thanh toán, người mua mặc định, chính sách ngày lập khi qua ngày). Module không bao
giờ tự bịa dòng hàng, thuế suất hay cấu hình.

Căn cứ pháp lý tài liệu này dẫn: Nghị định 254/2026/NĐ-CP và Thông tư 91/2026/TT-BTC, thay
Nghị định 123/2020/NĐ-CP và Nghị định 70/2025/NĐ-CP từ 01/07/2026.

## 1. Cài đặt

Cài `payment-module` trước (từ GitHub Release của nó), rồi `invoice-module` với các extra host
cần:

```sh
uv pip install "payment-module[sqlalchemy,postgres,sepay,fastapi] @ https://github.com/phanhaitrieu37/payment-modules/releases/download/v0.1.0/payment_module-0.1.0-py3-none-any.whl"
uv pip install "invoice-module[sqlalchemy,postgres,sepay,payment]"
```

Các extra: `sqlalchemy` (bảng, repository, unit of work, Alembic), `postgres` (asyncpg),
`sepay` (adapter SePay eInvoice, httpx), `payment` (consumer outbox cho `PaymentSettled`). Cần
Python 3.12 trở lên và PostgreSQL (schema dùng partial unique index, `JSONB` và `SKIP LOCKED`).

## 2. Tạo schema

[`define_tables`](../src/invoice_module/adapters/sqlalchemy/tables.py) khai báo năm bảng `inv_`
(tài khoản xuất hoá đơn, bản nháp, biên nhận settlement, document, review case); không bảng nào
có khoá ngoại sang bảng `pm_`. Migration đóng băng
[`schema_v1.upgrade`](../src/invoice_module/adapters/sqlalchemy/migrations/schema_v1.py) tạo các
bảng đó. Không có gì chạy khi import: host tự chạy migration, sau khi sao lưu.

```python
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from invoice_module.adapters.sqlalchemy.migrations import schema_v1
from invoice_module.adapters.sqlalchemy.tables import define_tables

INVOICE_TABLES = define_tables(sa.MetaData())  # the inv_* tables, created only by schema_v1


def migrate(sync_conn: sa.Connection) -> None:
    """In an Alembic revision, call schema_v1.upgrade(op) with Alembic's op instead."""
    schema_v1.upgrade(Operations(MigrationContext.configure(sync_conn)))
```

`schema_v1` không bao giờ đổi; schema sau sẽ là một module mới kèm bước nâng cấp.

## 3. Thông tin xác thực

Tài khoản xuất hoá đơn chỉ lưu một tham chiếu như `env:SEPAY_EINVOICE`.
[`EnvCredentialResolver`](../src/invoice_module/secrets/env_credential_resolver.py) đọc
`SEPAY_EINVOICE_CLIENT_ID` và `SEPAY_EINVOICE_CLIENT_SECRET` (tên tham chiếu cộng `_CLIENT_ID`
và `_CLIENT_SECRET`). Biến chưa đặt hoặc rỗng gây lỗi cấu hình nêu tên biến, không bao giờ nêu
giá trị.

- Giữ giá trị trong file env ngoài repo, mode `600`, do trình quản lý tiến trình nạp. Không
  commit, không in, không dán vào log, ticket hay chat.
- Sandbox và production có credential riêng; token sandbox gửi tới production nhận `401` (docs
  `xu-ly-loi`: "Token chỉ dùng được cho môi trường đã cấp nó").
- Muốn đọc từ kho secret riêng, hiện thực port
  [`CredentialResolver`](../src/invoice_module/ports/credentials.py).

## 4. Dựng module

[`build_invoice_module`](../src/invoice_module/builder.py) nối mọi use case một lần; giữ một
`InvoiceModule` mỗi tiến trình. Có provider và credential thì module dựng thêm use case của
worker, onboarding và hàng đợi review; không có thì chỉ dựng phần chạy được trên DB (nộp nháp,
nhận settlement, duyệt, giữ nháp, truy vấn), đúng phần CLI duyệt dùng.

```python
import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWorkFactory
from invoice_module.adapters.storage import FileArtifactStore
from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.builder import InvoiceModule, build_invoice_module
from invoice_module.ports.clock import SystemClock
from invoice_module.secrets import EnvCredentialResolver

ARTIFACT_TIMEOUT_S = 90.0  # one value for the client and the module config


def build_invoice(engine: AsyncEngine, http: httpx.AsyncClient, artifact_root) -> InvoiceModule:
    clock, credentials = SystemClock(), EnvCredentialResolver()
    client = SePayEInvoiceClient(
        http, credentials, clock, ProcessRateLimiter(), artifact_timeout_s=ARTIFACT_TIMEOUT_S
    )
    return build_invoice_module(
        SqlAlchemyInvoiceUnitOfWorkFactory(
            async_sessionmaker(engine, expire_on_commit=False), INVOICE_TABLES
        ),
        clock,
        provider=SePayEInvoiceProvider(client),
        credentials=credentials,
        artifact_store=FileArtifactStore(artifact_root),
        config=InvoiceModuleConfig(artifact_timeout_s=ARTIFACT_TIMEOUT_S),
    )
```

- Client luôn dùng sandbox `https://einvoice-api-sandbox.sepay.vn` cho tài khoản `test` và từ
  chối mọi URL khác cho chúng. Tài khoản `live` chỉ chạy khi host truyền URL production (docs
  `tong-quan`) vào `base_urls`; thiếu thì mọi lời gọi cho tài khoản live ném
  `ProviderConfigError` trước khi gửi bất cứ gì.
- Truyền cùng một `artifact_timeout_s` cho `SePayEInvoiceClient` và `InvoiceModuleConfig`.
  Lease tải tệp là `2 × artifact_timeout_s + 30` giây (mặc định 210 giây); timeout của client
  dài hơn giá trị của module để worker khác giành document giữa lúc đang tải. Sandbox từng mất
  khoảng 38 giây cho một lần tải.
- Dùng use case của module (`module.discard_draft`, `module.submit_draft`, ...) thay vì tự tạo:
  constructor như `DiscardDraft(uow_factory, clock, metrics)` có thể đổi giữa các bản minor.

## 5. Đăng ký tài khoản xuất hoá đơn

Một tài khoản xuất hoá đơn cho mỗi merchant × môi trường. Kế toán cấp mọi cấu hình: mẫu số,
mẫu ký hiệu (ví dụ `C{YY}...`, `{YY}` là hai chữ số năm lập), hình thức thanh toán, tên và địa
chỉ người mua mặc định (địa chỉ được để chuỗi rỗng), chính sách khi qua ngày (`issue` hoặc
`review`). Module không có giá trị mặc định cho cấu hình nào.

```python
from invoice_module.application.issuer_accounts import RegisterIssuerAccountCommand
from invoice_module.domain.enums import Environment, IssuerAccountStatus


async def onboard_issuer(invoice, merchant_id, accounting: dict, provider_account_id: str):
    """``accounting`` is the accountant's configuration (same keys as the command)."""
    account = await invoice.register_issuer_account.execute(
        RegisterIssuerAccountCommand(
            tenant_id=TENANT,
            merchant_id=merchant_id,
            environment=Environment.TEST,
            provider_account_id=provider_account_id,
            template_code=accounting["template_code"],
            invoice_series_template=accounting["invoice_series_template"],
            payment_method=accounting["payment_method"],
            credential_ref="env:SEPAY_EINVOICE",
            buyer_fallback_name=accounting["buyer_fallback_name"],
            buyer_fallback_address=accounting["buyer_fallback_address"],
            issue_date_cross_day_action=accounting["issue_date_cross_day_action"],
        ),
        actor=ADMIN,
    )
    return await invoice.set_issuer_account_status.execute(
        account.id, IssuerAccountStatus.ACTIVE, ADMIN, "accountant settings recorded"
    )
```

- `register_issuer_account` kiểm cấu hình trước mọi lời gọi provider (`payment_method` phải là
  `TM`, `CK`, `TM/CK` hoặc `KHAC`, enum docs của `CreateInvoiceRequest.payment_method`;
  `credential_ref` phải dạng `env:NAME`; tên người mua mặc định không được trắng), rồi gọi API
  chi tiết tài khoản provider và từ chối bằng `IssuerAccountMismatch` khi provider không biết
  tài khoản, mẫu số hay ký hiệu của năm nay (docs `xuat-hoa-don-dien-tu`: phần `C{YY}` "phải
  khớp năm hiện tại"). Tài khoản được lưu ở `draft`. Gọi lại cùng cấu hình trả tài khoản đã lưu;
  cấu hình khác cho cùng merchant và môi trường ném `IssuerAccountConflict`.
- `set_issuer_account_status` ghi actor và lý do. `active` cần chính sách qua ngày, tài khoản
  `live` cần thêm bằng chứng readiness; `disabled` khiến settlement mới không tìm thấy tài khoản
  nên mở `ISSUER_ACCOUNT_UNAVAILABLE` (mục 9). Khi SePay báo tài khoản không active: tắt tài
  khoản, sửa trên SePay, chạy lại readiness, bật lại.
- **Readiness (chỉ live).** Trước khi bật tài khoản `live`, ghi file bằng chứng sandbox của lượt
  chạy kịch bản bằng `record_issuer_readiness.execute(account_id, evidence_path, actor)`. Lệnh
  từ chối (`ReadinessRejected`) khi provider không báo tài khoản `active: true` (docs
  `danh-sach-tai-khoan`: "Chỉ sử dụng tài khoản có active: true"), khi thiếu file, khi
  `environment` của file khác `test`, hoặc khi s1, s2, s4 không PASS, hay s3 không PASS mà cũng
  không phải `INCONCLUSIVE` với `details.s3_variant` là `not_triggerable` hoặc `sync_reject`.
  Lệnh cảnh báo nhưng không chặn khi `tax_authority_approved_date` của tài khoản live sau hôm
  nay (`before_tax_approval_date`; docs: `422 INVOICE_DATE_BEFORE_TAX_APPROVAL` chỉ kiểm ở
  production) hoặc chưa có (`approval_date_unknown`).

## 6. Nộp bản nháp cùng CreateIntent

Host nộp bản nháp hoá đơn trong cùng transaction tạo payment intent, nên không có intent nào
thiếu bản nháp. Cả hai module dùng chung session của host:

```python
from datetime import timedelta
from decimal import Decimal

from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.application.create_intent import CreateIntentCommand

from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWork
from invoice_module.application.submit_draft import SubmitDraftCommand, build_line_inputs
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.money import compute_total


async def place_order(payment, invoice, sessions, order_id, merchant_id, account_id, mode):
    lines = (
        {
            "name": "Photo editing, monthly plan",
            "unit": "month",
            "quantity": Decimal("1"),
            "unit_price_vnd": 100_000,
            "tax_rate": 10,  # from the product's accounting data, never guessed
            "item_code": "PLAN-MONTHLY",  # the product's real item code
        },
    )
    amount_vnd = compute_total(build_line_inputs(lines))  # the bill amount, tax included
    async with sessions() as session, session.begin():
        created = await payment.create_intent.execute(
            CreateIntentCommand(
                TENANT,
                amount_vnd=amount_vnd,
                host_ref_type="order",
                host_ref_id=order_id,
                idempotency_key=order_id,
                expires_at=payment.clock.now() + timedelta(minutes=30),
                prefix_name="bill",
                merchant_id=merchant_id,
                receiving_account_id=account_id,
            ),
            uow=SqlAlchemyUnitOfWork.joined(session, PAYMENT_TABLES),
        )
        await invoice.submit_draft.execute(
            SubmitDraftCommand(
                TENANT,
                merchant_id,
                "order",
                order_id,
                created.intent.id,
                lines,
                amount_vnd,
                buyer=None,  # no buyer details: the accountant's fallback buyer is used
                delivery_mode=mode,  # DeliveryMode.AUTO or DeliveryMode.MANUAL, per order
            ),
            uow=SqlAlchemyInvoiceUnitOfWork.joined(session, INVOICE_TABLES),
        )
    return created
```

**Chế độ xuất.** Mỗi bản nháp mang `delivery_mode`; module không có giá trị mặc định. `auto`
phát hành hoá đơn ngay khi tiền về. `manual` giữ nháp trong module khi tiền về (không gửi gì
tới SePay); kế toán duyệt (mục 14), rồi module mới tạo và phát hành một bước. Host chọn theo
từng đơn.

**Luật gắn nháp.** Một bản nháp cho mỗi `(tenant, host_ref)`. Nộp lại cùng intent thì cập nhật
bản nháp đang mở và tăng revision. Lần checkout thứ hai cho cùng đơn (intent mới) chỉ được nhận
bản nháp đang mở khi intent đầu chưa có document đã settle; sau đó `SubmitDraft` ném
`DraftBoundToOtherIntent`. Bản nháp đã bị settlement dùng chỉ được mở lại cho intent mới (đường
thu trùng, mục 9). Bỏ bản nháp đang mở bằng `module.discard_draft.execute(tenant_id,
host_ref_type, host_ref_id, actor)`.

**Tổng tiền.** `declared_total_vnd` phải bằng tổng các dòng do module tính (không thì
`TotalMismatch`), và số tiền settle phải bằng tổng bản nháp; lệch thì mở `AMOUNT_MISMATCH`.
Tiền là số nguyên VND tính bằng `Decimal`, làm tròn half-up từng dòng; `tax_amount_vnd` host
đưa được lệch tối đa 1 VND mỗi dòng so với tiền thuế tính ra. `float` ở bất cứ đâu ném
`TypeError`.

**Hợp đồng host (docs SePay `xuat-hoa-don-dien-tu`, schema `CreateInvoiceRequest`).** Mọi điều
kiện dưới đây mà module không tự kiểm sẽ bị SePay trả `400 VALIDATION_ERROR`, mở
`PROVIDER_REJECTED`.

| Trường | Hợp đồng | Nguồn |
|---|---|---|
| loại dòng | v0.1 chỉ gửi dòng hàng hoá, `line_type = 1`; không dòng khuyến mãi, chiết khấu thương mại hay ghi chú | enum `InvoiceItem.line_type` 1–4 |
| `item_code` | bắt buộc ở mọi dòng: module từ chối dòng thiếu mã (`InvalidLine(index, "item_code")`) trước mọi thao tác DB. Docs ghi tuỳ chọn; sandbox trả `400 VALIDATION_ERROR` khi thiếu. Dùng mã hàng thật của sản phẩm trước cổng Live | `InvoiceItem.item_code` |
| `quantity` | dương, tối đa 2 chữ số thập phân ("tối đa 2 chữ số sau dấu phẩy"). Số lượng thập phân: chỉ `1.5` đã được chứng minh trên sandbox (phát hành ngày 23/10/2026, đọc lại là `"1.50"`, tổng chính xác); giá trị thập phân khác chưa kiểm chứng trên sandbox | `InvoiceItem.quantity` |
| `discount_vnd` | giá trị lớn hơn 0 chưa kiểm chứng trên sandbox; module cũng gửi `before_discount_and_tax_amount` trên dòng hàng hoá (docs mô tả trường này "dùng cho line_type=3") | `InvoiceItem.discount_amount` |
| `tax_rate` | một trong `-2`, `-1`, `0`, `5`, `8`, `10`; module gửi `tax_amount` đúng như đã tính ("dùng đúng giá trị bạn gửi, KHÔNG tính lại") | `InvoiceItem.tax_rate` |
| `notes` | tối đa 255 ký tự (giới hạn ghi ở trang điều chỉnh và thay thế; trang tạo không ghi) | `notes` |
| người mua | `national_id` tối đa 12 ký tự, `email` đúng định dạng; module không gửi `buyer_code` (docs: tối đa 100). Có mã số thuế thì cần tên và địa chỉ | `Buyer` |
| `payment_method` | `TM`, `CK`, `TM/CK` hoặc `KHAC`, kiểm lúc đăng ký tài khoản | enum `payment_method` |

## 7. Nối outbox

Module chỉ biết về thanh toán qua outbox của payment-module, không bao giờ trong transaction
settlement. Dựng invoice module trước, rồi truyền một
[`FanOutPublisher`](../src/invoice_module/integrations/payment_module/fanout.py) vào
`build_payment_module`:

```python
from payment_module.builder import build_payment_module

from invoice_module.integrations.payment_module import FanOutPublisher, PaymentSettledConsumer


def build_payment(engine, invoice, host_consumer):
    publisher = FanOutPublisher(
        [host_consumer, PaymentSettledConsumer(invoice.record_settled_payment)]
    )
    return build_payment_module(..., outbox_publisher=publisher)
```

Các consumer chạy tuần tự, lần lượt, cho từng event. Outbox giao ít nhất một lần và gửi lại cả
event khi một consumer ném lỗi, nên mọi consumer phải idempotent theo `event_id` (consumer hoá
đơn lưu biên nhận khoá theo `event_id`; giao lại không làm gì). Consumer hoá đơn chỉ ghi biên
nhận và document rồi commit; mọi lời gọi mạng nằm trong worker.

## 8. Chạy worker

```python
import asyncio

from invoice_module.runtime import InvoiceWorker


async def run(invoice, stop: asyncio.Event) -> None:
    await InvoiceWorker(invoice).run(stop)  # returns within one interval after stop.set()
```

Worker chạy bốn vòng lặp: phát hành, poll, sweep quá hạn và tải tệp. Vòng lặp lỗi được ghi log
(`invoice_worker_loop_failed`) và chạy tiếp; lease và trạng thái thử lại nằm trong DB. Nhiều
tiến trình worker có thể chạy trên cùng DB.

- Vòng phát hành đóng băng request ngay trước khi gửi, lấy ngày Asia/Saigon của thời điểm đó
  làm ngày lập, và ghi mọi POST trước khi nó rời đi. `reference_code` của hoá đơn gốc là
  `settlement_id`, ghi trước mọi lời gọi; nó không bao giờ tự đổi.
- Vòng poll kiểm job (3, 5, 10, 20, 30 giây, rồi mỗi 60 giây, hoặc `next_retry_at` của provider)
  tới khi `Success` hay `Failed`; quá `pending_timeout` (3 giờ, dài hơn cửa sổ 2 giờ của docs)
  thì document mở `PROVIDER_TIMEOUT`. Module không bao giờ gửi lại khi job đang chờ (docs
  `xu-ly-loi`: "Nhận Pending có nên tự gửi lại không? Không.").
- Vòng tải tệp tải PDF và XML, lưu lại và ghi sha256 của chúng.
- Sau `401` trên create, client bỏ token đã cache và không bao giờ gửi lại POST đó trong cùng
  lượt claim; POST đó vẫn tính vào ngân sách. Mỗi tiến trình giữ cache token riêng, nên một
  token bị thu hồi có thể tốn một POST cho mỗi tiến trình.

## 9. Vận hành hàng đợi review

Mọi việc module không hoàn tất an toàn được đưa vào review case. Mỗi document có tối đa một case
mở, mang được nhiều lý do; case đóng khi lý do cuối cùng được xử lý. Operator là người dùng (SLO:
1 ngày làm việc). Xử lý bằng `module.resolve_review.execute(case_id, resolution, actor, note,
...)` từ tiến trình có provider (CLI duyệt không xử lý review). `waive`, `acknowledge` và
`retry_with_new_reference` cần note; DB từ chối note trống. `DocumentInFlight` nghĩa là worker
đang giữ document: thử lại sau tối đa 60 giây.

### Lý do × trạng thái → cách xử lý

| Lý do | Trạng thái document | Cách xử lý được phép | Việc cần làm |
|---|---|---|---|
| `DRAFT_MISSING` | `needs_draft` | `attach_draft`, `waive` | Settlement không tìm thấy bản nháp sau 1 giờ. Nộp bản nháp của host (`attach_draft`), hoặc `waive` kèm note khi không phải xuất hoá đơn |
| `AMOUNT_MISMATCH` | `blocked` | `attach_draft`, `waive` | Số tiền settle khác tổng bản nháp. Gắn bản nháp có tổng bằng số tiền settle |
| `DUPLICATE_SETTLEMENT` | `blocked` | `retry`, `waive` | Settlement thứ hai cho cùng đơn; xem "Thu trùng" bên dưới |
| `ISSUER_ACCOUNT_UNAVAILABLE` | `blocked` | `retry`, `waive` | Không có tài khoản xuất hoá đơn active cho merchant và môi trường. Sửa hoặc bật tài khoản (mục 5), rồi `retry` |
| `ISSUE_DATE_CROSSED_DAY` | `blocked` | `retry`, `waive` | Ngày lập sẽ khác ngày settle và chính sách tài khoản là `review`. `retry` xác nhận ngày mới |
| `DRAFT_HELD` | `blocked` | `retry`, `attach_draft`, `waive` | Kế toán giữ nháp `manual`. `retry` đưa về chờ duyệt; `attach_draft` với nội dung host sửa (duyệt lại); `waive` |
| `QUOTA_EXHAUSTED` | `queued`, `approved` | `retry` | Xem "Hết hạn mức" bên dưới |
| `PROVIDER_REJECTED` | `failed` | `retry`, `attach_draft`, `retry_with_new_reference`, `mark_external`, `waive` | Câu trả lời dứt khoát của provider; đọc `details.code` hoặc `details.provider_message` |
| `PROVIDER_FAILED` | `failed` | `retry`, `attach_draft`, `retry_with_new_reference`, `mark_external`, `waive` | Giữ để xử lý case do bản cũ mở; lỗi mới dùng `PROVIDER_REJECTED` |
| `LOOKUP_MISMATCH` | `failed` | `retry`, `attach_draft`, `retry_with_new_reference`, `mark_external`, `waive` | Tra cứu thấy hoá đơn không khớp document (ví dụ tổng tiền). Đối soát tại cổng |
| `PROVIDER_TIMEOUT` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | Chưa rõ kết quả; xem "Kết quả chưa rõ" bên dưới |
| `AUTH_FAILED` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | Credential bị từ chối. Sửa credential, rồi `retry` |
| `MAX_ATTEMPTS_EXCEEDED` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | Hết ngân sách POST mà chưa biết kết quả. Đối soát tại cổng trước |
| `DOCUMENT_EXISTED_UNRESOLVED` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | `409` (hoặc xung đột khi gửi lại) mà tra cứu không thấy hoá đơn của ta; `details.provider_state` cho biết tra cứu thấy gì |
| `CORRECTION_REJECTED` | `failed` | `retry`, `waive` | Điều chỉnh và thay thế (v0.2) |
| `STALE_REFERENCE_FOUND` | `any` | `acknowledge` | Sweep thấy bản ghi dưới reference operator đã thay. Xử lý tại cổng, rồi `acknowledge` |
| `STALE_REFERENCE_FOUND` | `failed` | `acknowledge`, `mark_external` | Như trên; `mark_external` khi bản ghi cũ chính là hoá đơn |

`mark_external` luôn kèm số hoá đơn và ngày lập chép từ cổng SePay. `retry`, và `attach_draft`
cùng reference, cho phép đúng **một** POST nữa tính từ số POST hiện tại (kể cả khi chưa gửi lần
nào, ví dụ sau `AUTH_FAILED` khi lấy token); chúng không khôi phục lượt tự gửi lại do ngày. POST
đó bị từ chối hay lại mất kết quả thì document quay lại review. Sau một lần đổi reference bị
crash giữa chừng, `retry` đầu tiên cho một POST; thất bại thì `retry` lần nữa.

### Thu trùng

Settlement thứ hai cho cùng đơn không bao giờ tự xuất: document của nó đứng `blocked` với
`DUPLICATE_SETTLEMENT` (biên nhận vẫn được ghi). `retry` kèm note khi đó thật sự là đơn cần xuất
hoá đơn; `waive` kèm note khi tiền được hoàn hoặc cấn trừ ngoài module (không hoá đơn). Bản nháp
host gửi muộn cho document như vậy: (1) document **chỉ** bị giữ vì thu trùng (không ở
`needs_draft`, không có `AMOUNT_MISMATCH` mở) không được tự gắn; module tìm và gắn bản nháp khi
operator chọn `retry`; (2) document vẫn thuộc tập candidate (`blocked` với `AMOUNT_MISMATCH` mở)
có thể nhận snapshot và clear `AMOUNT_MISMATCH`, nhưng vẫn `blocked`: không bao giờ được xếp hàng
hay phát hành khi thu trùng còn mở hoặc đã `waive`, và `waive` là quyết định cuối (bản nháp đến
sau, `retry` lý do khác hay đóng case đều không mở lại).

### Từ chối ngày lập và ngân sách POST

- `400 ISSUED_DATE_INVALID` (thường do gửi vắt qua nửa đêm): module tra cứu reference rồi gửi
  lại **một lần** cùng reference với ngày hôm nay. Lượt gửi lại này được ghi bền vững và không
  bao giờ lặp lại, kể cả sau khi khởi động lại; `409` ở lần gửi lại luôn vào review. Lượt gửi lại
  theo chính sách qua ngày của tài khoản (`review` thì mở `ISSUE_DATE_CROSSED_DAY`).
- Review ngày mở khi lần gửi lại vẫn bị từ chối hoặc nhận `409`, khi lượt gửi lại đã dùng (kể cả
  trước khi khởi động lại), hoặc khi module không chắc POST trước có tới SePay hay không
  (`details.date_resend_skipped = "earlier_post_unrecorded"`: có thể mới gửi một POST, không phải
  hai). `earlier_post_rejected` nghĩa là POST ngay trước, cùng reference, có ghi nhận bị từ chối;
  `date_resend_used` và `operator_grant_used` là các trường hợp còn lại.
- Module tự gửi tối đa **2** POST cho mỗi reference trên mọi đường (gửi đầu, gửi lại sau mất
  phản hồi, timeout hay khởi động lại, lượt gửi lại do ngày); mọi POST đã gửi đều tính, kể cả POST
  nhận `500 QUOTA_HAS_BEEN_USERD_UP`. Hết ngân sách mà chưa biết kết quả thì mở
  `MAX_ATTEMPTS_EXCEEDED`.
- Nghi thứ tự ngày lập trong ký hiệu thì đối soát tại cổng (bên dưới) thay vì `retry`.

### Hết hạn mức

SePay trả `500 QUOTA_HAS_BEEN_USERD_UP` (mã viết đúng như API trả; docs: "đừng retry tự động vì
lỗi này không tự hết"). Module mở `QUOTA_EXHAUSTED` ngay và không bao giờ tự gửi lại: document
đứng ở `queued` hoặc `approved` (duyệt được giữ) và không có lượt chạy kế tiếp. Gia hạn hạn mức
trên cổng SePay, kiểm `GET /v1/usage` (`quota_remaining`), rồi `retry` (đúng một POST; lý do này
không có `acknowledge`). `429` không có trong docs SePay; module coi là lỗi tạm thời. Job kết thúc
`Failed` với thông điệp hết hạn mức vào `PROVIDER_REJECTED` kèm thông điệp nguyên văn. Mã hết hạn
mức được nhận ở mọi endpoint, nhưng chỉ create mới mở `QUOTA_EXHAUSTED`: ở bước kiểm trạng thái
hay tra cứu nó hiện ra như một bước worker lỗi (`invoice_*_step_failed`), và document chờ hết
lease (60 giây) trước lần thử sau.

### Thông điệp lỗi của provider

- `PROVIDER_REJECTED` với `details.provider_job_state = "Failed"`: `details.provider_message` là
  thông điệp nguyên văn của SePay (docs: `Failed` bất đồng bộ "không có error.code, nguyên nhân
  nằm trong message"). Module không bao giờ tự gửi lại. `retry` xoá request cũ và gửi **một** POST
  mới cùng reference, kể cả khi worker chết giữa lúc đóng băng và gửi. Nếu POST đó nhận `409` mà
  provider vẫn giữ bản ghi Failed, case thành `DOCUMENT_EXISTED_UNRESOLVED` với `provider_state =
  "failed"`: provider đã tiêu thụ reference. Đối soát tại cổng, rồi `mark_external` hoặc `waive`.
- Nếu thông điệp nói về **thứ tự ngày lập trong ký hiệu** (đã có hoá đơn ngày sau cùng ký hiệu, ví
  dụ phát hành tay trên cổng hoặc hệ thống khác dùng chung ký hiệu), `retry` sẽ thất bại cho tới khi
  qua ngày đó: **không** `retry` lặp. Đối soát tại cổng, rồi `mark_external` (đã phát hành tại cổng)
  hoặc `waive`.
- Chữ hoa/thường: `details.provider_job_state` là `"Failed"` (cách viết của SePay) với job thất
  bại, nhưng là trạng thái chữ thường của module (`"failed"`, `"pending"`, `"success"`) với kết quả
  chưa xác nhận.
- **Trạng thái tài khoản (dứt khoát):** `REGISTRATION_NOT_COMPLETE`, `BILLING_IS_UNPAID`,
  `EINVOICE_ACCOUNT_NOT_FOUND` trong `details.code` (mã gốc của SePay, docs: "gọi lại không làm
  thay đổi kết quả"). Hoàn tất đăng ký, thanh toán, hoặc sửa `provider_account_id` trên my.sepay.vn,
  rồi `retry`.

### Chưa xác nhận được kết quả phát hành

Lần kiểm có thông điệp đúng nguyên văn "Hệ thống chưa xác nhận được kết quả phát hành với nhà cung
cấp hóa đơn" (docs `theo-doi-trang-thai-xuat-hoa-don`: "Không gửi lại yêu cầu - hãy liên hệ SePay")
mở case với `details.unconfirmed = true`. `retry` và `attach_draft` bị từ chối tới khi tra cứu thấy
hoá đơn đã phát hành hoặc đang xử lý. Liên hệ SePay, rồi `mark_external` (SePay xác nhận đã phát
hành; chép số và ngày lập) hoặc `waive`.

Hạn chế đã biết: với lần kiểm `Pending` hoặc `Success`, văn bản thông điệp không được lưu
(`provider_message` vắng và log `invoice_document_failed` ghi `provider_message_length = null`);
chỉ mã cho biết đó là câu trong docs. Chỉ lần kiểm `Failed` mới lưu thông điệp.

Nếu câu trả lời "chưa xác nhận" đến muộn sau khi document đã vào `PROVIDER_TIMEOUT`, cơ chế fencing
của lease bỏ qua nó; `retry` mà tra cứu không thấy gì thì xếp hàng document lại.

### Kết quả chưa rõ

`DOCUMENT_EXISTED_UNRESOLVED`, `PROVIDER_TIMEOUT` và tra cứu không thấy gì sau lỗi: document ở lại
review; module **không bao giờ** tự đổi reference và không có thời hạn. Đối soát reference cũ tại
cổng SePay, rồi gọi `retry_with_new_reference` với `note` ghi kết quả đối soát (bắt buộc; DB từ chối
note trống). Module kiểm lại một lần nữa rằng tra cứu không thấy gì ngay trước khi đổi.
`PROVIDER_TIMEOUT` với `details.last_poll_error = "transient_error"` nghĩa là các lần kiểm cuối
không nhận được câu trả lời dùng được (ví dụ lần kiểm cứ trả về reference khác).
`STALE_REFERENCE_FOUND` (sweep thấy bản ghi dưới reference đã thay): xử lý tại cổng theo bảng của
kế toán bên dưới, rồi `acknowledge`.

### Bản ghi đã huỷ

Tra cứu báo `cancelled` mở `DOCUMENT_EXISTED_UNRESOLVED` với `provider_state = "cancelled"`.
`retry` và `attach_draft` bị từ chối (`provider_record_cancelled`), `retry_with_new_reference` bị
từ chối theo cấu trúc (`ReferenceStillExists`). Lối ra của v0.1 là `mark_external` (rồi
`MarkCancelledExternally` nếu hoá đơn bị huỷ sau khi phát hành) hoặc `waive`; hoá đơn thay thế là
việc của v0.2.

### Mã lỗi (docs SePay `xu-ly-loi` và trang tạo hoá đơn)

Module quyết định theo `error.code` và HTTP status, không bao giờ theo văn bản thông điệp (docs:
"Xử lý lỗi dựa trên error.code, không khớp theo chuỗi error.message").

| HTTP và mã | Hành động của module |
|---|---|
| `400 ISSUED_DATE_INVALID` | Tra cứu, rồi gửi lại một lần với ngày hôm nay (bên trên) |
| `400 VALIDATION_ERROR` | `PROVIDER_REJECTED` (`validation_error`); sửa dữ liệu, `attach_draft` |
| `400 SELLER_STORE_NOT_FOUND` | `PROVIDER_REJECTED` (`seller_store_invalid`) |
| `400 SELLER_STORE_INACTIVE` | `PROVIDER_REJECTED` (`seller_store_invalid`) |
| `422 INVOICE_DATE_BEFORE_TAX_APPROVAL` | `PROVIDER_REJECTED` (`registration_not_approved`); chỉ production |
| `422 REGISTRATION_NOT_TAX_APPROVED` | `PROVIDER_REJECTED` (`registration_not_approved`) |
| `409 EINVOICE_DOCUMENT_EXISTED` | Mọi `409` trên create: tra cứu theo reference; tự nó không bao giờ là thành công |
| `500 QUOTA_HAS_BEEN_USERD_UP` | `QUOTA_EXHAUSTED`, không tự gửi lại |
| `500 REGISTRATION_NOT_COMPLETE` | `PROVIDER_REJECTED`, trạng thái tài khoản |
| `500 BILLING_IS_UNPAID` | `PROVIDER_REJECTED`, trạng thái tài khoản |
| `500 EINVOICE_ACCOUNT_NOT_FOUND` | `PROVIDER_REJECTED`, trạng thái tài khoản |
| `401` | Lấy token mới; trên create không gửi lại trong cùng lượt claim, và POST vẫn tính |
| `5xx` không kèm mã nhận diện được | Tạm thời: thử lại sau, tra cứu trước |
| `404 EINVOICE_DOCUMENT_NOT_FOUND` khi tra cứu | Không có hoá đơn dưới reference (mã SePay thật sự trả) |
| `2xx` mang `EINVOICE_DOCUMENT_EXISTED` | Không phải tiếp nhận: `PROVIDER_REJECTED` |

### Sửa sai sau khi phát hành (bảng của kế toán)

Docs SePay vẫn dẫn Nghị định 123/2020/NĐ-CP và Thông tư 78/2021/TT-BTC; module dẫn Nghị định
254/2026/NĐ-CP và Thông tư 91/2026/TT-BTC. Kế toán xác nhận trước cổng Live. Bảng dưới là câu trả
lời của kế toán, soát lại ngày 29/10/2026 theo Nghị định 254/2026/NĐ-CP Điều 21 và Thông tư
91/2026/TT-BTC Điều 10 (giữ nguyên bảng ngày 20/10/2026). v0.1 không phát hành điều chỉnh hay thay
thế: làm trên cổng SePay (v0.2 bổ sung).

| Sai sót trên hoá đơn đã phát hành | Cách xử lý | Thông báo | Thời hạn |
|---|---|---|---|
| Sai tên, địa chỉ người mua (mã số thuế đúng) | Điều chỉnh | Mẫu 04 | Trong tháng |
| Sai mã số thuế người mua | Điều chỉnh | Mẫu 04 | Trong tháng |
| Sai thuế suất hoặc tiền thuế | Thay thế | Mẫu 04 | Trong tháng |
| Sai đơn giá, số lượng, tổng tiền | Thay thế | Mẫu 04 | Trong tháng |
| Khách muốn lấy hoá đơn công ty sau khi đã xuất dạng "không lấy hoá đơn" | Thay thế | Mẫu 04 | Trong tháng |

## 10. Huỷ hoá đơn

Module không có API huỷ. Huỷ trên cổng SePay, rồi ghi nhận lại:

```python
async def record_cancellation(invoice, document_id, actor: str) -> None:
    await invoice.mark_cancelled_externally.execute(document_id, "cancelled at the portal", actor)
```

Chỉ document `issued` mới ghi nhận được (không thì `CancellationNotAllowed`); actor và lý do được
ghi lại.

## 11. Tệp hoá đơn và sao lưu

Vòng tải tệp lưu PDF và XML qua
[`FileArtifactStore`](../src/invoice_module/adapters/storage/file_store.py) dưới thư mục gốc host
truyền vào, ghi URI `file://` và sha256 của cả hai trên document, và không bao giờ ghi đè tệp.

- **XML đã ký số là bản gốc hợp pháp** của hoá đơn điện tử; PDF chỉ là bản thể hiện (Nghị định
  254/2026/NĐ-CP Điều 5, lưu dưới dạng thông điệp dữ liệu). Module tải và băm cả hai.
- Lưu 10 năm (câu trả lời của kế toán). Sao lưu thư mục gốc định kỳ bằng công cụ của host, và giữ
  một bản ngoài máy chủ ứng dụng.
- Tệp được tạo với mode `0600`, thuộc user chạy worker: chạy sao lưu bằng user đó (hoặc tự cấp quyền
  đọc), và đặt thư mục gốc trên filesystem giữ quyền sở hữu.
- Hoá đơn đã phát hành mà thiếu tệp quá 1 giờ thì tăng `invoice_artifacts_missing_total`.

## 12. Cảnh báo và truy vấn độ phủ

Metric đi vào `MetricsSink` host truyền vào (`increment` kèm tag). Cảnh báo theo:

| Metric hoặc log | Ý nghĩa |
|---|---|
| `invoice_review_overdue_total` | Review mở quá 1 ngày làm việc (SLO) |
| `invoice_approval_overdue_total` | Nháp `manual` chưa duyệt sau 1 ngày làm việc kể từ `settled_at` (cùng cách đếm ngày làm việc với SLO review) |
| `invoice_documents_stale_total` (log `invoice_document_stale`) | Document kẹt ở trạng thái đang xử lý quá 15 phút |
| `invoice_quota_exhausted_total` | Create bị trả mã hết hạn mức |
| `invoice_artifacts_missing_total` | Hoá đơn đã phát hành thiếu tệp sau 1 giờ |
| `invoice_stale_reference_found_total` | Reference đã thay vẫn sinh bản ghi |
| log `invoice_document_failed` (warning) | Mọi document chuyển sang `failed`, kèm `error_code`, `reason`, `provider_state` và `provider_message_length` |
| `worker_loop_errors_total` | Một bước worker ném lỗi |

Document hết hạn mức đứng ở `queued` hoặc `approved` và không có lượt chạy kế tiếp, nên cảnh báo
stale bắn ở mọi lần sweep cho tới khi gia hạn hạn mức và operator `retry`: đây là hành vi chấp
nhận, không phải lỗi.

Truy vấn độ phủ (prefix mặc định `pm_` và `inv_`):

```sql
-- settlements without an original invoice document (should be empty)
SELECT s.id, s.settled_at FROM pm_settlements s
LEFT JOIN inv_documents d ON d.settlement_id = s.id AND d.kind = 'original'
WHERE d.id IS NULL AND s.settled_at < now() - interval '5 minutes';

-- documents waiting for a draft for over 1 hour
SELECT id, host_ref_id, created_at FROM inv_documents
WHERE status = 'needs_draft' AND created_at < now() - interval '1 hour';

-- review cases open for over one day (the metric counts business days)
SELECT id, document_id, reason, opened_at FROM inv_review_cases
WHERE status = 'open' AND opened_at < now() - interval '1 day';

-- time from settlement to issue
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY issued_at - settled_at)
FROM inv_documents WHERE status = 'issued' AND kind = 'original';
```

- Trước 01/01/2027, kiểm rằng tài khoản SePay có ký hiệu năm mới khớp `invoice_series_template`
  (đăng ký chỉ kiểm ký hiệu của năm nay).
- **Hạn mức.** Bộ đếm hạn mức của sandbox đứng yên (giữ 999 qua nháp và hoá đơn đã phát hành), nên
  mức tiêu thụ hạn mức của nháp và của hoá đơn phát hành **chưa đo được**. Docs SePay ghi "Hóa đơn
  nháp không tính vào hạn ngạch"; module chưa kiểm được câu này (và không tạo nháp trên SePay). Theo
  dõi `invoice_quota_exhausted_total` trên Live và kiểm `GET /v1/usage` trước đợt xuất lớn (docs:
  "Kiểm tra số lượt còn lại bằng API kiểm tra hạn mức trước khi xuất hóa đơn").
- **Chính sách log.** Adapter và worker chỉ log method, path, HTTP status, mã lỗi, trạng thái job
  và độ dài thông điệp. Văn bản của SePay chỉ nằm trong `review_cases.details.provider_message`.

## 13. Giới hạn đã biết

- v0.1 không huỷ, điều chỉnh hay thay thế qua API (huỷ trên cổng, mục 10; điều chỉnh và thay thế
  là v0.2).
- Tệp hoá đơn chỉ nằm trên filesystem cục bộ (không S3); một provider (SePay eInvoice).
- Không xuất hoá đơn cho hoàn tiền hay thanh toán một phần; không xuất hoá đơn theo kỳ cho dịch vụ
  trả trước nhiều kỳ.
- Không có giao diện duyệt (dùng CLI ở mục 14) và không có sản phẩm "từ chối nháp": nháp sai được
  giữ lại và vào review.
- Nháp chỉ nằm trong module; không tạo nháp trên SePay. Một chế độ xuất cho cả đơn.
- Ở chế độ `manual`, ngày lập là ngày module gửi hoá đơn sau khi duyệt (thường là ngày duyệt).
  Người dùng và kế toán đã chấp nhận; đối chiếu với Nghị định 254/2026/NĐ-CP Điều 9 khoản 2 là
  trách nhiệm của kế toán.
- Thông điệp "chưa xác nhận" không được lưu ở lần kiểm `Pending` hoặc `Success` (mục 9).

## 14. Duyệt hoá đơn xuất thủ công

Kế toán dùng CLI, chỉ làm việc với DB (không gọi provider, không cần credential):

```sh
export INVOICE_DATABASE_URL="postgresql+asyncpg://..."  # or pass --database-url
python -m invoice_module.cli approvals list
python -m invoice_module.cli approvals show DOCUMENT_ID
python -m invoice_module.cli approvals approve DOCUMENT_ID --actor "accountant-name" --expect-sha256 SHA256_FROM_SHOW
python -m invoice_module.cli approvals hold DOCUMENT_ID --actor "accountant-name" --note "wrong buyer tax code"
```

- `list` in mỗi nháp chờ duyệt một dòng JSON (`document_id`, `settlement_id`, `host_ref`,
  `total_vnd`, `buyer_name`, `settled_at`, `age_hours`); `--overdue-only` chỉ giữ nháp đã quá
  hạn, `--tenant-id` và `--limit` thu hẹp danh sách.
- `show` in toàn bộ nháp (người mua, từng dòng với thuế suất và tiền thuế, tổng tiền) và
  `snapshot_sha256`. Đọc kỹ trước khi duyệt.
- `approve` gắn lần duyệt với đúng nội dung đã hiển thị: truyền `--expect-sha256` lấy từ `show`;
  nội dung đã đổi thì kết quả là `not_allowed:snapshot_changed` và phải duyệt lại. Actor và thời
  điểm duyệt ghi vào `approved_by` và `approved_at`.
- **Ngày lập hoá đơn là ngày module gửi hoá đơn tới SePay sau khi duyệt.** Module đóng băng ngày
  ngay trước khi gửi, theo giờ Asia/Saigon, và không bao giờ gửi ngày cũ; `approved_at` được lưu
  riêng. SePay chỉ thấy hoá đơn sau khi duyệt.
- Nháp sai: `hold` (chỉ được trước khi bắt đầu gửi), host nộp nội dung đã sửa, `attach_draft`
  trong hàng đợi review (mục 9), rồi duyệt lại.
- Kết quả là các dòng JSON (`approved`, `already_approved`, `held`, `not_allowed:<lý do>`,
  `in_flight`). Exit code: `0` mọi document đã duyệt (hoặc đã giữ), `3` có document không làm được,
  `2` sai tham số (thiếu `--actor` hay `--note`, giá trị trắng), `1` không dùng được DB. URL DB và
  mật khẩu không bao giờ được in ra.
- Cảnh báo quá hạn ở mục 12.
