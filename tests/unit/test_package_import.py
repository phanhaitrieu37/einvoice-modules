def test_package_imports_and_has_version() -> None:
    import payment_module

    import invoice_module

    assert invoice_module.__version__ == "0.1.0"
    assert payment_module.__version__ == "0.1.0"
