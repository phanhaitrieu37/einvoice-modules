# Changelog

Every release lists its changes under four contracts: Public API, Event consumption, DB schema
and Known limitations. In 0.x a breaking change to any of them bumps the minor version; fixes
and additive changes bump the patch version.

## 0.1.0 — TODO: release date (set when the tag is made)

First release of `invoice-module` (import `invoice_module`): one VAT e-invoice through SePay
eInvoice for every `PaymentSettled` of `payment-module`. The setup is in the
[getting started guide](docs/getting-started.md).

**Reproducible build.** `make build` sets `SOURCE_DATE_EPOCH` to the committer time of `HEAD`,
refuses to build when tracked files have uncommitted changes, and writes the wheel, the sdist
and `dist/SHA256SUMS`.

### Public API

- `build_invoice_module(uow_factory, clock, metrics, *, provider, credentials, artifact_store,
  config)` returns an `InvoiceModule`: submit and discard drafts, record settled payments, the
  worker steps (issue, poll, fetch artifacts, the overdue sweep), resolve reviews, approve and
  hold `manual` drafts, mark an invoice cancelled at the portal, register issuer accounts, set
  their status and record their readiness, and the queries for document status, open reviews
  and pending approvals. Without a provider the module still approves, holds and queries; the
  use cases that need the provider are `None`.
- `InvoiceWorker(module, WorkerIntervals(...)).run(stop_event)` runs the issue, poll, sweep and
  artifact loops until the event is set.
- Ports for the host: unit of work, clock, metrics, provider, credentials (`env:NAME`
  references through `EnvCredentialResolver`) and artifact store.
- Optional adapters behind extras: `sqlalchemy` (tables, repositories, unit of work,
  migration), `postgres` (asyncpg), `sepay` (the SePay eInvoice adapter, sandbox base URL for
  the `test` environment), `payment` (the `payment-module` integration). Artifacts are stored
  by `FileArtifactStore` on the local filesystem.
- Command line, database only: `python -m invoice_module.cli approvals list|show|approve|hold`.
  v0.1 ships only `approvals`. Accounting can put a draft on hold there, but releasing a hold
  is a review resolution, which needs a process built with the provider.
- Money is `int` VND and quantities are `Decimal` end to end; a `float` raises `TypeError`.

### Event consumption

- `PaymentSettledConsumer` consumes `PaymentSettled` with `schema_version = 1` from the
  `payment-module` outbox, behind `FanOutPublisher` next to the host's own consumers. Delivery
  is at least once: a redelivered `event_id` records nothing new, and a settlement has exactly
  one original document whatever the number of deliveries or workers.
- Any other schema version raises `UnsupportedEventVersion` and nothing is recorded; other
  event types are ignored.
- Every settlement gets an original document. Its total is the settled `amount_vnd`; its
  provider reference is the `settlement_id`, written before any network call and never
  changed to retry.
- `SubmitDraftCommand.delivery_mode` is required (keyword-only, `auto` or `manual`): `auto`
  issues as soon as the money arrives, `manual` keeps the draft in the module until accounting
  approves it.

### DB schema

- `schema_v1`: five tables with the `inv_` prefix (`inv_issuer_accounts`, `inv_invoice_drafts`,
  `inv_documents`, `inv_settlement_receipts`, `inv_review_cases`), defined by
  `define_tables(metadata, prefix="inv_")` and created by the frozen migration
  `schema_v1.upgrade`. No foreign key points at a `pm_` table.
- The schema is frozen from this release on. A later change ships as `schema_v2` with an
  upgrade step, never as an edit to `schema_v1`.

### Known limitations

- No cancellation through the API: an invoice is cancelled at the provider's portal and
  recorded with `mark_cancelled_externally`. Adjustment and replacement invoices come in 0.2.
- Verified on the SePay eInvoice sandbox only; the Live environment has not been exercised.
- The module relies on SePay returning the reference code as the `tracking_code` of a create.
  Every sandbox create did, but the create docs do not promise it. Any other code is never taken
  as an acceptance, so nothing is issued twice, but such an invoice may need review and its
  status check and download may not find it; the adapter logs `sepay_tracking_code_mismatch`.
  Check the first Live invoice (getting started, sections 5 and 13).
- Issue latency on the sandbox (settlement to issued, `auto` mode, 20 settlements):
  TODO: p95 measured in the sandbox run, not yet recorded.
- Readiness scenario s3 (an asynchronous `Failed` job) is `INCONCLUSIVE`: the sandbox could not
  be made to produce one. It is accepted on the evidence of the fake-provider tests of the
  failed-job path.
- `manual` mode has no user interface (the command line above) and no "reject draft" action: a
  wrong draft is held and goes to review.
- A second settlement for the same order is never invoiced on its own: it stays `blocked` with
  a `DUPLICATE_SETTLEMENT` review until an operator retries (invoice it) or waives it (refund
  or offset).
- The actor of an issuer account status change, and the actor who records an account's
  readiness evidence (log `invoice_issuer_readiness_recorded`), are written to the log only;
  storing them in the database waits for `schema_v2` in 0.2.
- Artifacts on the local filesystem only; one provider (SePay eInvoice).
