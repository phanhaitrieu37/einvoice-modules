from pathlib import Path

from tools.einvoice_probe.envfile import load_env_file, preflight


def test_load_env_file_ignores_comments_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "probe.env"
    path.write_text(
        "# comment\nexport SEPAY_EINVOICE_ENVIRONMENT='test'\n"
        'SEPAY_EINVOICE_SANDBOX_CLIENT_ID="client value" # inline\n'
        "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET=secret\n",
        encoding="utf-8",
    )

    assert load_env_file(path) == {
        "SEPAY_EINVOICE_ENVIRONMENT": "test",
        "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "client value",
        "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "secret",
    }


def test_preflight_prints_presence_only() -> None:
    env = {
        "SEPAY_EINVOICE_ENVIRONMENT": "test",
        "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "private-client-id",
        "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "private-client-secret",
    }

    output = "\n".join(preflight(env))

    assert output == (
        "SEPAY_EINVOICE_ENVIRONMENT: present\n"
        "SEPAY_EINVOICE_SANDBOX_CLIENT_ID: present\n"
        "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET: present\n"
        "environment: test"
    )
    assert "private-" not in output
