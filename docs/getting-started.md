# Getting started

**English** · [Tiếng Việt](getting-started.vi.md)

This guide takes a host that already uses `payment-module` to one VAT invoice per settlement:
install `invoice-module`, create its tables, onboard the issuer account the accountant set up
at SePay eInvoice, submit the invoice draft when the payment intent is created, and run the
worker that issues the invoice once the money has arrived. The second half is the operator
runbook: the review queue, cancellation, artifacts, alerts and accounting approval.

The example host [`examples/invoice_host`](../examples/invoice_host) runs the whole flow on
PostgreSQL with a fake provider and mounts the payment-module webhook router behind a real HTTP
listener. Its `onboard(payment, invoice, issuer_config, *, provider_account_id)` takes the SePay
account id as a required keyword, because that id comes from SePay, not from the accountant's
configuration file.

What the module owns and what your host owns: the module records every `PaymentSettled`,
freezes the draft your host submitted and issues exactly one original invoice for it at SePay
eInvoice. Your host owns products, prices, tax rates, item codes and buyer details; the
accountant owns the issuer settings (template, series, payment method, buyer fallback, the
cross-day policy). The module never invents a line, a tax rate or a setting.

Legal basis cited by this guide: Decree 254/2026/NĐ-CP and Circular 91/2026/TT-BTC, which
replace Decree 123/2020/NĐ-CP and Decree 70/2025/NĐ-CP from 1 July 2026.

## 1. Install

Install `payment-module` first (from its GitHub Release), then `invoice-module` with the extras
your host needs:

```sh
uv pip install "payment-module[sqlalchemy,postgres,sepay,fastapi] @ https://github.com/phanhaitrieu37/payment-modules/releases/download/v0.1.0/payment_module-0.1.0-py3-none-any.whl"
uv pip install "invoice-module[sqlalchemy,postgres,sepay,payment]"
```

Extras: `sqlalchemy` (tables, repositories, unit of work, Alembic), `postgres` (asyncpg),
`sepay` (the SePay eInvoice adapter, httpx), `payment` (the `PaymentSettled` outbox consumer).
Python 3.12 or newer and PostgreSQL are required (the schema uses partial unique indexes,
`JSONB` and `SKIP LOCKED`).

## 2. Create the schema

[`define_tables`](../src/invoice_module/adapters/sqlalchemy/tables.py) declares the five
`inv_` tables (issuer accounts, drafts, settlement receipts, documents, review cases); none of
them has a foreign key to a `pm_` table. The frozen migration
[`schema_v1.upgrade`](../src/invoice_module/adapters/sqlalchemy/migrations/schema_v1.py)
creates them. Nothing runs on import: your host runs the migration, after a backup.

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

`schema_v1` never changes; a later schema ships as a new module with an upgrade step.

## 3. Credentials

An issuer account stores only a reference such as `env:SEPAY_EINVOICE`.
[`EnvCredentialResolver`](../src/invoice_module/secrets/env_credential_resolver.py) reads
`SEPAY_EINVOICE_CLIENT_ID` and `SEPAY_EINVOICE_CLIENT_SECRET` (the reference name plus
`_CLIENT_ID` and `_CLIENT_SECRET`). An unset or blank variable raises a configuration error that
names the variable, never a value.

- Keep the values in an env file outside the repository with mode `600`, loaded by the
  process manager. Never commit it, print it or paste it into logs, tickets or chat.
- The sandbox and production have separate credentials; a sandbox token sent to production gets
  `401` (docs `xu-ly-loi`: "Token chỉ dùng được cho môi trường đã cấp nó").
- To read from your own secret store, implement the
  [`CredentialResolver`](../src/invoice_module/ports/credentials.py) port.

## 4. Build the module

[`build_invoice_module`](../src/invoice_module/builder.py) wires every use case once; keep one
`InvoiceModule` per process. With a provider and credentials it also builds the worker use
cases, onboarding and the review queue; without them it builds only what works on the database
alone (draft submission, settlement intake, approval, hold, queries), which is what the
approval command line uses.

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

