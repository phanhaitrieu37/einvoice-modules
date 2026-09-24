import json
import secrets
import time
from uuid import uuid4

import sqlalchemy as sa
from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.adapters.sepay.provider import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SePayProvider,
    sign,
)
from payment_module.adapters.sqlalchemy.tables import define_tables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.builder import build_payment_module
from payment_module.domain.enums import ConnectionStatus, Environment
from payment_module.ports.clock import SystemClock
from payment_module.secrets import EnvSecretResolver
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

_SECRET = secrets.token_hex(32)
_ACCOUNT_NUMBER = "0071000654321"


def pm_tables(prefix: str = "pm_"):
    metadata = sa.MetaData()
    return define_tables(metadata, prefix)


def build_payment(engine: AsyncEngine, tables, publisher):
    return build_payment_module(
        PaymentModuleConfig(),
        SqlAlchemyUnitOfWorkFactory(async_sessionmaker(engine, expire_on_commit=False), tables),
        {"sepay": SePayProvider()},
        EnvSecretResolver(environ={"SEPAY_WEBHOOK_SECRET": _SECRET}),
        SystemClock(),
        outbox_publisher=publisher,
        template_checklists={"sepay": SePayTemplateChecklist()},
    )


async def onboard_payment(module):
    tenant, admin = "tenant-test", "test-admin"
    merchant = await module.register_merchant.execute(tenant, "branch-test", admin)
    account = await module.register_receiving_account.execute(
        tenant, merchant.id, Environment.TEST, "VCB", _ACCOUNT_NUMBER, "Test seller", admin
    )
    connection = await module.register_connection.execute(
        tenant, merchant.id, "sepay", Environment.TEST, "env:SEPAY_WEBHOOK_SECRET", admin
    )
    await module.bind_connection_account.execute(tenant, connection.id, account.id, admin)
    draft = await module.create_reference_profile.execute(1, {"bill": "BILL"}, 6, "23456789", admin)
    ready = {
        item.key: True
        for item in SePayTemplateChecklist().checklist(connection, draft.profile).items
    }
    await module.record_connection_readiness.execute(
        tenant, connection.id, 1, Environment.TEST, ready, "synthetic-test-evidence", admin
    )
    await module.activate_reference_profile.execute(1, admin)
    await module.set_connection_status.execute(
        tenant, connection.id, ConnectionStatus.ACTIVE, admin, "test"
    )
    return merchant, account, connection


def signed_delivery(amount_vnd: int, code: str) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(
        {
            "id": int(uuid4().int % 800_000_000) + 100_000_000,
            "gateway": "VCB",
            "transactionDate": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accountNumber": _ACCOUNT_NUMBER,
            "subAccount": None,
            "code": code,
            "content": code,
            "transferType": "in",
            "transferAmount": amount_vnd,
            "referenceCode": f"FT{uuid4().int % 10**11}",
        }
    ).encode()
    timestamp = int(time.time())
    return raw, {
        "content-type": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(raw, _SECRET, timestamp),
    }


async def settle(module, connection, intent):
    raw, headers = signed_delivery(intent.amount.value, intent.payment_reference)
    ingested = await module.ingest_webhook.execute(connection.locator, raw, headers)
    await module.process_inbox.run_batch()
    return ingested
