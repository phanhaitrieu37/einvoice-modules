"""Artifact loop: store the PDF and XML of issued invoices; a failed download never changes
``issued``."""

import hashlib
import logging
from dataclasses import dataclass
from uuid import UUID

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow
from invoice_module.application.config import InvoiceModuleConfig, retry_delay
from invoice_module.domain.enums import IssueErrorCode
from invoice_module.ports.artifacts import ArtifactStore
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.provider import EInvoiceProvider
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactResult:
    document_id: UUID
    outcome: str


def artifact_key(document: DocumentRow, extension: str) -> str:
    return (
        f"{document.tenant_id}/{document.environment}/{document.issuer_account_id}/"
        f"{document.id}.{extension}"
    )


class FetchArtifacts:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        provider: EInvoiceProvider,
        credentials: CredentialResolver,
        store: ArtifactStore,
        clock: Clock,
        metrics: MetricsSink | None = None,
        config: InvoiceModuleConfig | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._credentials = credentials
        self._store = store
        self._clock = clock
        self._metrics = metrics or NoOpMetricsSink()
        self._config = config or InvoiceModuleConfig()
        self._owner = self._config.owner()

    async def run_batch(self, limit: int | None = None) -> list[ArtifactResult]:
        async with self._uow_factory() as uow:
            claimed = await uow.documents.claim_artifacts_due(
                self._clock.now(),
                self._config.artifact_lease_seconds,
                self._owner,
                limit or self._config.batch_size,
            )
            await uow.commit()
        return [await self._fetch(document) for document in claimed]

    async def _fetch(self, document: DocumentRow) -> ArtifactResult:
        try:
            assert document.issuer_account_id is not None
            async with self._uow_factory() as uow:
                account = await uow.issuer_accounts.get_view(document.issuer_account_id)
            assert account is not None and document.provider_invoice_id is not None
            creds = await self._credentials.client_credentials(account.credential_ref)
            artifacts = await self._provider.fetch_artifacts(
                account, creds, document.reference_code, document.provider_invoice_id
            )
            pdf_uri = await self._store.put(
                artifact_key(document, "pdf"), artifacts.pdf, "application/pdf"
            )
            xml_uri = await self._store.put(
                artifact_key(document, "xml"), artifacts.xml, "application/xml"
            )
        except Exception as exc:
            logger.warning(
                "invoice_artifact_fetch_failed",
                extra={"document_id": str(document.id), "error": type(exc).__name__},
            )
            increment_safely(self._metrics, "invoice_artifact_fetch_failed_total", {})
            now = self._clock.now()
            async with self._uow_factory() as uow:
                recorded = await uow.documents.finalize(
                    document.id,
                    document.lease_generation,
                    last_error_code=IssueErrorCode.ARTIFACT_FETCH_FAILED.value,
                    next_attempt_at=now + retry_delay(document.attempts),
                    updated_at=now,
                )
                await uow.commit()
            return ArtifactResult(document.id, "backoff" if recorded else "lease_lost")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            recorded = await uow.documents.finalize(
                document.id,
                document.lease_generation,
                pdf_uri=pdf_uri,
                xml_uri=xml_uri,
                pdf_sha256=hashlib.sha256(artifacts.pdf).hexdigest(),
                xml_sha256=hashlib.sha256(artifacts.xml).hexdigest(),
                last_error_code=None,
                next_attempt_at=None,
                updated_at=now,
            )
            await uow.commit()
        if not recorded:
            increment_safely(self._metrics, "invoice_lease_lost_total", {"loop": "artifacts"})
            return ArtifactResult(document.id, "lease_lost")
        return ArtifactResult(document.id, "stored")
