import pytest

from invoice_module.ports.provider import ProviderConfigError
from invoice_module.secrets import EnvCredentialResolver


async def test_resolves_client_id_and_secret() -> None:
    resolver = EnvCredentialResolver(
        {"SYN_SEPAY_CLIENT_ID": "synthetic-id", "SYN_SEPAY_CLIENT_SECRET": "synthetic-secret"}
    )
    creds = await resolver.client_credentials("env:SYN_SEPAY")
    assert creds.client_id == "synthetic-id"
    assert creds.client_secret == "synthetic-secret"
    assert "synthetic-secret" not in repr(creds)


async def test_reads_environment_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = EnvCredentialResolver()
    monkeypatch.setenv("SYN_LATE_CLIENT_ID", "late-id")
    monkeypatch.setenv("SYN_LATE_CLIENT_SECRET", "late-secret")
    assert (await resolver.client_credentials("env:SYN_LATE")).client_id == "late-id"


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        ({"SYN_SEPAY_CLIENT_SECRET": "synthetic-secret-value"}, "SYN_SEPAY_CLIENT_ID"),
        ({"SYN_SEPAY_CLIENT_ID": "synthetic-id-value"}, "SYN_SEPAY_CLIENT_SECRET"),
        (
            {"SYN_SEPAY_CLIENT_ID": "synthetic-id-value", "SYN_SEPAY_CLIENT_SECRET": "  "},
            "SYN_SEPAY_CLIENT_SECRET",
        ),
        (
            {"SYN_SEPAY_CLIENT_ID": "", "SYN_SEPAY_CLIENT_SECRET": "synthetic-secret-value"},
            "SYN_SEPAY_CLIENT_ID",
        ),
    ],
)
async def test_missing_variable_names_variable_only(environ: dict[str, str], missing: str) -> None:
    with pytest.raises(ProviderConfigError) as raised:
        await EnvCredentialResolver(environ).client_credentials("env:SYN_SEPAY")
    message = str(raised.value)
    assert missing in message
    assert "synthetic-id-value" not in message
    assert "synthetic-secret-value" not in message


@pytest.mark.parametrize("ref", ["vault:SYN_SEPAY", "SYN_SEPAY", "env:", "env:   ", "ENV:SYN"])
async def test_rejects_non_env_scheme(ref: str) -> None:
    resolver = EnvCredentialResolver(
        {"SYN_SEPAY_CLIENT_ID": "synthetic-id", "SYN_SEPAY_CLIENT_SECRET": "synthetic-secret"}
    )
    with pytest.raises(ValueError):
        await resolver.client_credentials(ref)