- The client always uses the sandbox `https://einvoice-api-sandbox.sepay.vn` for `test`
  accounts and refuses any other URL for them. A `live` account works only after the host
  passes the production base URL (docs `tong-quan`) in `base_urls`; without it every call for a
  live account raises `ProviderConfigError` before anything is sent.
- Pass the same `artifact_timeout_s` to `SePayEInvoiceClient` and to `InvoiceModuleConfig`.
  The artifact lease is `2 × artifact_timeout_s + 30` seconds (210 s by default); a client
  timeout longer than the module's value lets another worker take over a document mid-download.
  The sandbox served one download in about 38 s.
- Use the module's use cases (`module.discard_draft`, `module.submit_draft`, ...) instead of
  constructing them: constructors such as `DiscardDraft(uow_factory, clock, metrics)` may change
  between minor versions.

## 5. Onboard an issuer account

One issuer account per merchant and environment. The accountant supplies every setting: template
code, series template (for example `C{YY}...`, where `{YY}` becomes the two digits of the issue
year), payment method, buyer fallback name and address (the address may be the empty string),
the cross-day policy (`issue` or `review`). The module has no default for any of them.

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

- `register_issuer_account` checks the settings before any provider call (`payment_method`
  must be one of `TM`, `CK`, `TM/CK`, `KHAC`, the docs enum of
  `CreateInvoiceRequest.payment_method`; `credential_ref` must be `env:NAME`; the fallback name
  must not be blank), then calls the provider account detail and refuses with
  `IssuerAccountMismatch` when the provider does not know the account, the template or this
  year's series (docs `xuat-hoa-don-dien-tu`: the `C{YY}` part "phải khớp năm hiện tại"). The
  account is stored as `draft`. The same settings again return the stored account; other settings
  for the same merchant and environment raise `IssuerAccountConflict`.
- `set_issuer_account_status` requires an actor and a reason and writes both, with the old and
  the new status, to the `invoice_issuer_account_status_changed` log line only: the account row
  keeps the current status and `updated_at`, not who changed it, so keep that log line if you
  need the history of status changes. `active` needs the cross-day
  policy, and a `live` account also needs readiness evidence; `disabled` stops new settlements
  from finding the account, so they open `ISSUER_ACCOUNT_UNAVAILABLE` (section 9). Disable the
  account when SePay reports it inactive, fix it at SePay, re-run readiness, activate it again.
- **Readiness (live only).** Before a `live` account is activated, record the sandbox evidence
  file of the scenario run with `record_issuer_readiness.execute(account_id, evidence_path,
  actor)`. It refuses (`ReadinessRejected`) when the provider does not report the account as
  `active: true` (docs `bat-dau-nhanh`, callout "Chọn tài khoản đúng": "Chỉ sử dụng tài khoản có active: true"), when the
  file is missing, when its `environment` is not `test`, or when s1, s2 or s4 did not pass, or s3
  neither passed nor is `INCONCLUSIVE` with `details.s3_variant` `not_triggerable` or
  `sync_reject`. It warns without blocking when the live account's
  `tax_authority_approved_date` is after today (`before_tax_approval_date`; docs: `422
  INVOICE_DATE_BEFORE_TAX_APPROVAL` is checked in production only) or not reported
  (`approval_date_unknown`).

## 6. Submit the draft with CreateIntent

The host submits the invoice draft in the same transaction that creates the payment intent, so
an intent never exists without its draft. Both modules join the host's session:

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

**Delivery mode.** Every draft carries `delivery_mode`; the module has no default. `auto`
issues the invoice as soon as the money arrives. `manual` keeps the draft inside the module when
the money arrives (nothing is sent to SePay); the accountant approves it (section 14), then the
module creates and issues the invoice in one step. The host chooses per order.

**Binding rules.** One draft per `(tenant, host_ref)`. Submitting again with the same intent
updates the open draft and bumps its revision. A second checkout for the same order (a new
intent) may take over the open draft only while the first intent has no settled document; after
that `SubmitDraft` raises `DraftBoundToOtherIntent`. A draft consumed by a settlement is reopened
only for a new intent (the duplicate-payment path, section 9). Drop an open draft with
`module.discard_draft.execute(tenant_id, host_ref_type, host_ref_id, actor)`.

