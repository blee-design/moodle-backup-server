"""Smoke test — verifies backup_server imports without NameError."""

def test_module_imports():
    import backup_server  # noqa: F401