**Totals.** `declared_total_vnd` must equal the sum of the line totals computed by the module
(`TotalMismatch` otherwise), and the settled amount must equal the draft total; a mismatch opens
`AMOUNT_MISMATCH`. Money is integer VND computed with `Decimal`, half-up per line; a host-given
`tax_amount_vnd` may differ from the computed tax by at most 1 VND per line. A `float` anywhere
raises `TypeError`.

**Host contract (SePay docs `xuat-hoa-don-dien-tu`, schema `CreateInvoiceRequest`).** Every
check below that the module does not make itself comes back from SePay as `400
VALIDATION_ERROR`, which opens `PROVIDER_REJECTED`.

| Field | Contract | Source |
|---|---|---|
| line type | v0.1 sends only goods lines, `line_type = 1`; no promotion, trade-discount or note lines | `InvoiceItem.line_type` enum 1–4 |
| `item_code` | required on every line: the module refuses a line without one (`InvalidLine(index, "item_code")`) before any database work. The docs mark it optional; the sandbox answers `400 VALIDATION_ERROR` without it. Use the product's real item code before Live | `InvoiceItem.item_code` |
| `quantity` | positive, at most 2 decimals ("tối đa 2 chữ số sau dấu phẩy"). Fractional quantities: only `1.5` is proven on the sandbox (issued on 23/10/2026, read back as `"1.50"` with an exact total); other fractional values are unverified on the sandbox | `InvoiceItem.quantity` |
| `discount_vnd` | a value above 0 is unverified on the sandbox; the module also sends `before_discount_and_tax_amount` on goods lines (the docs describe it "dùng cho line_type=3") | `InvoiceItem.discount_amount` |
| `tax_rate` | one of `-2`, `-1`, `0`, `5`, `8`, `10`; the module sends `tax_amount` as computed ("dùng đúng giá trị bạn gửi, KHÔNG tính lại") | `InvoiceItem.tax_rate` |
| `notes` | at most 255 characters (the limit stated on the adjust and replace pages; the create page states none) | `notes` |
| buyer | `national_id` at most 12 characters, `email` a valid address; the module does not send `buyer_code` (docs: at most 100). A tax code needs a name and an address | `Buyer` |
| `payment_method` | `TM`, `CK`, `TM/CK` or `KHAC`, checked at onboarding | `payment_method` enum |

## 7. Wire the outbox

The module hears about payments only through the payment-module outbox, never inside the
settlement transaction. Build the invoice module first, then pass a
[`FanOutPublisher`](../src/invoice_module/integrations/payment_module/fanout.py) to
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

Consumers run in order, one after the other, for each event. The outbox delivers at least once
and retries the whole event when a consumer raises, so every consumer must be idempotent by
`event_id` (the invoice consumer stores a receipt keyed by `event_id`; a redelivery is a no-op).
The invoice consumer only records the receipt and the document and commits; every network call
happens in the worker.

## 8. Run the worker

```python
import asyncio

from invoice_module.runtime import InvoiceWorker


async def run(invoice, stop: asyncio.Event) -> None:
    await InvoiceWorker(invoice).run(stop)  # returns within one interval after stop.set()
```

The worker runs four loops: issue, poll, the overdue sweep and artifacts. A loop that fails is
logged (`invoice_worker_loop_failed`) and continues; leases and retry state live in the
database. Several worker processes may run against the same database.

- The issue loop freezes the request just before sending, with the Asia/Saigon date of that
  moment as the issue date, and records every POST before it leaves. The `reference_code` of an
  original invoice is the `settlement_id`, written before any call; it never changes on its own.
- The poll loop checks the job (3, 5, 10, 20, 30, then every 60 seconds, or the provider's
  `next_retry_at`) until `Success` or `Failed`; after `pending_timeout` (3 hours, above the
  docs' 2-hour window) the document opens `PROVIDER_TIMEOUT`. The module never resends while a
  job is pending (docs `xu-ly-loi`: "Nhận Pending có nên tự gửi lại không? Không.").
- The artifact loop downloads the PDF and the XML, stores them and records their sha256.
- After a `401` on a create the client drops its cached token and never resends that POST in
  the same claim; the POST still counts against the budget. Several processes each keep their
  own token cache, so a revoked token may cost one POST per process.

## 9. Operate the review queue

Anything the module cannot finish safely goes to a review case. One open case per document can
carry several reasons; it closes when the last one is resolved. The operator is the user (SLO:
one business day). Resolve with `module.resolve_review.execute(case_id, resolution, actor, note,
...)` from a process built with the provider (the approval command line does not resolve
reviews). `waive`, `acknowledge` and `retry_with_new_reference` need a note; the database refuses
a blank one. `DocumentInFlight` means a worker holds the document: try again within 60 seconds.

### Reason × status → resolution

| Reason | Document status | Allowed resolutions | What to do |
|---|---|---|---|
| `DRAFT_MISSING` | `needs_draft` | `attach_draft`, `waive` | The settlement found no matching draft after 1 hour. Submit the host's draft (`attach_draft`), or `waive` with a note when no invoice is due |
| `AMOUNT_MISMATCH` | `blocked` | `attach_draft`, `waive` | The settled amount differs from the draft total. Attach a draft whose total equals the settled amount |
| `DUPLICATE_SETTLEMENT` | `blocked` | `retry`, `waive` | A second settlement for the same order; see "Duplicate payment" below |
| `ISSUER_ACCOUNT_UNAVAILABLE` | `blocked` | `retry`, `waive` | No active issuer account for the merchant and environment. Fix or activate the account (section 5), then `retry` |
| `ISSUE_DATE_CROSSED_DAY` | `blocked` | `retry`, `waive` | The issue date would differ from the settlement date and the account policy is `review`. `retry` confirms the new date |
| `DRAFT_HELD` | `blocked` | `retry`, `attach_draft`, `waive` | The accountant held a manual draft. `retry` sends it back for approval; `attach_draft` with the host's corrected content (approve again); `waive` |
| `QUOTA_EXHAUSTED` | `queued`, `approved` | `retry` | See "Quota" below |
| `PROVIDER_REJECTED` | `failed` | `retry`, `attach_draft`, `retry_with_new_reference`, `mark_external`, `waive` | A definitive provider answer; read `details.code` or `details.provider_message` |
| `PROVIDER_FAILED` | `failed` | `retry`, `attach_draft`, `retry_with_new_reference`, `mark_external`, `waive` | Kept for cases opened by earlier versions; new failures use `PROVIDER_REJECTED` |
| `LOOKUP_MISMATCH` | `failed` | `retry`, `attach_draft`, `retry_with_new_reference`, `mark_external`, `waive` | The lookup found an invoice that does not match the document (for example the total). Reconcile at the portal |
| `PROVIDER_TIMEOUT` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | Unknown outcome; see "Unknown outcome" below |
| `AUTH_FAILED` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | The credentials were refused. Fix them, then `retry` |
| `MAX_ATTEMPTS_EXCEEDED` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | The POST budget is spent without a known result. Reconcile at the portal first |
| `DOCUMENT_EXISTED_UNRESOLVED` | `failed` | `retry`, `retry_with_new_reference`, `mark_external`, `waive` | A `409` (or a resend conflict) whose lookup did not show our invoice; `details.provider_state` says what it showed |
| `CORRECTION_REJECTED` | `failed` | `retry`, `waive` | Adjustment and replacement (v0.2) |
| `STALE_REFERENCE_FOUND` | `any` | `acknowledge` | The sweep found a record under a reference the operator replaced. Settle it at the portal, then `acknowledge` |
| `STALE_REFERENCE_FOUND` | `failed` | `acknowledge`, `mark_external` | As above; `mark_external` when the stale record is the invoice |

`mark_external` always takes the invoice number and the issue date copied from the SePay portal.
`retry`, and `attach_draft` under the same reference, allow exactly one more POST counted from
the current number (also when nothing was sent yet, for example after `AUTH_FAILED` while
getting a token); they never restore the automatic date resend. If that POST is refused or its
result is lost again, the document comes back to review. After a rotation that crashed half way,
the first `retry` grants one POST; `retry` again if it fails.

### Duplicate payment

A second settlement for the same order is never issued by itself: its document stays `blocked`
with `DUPLICATE_SETTLEMENT` (the receipt is recorded either way). `retry` with a note when the
payment really is a second order to invoice; `waive` with a note when the money is refunded or
offset outside the module (no invoice). A draft the host submits late for such a document: (1) a
document held only for the duplicate (not `needs_draft`, no open `AMOUNT_MISMATCH`) is not
attached automatically; the module finds and attaches the draft when the operator chooses
`retry`; (2) a document still in the candidate set (`blocked` with an open `AMOUNT_MISMATCH`)
may take the snapshot and clear `AMOUNT_MISMATCH`, but stays `blocked`: it is never queued or
issued while the duplicate hold is open or waived, and `waive` is final (a later draft, a
`retry` of another reason or closing the case never reopens it).

### Date refusal and the POST budget

- `400 ISSUED_DATE_INVALID` (usually a send that crossed midnight): the module looks the
  reference up and resends **once** under the same reference with today's date. This resend is
  recorded durably and never repeated, even after a restart; a `409` on it always goes to review.
  The resend follows the account's cross-day policy (`review` opens `ISSUE_DATE_CROSSED_DAY`
  instead).
- The date review opens when the resend is refused again or answers `409`, when the resend was
  already used (also before a restart), or when the module cannot tell whether the earlier POST
  reached SePay (`details.date_resend_skipped = "earlier_post_unrecorded"`: maybe only one POST
  went out, not two). `earlier_post_rejected` means the POST just before, under the same
  reference, has a recorded refusal; `date_resend_used` and `operator_grant_used` name the
  other cases.
- The module sends at most **2** POSTs per reference on every path (first send, resend after a
  lost answer, timeout or restart, the date resend); every POST sent counts, including one
  answered with `500 QUOTA_HAS_BEEN_USERD_UP`. When the budget is spent without a known result
  it opens `MAX_ATTEMPTS_EXCEEDED`.
- If you suspect the date order of the series, reconcile at the portal (below) instead of
  retrying.

### Quota

SePay answers `500 QUOTA_HAS_BEEN_USERD_UP` (code spelled as the API returns it; docs:
"đừng retry tự động vì lỗi này không tự hết"). The module opens `QUOTA_EXHAUSTED` at once and
never resends by itself: the document stays `queued` or `approved` (an approval is kept) with no
next attempt. Top the quota up at the SePay portal, check `GET /v1/usage` (`quota_remaining`),
then `retry` (exactly one POST; `acknowledge` is not offered for this reason). `429` is not in
the SePay docs; the module treats it as transient. A job that ends `Failed` with a quota message
goes to `PROVIDER_REJECTED` with the message verbatim. The quota code is mapped on every
endpoint, but only a create opens `QUOTA_EXHAUSTED`: on a status check or a lookup it surfaces
as a failed worker step (`invoice_*_step_failed`), and the document waits for its lease (60 s)
before the next try.

### Provider failure messages

- `PROVIDER_REJECTED` with `details.provider_job_state = "Failed"`: `details.provider_message`
  is SePay's message verbatim (docs: an asynchronous `Failed` "không có error.code, nguyên nhân
  nằm trong message"). The module never resends by itself. `retry` clears the old request and
  sends **one** new POST under the same reference, even if a worker dies between freezing and
  sending. If that POST gets `409` and the provider still holds the failed record, the case
  becomes `DOCUMENT_EXISTED_UNRESOLVED` with `provider_state = "failed"`: the provider consumed
  the reference. Reconcile at the portal, then `mark_external` or `waive`.
- If the message is about the **issue-date order of the series** (an invoice with a later date
  already exists under the same series, for example one issued by hand at the portal or by
  another system sharing the series), `retry` fails until that date has passed: do **not** retry
  in a loop. Reconcile at the portal, then `mark_external` (issued there) or `waive`.
- Casing: `details.provider_job_state` is `"Failed"` (SePay's wording) on a failed job, but the
  module's lower-case state (`"failed"`, `"pending"`, `"success"`) on an unconfirmed outcome.
- **Account status (definitive):** `REGISTRATION_NOT_COMPLETE`, `BILLING_IS_UNPAID`,
  `EINVOICE_ACCOUNT_NOT_FOUND` in `details.code` (SePay's code, docs: "gọi lại không làm thay
  đổi kết quả"). Finish the registration, pay, or correct `provider_account_id` at my.sepay.vn,
  then `retry`.

### Unconfirmed outcome

A check whose message is exactly "Hệ thống chưa xác nhận được kết quả phát hành với nhà cung
cấp hóa đơn" (docs `theo-doi-trang-thai-xuat-hoa-don`: "Không gửi lại yêu cầu - hãy liên hệ
SePay") opens a case with `details.unconfirmed = true`. `retry` and `attach_draft` are refused
until a lookup shows the invoice issued or in progress. Contact SePay, then `mark_external` (SePay
confirms it was issued; copy the number and the issue date) or `waive`.

Known limitation: on a `Pending` or `Success` check the message text is not stored
(`provider_message` is absent and the `invoice_document_failed` log shows
`provider_message_length = null`); the code alone says it was the documented sentence. Only a
`Failed` check stores the message.

If a late unconfirmed answer arrives after the document already went to `PROVIDER_TIMEOUT`, the
lease fencing discards it; a `retry` whose lookup finds nothing queues the document again.

### Unknown outcome

`DOCUMENT_EXISTED_UNRESOLVED`, `PROVIDER_TIMEOUT` and a lookup that found nothing after an
error: the document stays in review; the module **never** changes the reference by itself and has
no time limit. Reconcile the old reference at the SePay portal, then call
`retry_with_new_reference` with a `note` recording what you found (required; the database
refuses a blank note). The module checks once more that the lookup finds nothing right before
the change. A `PROVIDER_TIMEOUT` with `details.last_poll_error = "transient_error"` means the
last checks got no usable answer (for example a check that kept echoing another reference).
`STALE_REFERENCE_FOUND` (the sweep found a record under a replaced reference): settle it at the
portal following the accountant's table below, then `acknowledge`.

### Cancelled record

A lookup that reports `cancelled` opens `DOCUMENT_EXISTED_UNRESOLVED` with `provider_state =
"cancelled"`. `retry` and `attach_draft` are refused (`provider_record_cancelled`), and
`retry_with_new_reference` is refused by construction (`ReferenceStillExists`). In v0.1 the way
out is `mark_external` (then `MarkCancelledExternally` if the invoice was cancelled after it was
issued) or `waive`; a replacement invoice is v0.2.

### Error codes (SePay docs `xu-ly-loi` and the create page)

The module decides on `error.code` and the HTTP status, never on the message text (docs: "Xử
lý lỗi dựa trên error.code, không khớp theo chuỗi error.message").

| HTTP and code | Module action |
|---|---|
| `400 ISSUED_DATE_INVALID` | Lookup, then one resend with today's date (above) |
| `400 VALIDATION_ERROR` | `PROVIDER_REJECTED` (`validation_error`); fix the data, `attach_draft` |
| `400 SELLER_STORE_NOT_FOUND` | `PROVIDER_REJECTED` (`seller_store_invalid`) |
| `400 SELLER_STORE_INACTIVE` | `PROVIDER_REJECTED` (`seller_store_invalid`) |
| `422 INVOICE_DATE_BEFORE_TAX_APPROVAL` | `PROVIDER_REJECTED` (`registration_not_approved`); production only |
| `422 REGISTRATION_NOT_TAX_APPROVED` | `PROVIDER_REJECTED` (`registration_not_approved`) |
| `409 EINVOICE_DOCUMENT_EXISTED` | Every `409` on a create: lookup by reference; never success by itself |
| `500 QUOTA_HAS_BEEN_USERD_UP` | `QUOTA_EXHAUSTED`, no automatic resend |
| `500 REGISTRATION_NOT_COMPLETE` | `PROVIDER_REJECTED`, account status |
| `500 BILLING_IS_UNPAID` | `PROVIDER_REJECTED`, account status |
| `500 EINVOICE_ACCOUNT_NOT_FOUND` | `PROVIDER_REJECTED`, account status |
| `401` (`TOKEN_EXPIRED`; docs `xu-ly-loi`: "Token quá 24 giờ. Lấy token mới") | New token; on a create no resend in the same claim, and the POST counts |
| `5xx` without a known code | Transient: retry later, lookup first |
| `404 EINVOICE_DOCUMENT_NOT_FOUND` on a lookup | No invoice under the reference (the code SePay really returns), but not proof the create was never received: the lookup does not see a job that is still pending, so before any resend the job is checked by reference |
| `404 EINVOICE_CREATE_TRACKING_NOT_FOUND` on a status check (matched by code, also on a `500`) | No job under the reference: only this answer, after a lookup that found nothing, lets the resend through |
| `2xx` carrying `EINVOICE_DOCUMENT_EXISTED` | Not an acceptance: `PROVIDER_REJECTED` |

### Corrections after issue (accountant's table)

The SePay docs still cite Decree 123/2020/NĐ-CP and Circular 78/2021/TT-BTC; this module cites
Decree 254/2026/NĐ-CP and Circular 91/2026/TT-BTC. Have the accountant confirm before going live.
The table below is the accountant's answer, reviewed on 29/10/2026 against Decree 254/2026/NĐ-CP
Article 21 and Circular 91/2026/TT-BTC Article 10 (unchanged from 20/10/2026). In v0.1 the module
issues neither adjustments nor replacements: do them at the SePay portal (v0.2 adds them).

| Error on an issued invoice | Treatment | Notice | Deadline |
|---|---|---|---|
| Wrong buyer name or address (tax code correct) | Adjustment | Form 04 | Within the month |
| Wrong buyer tax code | Adjustment | Form 04 | Within the month |
| Wrong tax rate or tax amount | Replacement | Form 04 | Within the month |
| Wrong unit price, quantity or total | Replacement | Form 04 | Within the month |
| Buyer wants a company invoice after a "no invoice requested" one | Replacement | Form 04 | Within the month |

## 10. Cancellation

The module has no cancellation API. Cancel at the SePay portal, then record it:

```python
async def record_cancellation(invoice, document_id, actor: str) -> None:
    await invoice.mark_cancelled_externally.execute(document_id, "cancelled at the portal", actor)
```

Only an `issued` document can be marked (`CancellationNotAllowed` otherwise); the actor and the
reason are recorded.

## 11. Artifacts and backup

The artifact loop stores the PDF and the XML through
[`FileArtifactStore`](../src/invoice_module/adapters/storage/file_store.py) under the root you
pass, records `file://` URIs and the sha256 of both on the document, and never overwrites a file.

- The **signed XML is the legal original** of the e-invoice; the PDF is only a rendering (Decree
  254/2026/NĐ-CP Article 5, stored as a data message). The module downloads and hashes both.
- Keep them for 10 years (the accountant's answer). Back up the artifact root regularly with
  the host's own tools, and keep a copy outside the application server.
- Files are created with mode `0600`, owned by the worker's user: run the backup as that user
  (or give it read access through your own means), and keep the root on a filesystem that keeps
  ownership.
- An invoice issued without its files for over 1 hour raises `invoice_artifacts_missing_total`.

## 12. Alerts and coverage queries

Metrics go to the `MetricsSink` you pass (`increment` with tags). Alert on:

| Metric or log | Meaning |
|---|---|
| `invoice_review_overdue_total` | A review open for more than 1 business day (SLO) |
| `invoice_approval_overdue_total` | A `manual` draft not approved 1 business day after `settled_at` (same business-day count as the review SLO) |
| `invoice_documents_stale_total` (log `invoice_document_stale`) | A document stuck in a working status for over 15 minutes |
| `invoice_quota_exhausted_total` | A create answered with the quota code |
| `invoice_artifacts_missing_total` | An issued invoice without its files after 1 hour |
| `invoice_stale_reference_found_total` | A replaced reference produced a record |
| log `invoice_document_failed` (warning) | Every document that goes to `failed`, with `error_code`, `reason`, `provider_state` and `provider_message_length` |
| `worker_loop_errors_total` | A worker step raised |

A document out of quota stays `queued` or `approved` with no next attempt, so the stale alert
fires on every sweep until the quota is topped up and the operator retries: this is expected,
not a fault.

Coverage queries (default prefixes `pm_` and `inv_`):

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

- Before 1 January 2027, check that the SePay account has the new year's series matching
  `invoice_series_template` (register checks this year's series only).
- **Quota.** The sandbox quota counter did not move (it stayed at 999 across drafts and issued
  invoices), so how drafts and issued invoices consume the quota is **unmeasured**. The SePay
  docs say "Hóa đơn nháp không tính vào hạn ngạch"; this module could not verify it (and issues
  no provider drafts). Watch `invoice_quota_exhausted_total` on Live and check `GET /v1/usage`
  before a large batch (docs: "Kiểm tra số lượt còn lại bằng API kiểm tra hạn mức trước khi xuất
  hóa đơn").
- **Log policy.** The adapter and the worker log only the method, the path, the HTTP status,
  the error code, the job status and the length of a message. SePay's text is stored only in
  `review_cases.details.provider_message`.

## 13. Known limitations

- No cancellation, adjustment or replacement through the API in v0.1 (cancel at the portal,
  section 10; adjustment and replacement are v0.2).
- Artifacts on the local filesystem only (no S3); one provider (SePay eInvoice).
- No invoice for refunds or partial payments; no periodic invoicing of prepaid multi-period
  services.
- No approval user interface (the command line of section 14) and no "reject draft" product: a
  wrong draft is held and goes to review.
- Drafts live only in the module; nothing is drafted at SePay. One delivery mode per order.
- In `manual` mode the issue date is the day the module sends the invoice after approval
  (usually the approval day). The user and the accountant accepted this; checking it against
  Decree 254/2026/NĐ-CP Article 9, clause 2 is the accountant's responsibility.
- The unconfirmed message is not stored on `Pending` or `Success` checks (section 9).

## 14. Approve manual-delivery invoices

Accounting uses the command line, on the database only (no provider call, no credential):

```sh
export INVOICE_DATABASE_URL="postgresql+asyncpg://..."  # or pass --database-url
python -m invoice_module.cli approvals list
python -m invoice_module.cli approvals show DOCUMENT_ID
python -m invoice_module.cli approvals approve DOCUMENT_ID --actor "accountant-name" --expect-sha256 SHA256_FROM_SHOW
python -m invoice_module.cli approvals hold DOCUMENT_ID --actor "accountant-name" --note "wrong buyer tax code"
```

- `list` prints one JSON line per draft waiting for approval (`document_id`, `settlement_id`,
  `host_ref`, `total_vnd`, `buyer_name`, `settled_at`, `age_hours`); `--overdue-only` keeps the
  drafts past the deadline, `--tenant-id` and `--limit` narrow it.
- `show` prints the whole draft (buyer, every line with its tax rate and tax, the total) and its
  `snapshot_sha256`. Read it before approving.
- `approve` binds the approval to exactly the content shown: pass `--expect-sha256` from `show`;
  if the content changed since, the result is `not_allowed:snapshot_changed` and you approve
  again. The actor and the time go to `approved_by` and `approved_at`.
- **The issue date is the day the module sends the invoice to SePay after approval.** The
  module freezes the date just before sending, in Asia/Saigon time, and never sends an old date;
  `approved_at` is kept separately. SePay sees the invoice only after approval.
- A wrong draft: `hold` it (only before sending starts), have the host submit the corrected
  content, `attach_draft` in the review queue (section 9), then approve again.
- Results are JSON lines (`approved`, `already_approved`, `held`, `not_allowed:<reason>`,
  `in_flight`). Exit codes: `0` all approved (or held), `3` some document could not be, `2` a usage
  error (missing `--actor` or `--note`, a blank value, a misspelt option), `1` the database
  could not be used, or the command failed otherwise (only the error type is printed). The
  database URL and its password are never printed, not even in a usage error.
- The overdue alert is in section 12.
