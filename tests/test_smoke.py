"""Smoke tests — fast checks that backup_server is importable and that its
pure-logic helpers behave correctly.

These run without root, without a database, without php/tar/git — only
stdlib + pytest. They catch:
  - import-time NameErrors (e.g. missing symbol after a refactor)
  - broken config migration on old/new schemas
  - regressions in the topological restore ordering
  - regressions in the Moodle config.php parser
  - boundary bugs in _human_size / _human_time

Run:  pytest tests/ -v
      make test
"""

import os

import pytest

import backup_server as bs


# ──────────────────────────────────────────────────────────────────────
# module import / public surface
# ──────────────────────────────────────────────────────────────────────
def test_module_imports():
    """Import succeeded (the rest of this file wouldn't run otherwise)."""
    assert bs.__name__ == "backup_server"


def test_public_symbols_exist():
    """Every function the menu/CLI rely on must still exist."""
    required = [
        # entry points
        "main", "run_menu",
        # operations
        "backup", "restore", "restore_previous",
        "update_moodle", "update_moodle_code", "upgrade_moodle",
        "set_maintenance", "run_cron", "setup_cron_job", "show_status",
        # config
        "load_config", "save_config", "get_config_path",
        "load_server_config", "_migrate_config", "_deep_merge",
        # helpers
        "_server_restore_order", "_parse_config_var",
        "_human_size", "_human_time", "_scan_tree",
        # db
        "get_db_client", "get_db_dump",
        "database_exists", "create_database_if_not_exists",
        "grant_database_privileges",
        # php setup
        "detect_os_family", "detect_chroot",
        "_php_modules", "_missing_php_extensions",
    ]
    missing = [n for n in required if not hasattr(bs, n)]
    assert not missing, f"missing symbols: {missing}"


# ──────────────────────────────────────────────────────────────────────
# config loading (module was imported with a temp $HOME)
# ──────────────────────────────────────────────────────────────────────
def test_config_loads():
    """CONFIG is a dict with all required top-level keys."""
    cfg = bs.CONFIG
    assert isinstance(cfg, dict)
    for key in ("version", "php_bin", "web_parent", "db_charset",
                "db_collation", "writable_dirs", "servers"):
        assert key in cfg, f"CONFIG missing '{key}'"


def test_config_path_lives_in_home():
    """get_config_path points inside $HOME and ends with the hidden filename."""
    path = bs.get_config_path()
    assert path.endswith(".moodle_ninja_config.json")
    assert os.path.dirname(path) == os.environ["HOME"]
    assert not os.path.isdir(path)


# ──────────────────────────────────────────────────────────────────────
# _deep_merge
# ──────────────────────────────────────────────────────────────────────
def test_deep_merge_flat():
    a = {"x": 1, "y": 2}
    bs._deep_merge(a, {"y": 99, "z": 3})
    assert a == {"x": 1, "y": 99, "z": 3}


def test_deep_merge_nested():
    a = {"srv": {"host": "old", "port": 1}}
    bs._deep_merge(a, {"srv": {"port": 2}})
    assert a == {"srv": {"host": "old", "port": 2}}


# ──────────────────────────────────────────────────────────────────────
# _migrate_config — the schema shim that keeps old configs working
# ──────────────────────────────────────────────────────────────────────
def test_migrate_old_schema():
    """primary_server / secondary_server become per-server flags."""
    cfg = {
        "primary_server": "alpha",
        "secondary_server": "beta",
        "servers": {
            "alpha": {"config_file": "/a/config.php"},
            "beta":  {"config_file": "/b/config.php"},
        },
    }
    bs._migrate_config(cfg)
    assert "primary_server" not in cfg
    assert "secondary_server" not in cfg
    assert cfg["version"] == 2
    assert cfg["servers"]["alpha"]["backup_web_root"] is True
    assert cfg["servers"]["beta"]["backup_web_root"] is False
    assert cfg["servers"]["beta"]["clone_web_root_from"] == "alpha"


def test_migrate_drops_placeholders_when_real_servers_exist():
    cfg = {"servers": {
        "example-a": {"backup_web_root": True},
        "example-b": {"backup_web_root": False,
                      "clone_web_root_from": "example-a"},
        "real":      {"config_file": "/r/config.php"},
    }}
    bs._migrate_config(cfg)
    assert set(cfg["servers"].keys()) == {"real"}


def test_migrate_keeps_placeholders_on_first_run():
    """A fresh install keeps example-a/example-b so the schema is discoverable."""
    cfg = {"servers": {
        "example-a": {"backup_web_root": True},
        "example-b": {"backup_web_root": False},
    }}
    bs._migrate_config(cfg)
    assert set(cfg["servers"].keys()) == {"example-a", "example-b"}


def test_migrate_strips_null_clone_source():
    cfg = {"servers": {"x": {"backup_web_root": True,
                              "clone_web_root_from": None}}}
    bs._migrate_config(cfg)
    assert "clone_web_root_from" not in cfg["servers"]["x"]


def test_migrate_is_idempotent():
    cfg = {
        "primary_server": "p",
        "secondary_server": "s",
        "servers": {"p": {}, "s": {}},
    }
    bs._migrate_config(cfg)
    snapshot = {k: dict(v) for k, v in cfg["servers"].items()}
    bs._migrate_config(cfg)          # run again
    for k in snapshot:
        assert cfg["servers"][k] == snapshot[k]


# ──────────────────────────────────────────────────────────────────────
# _parse_config_var — Moodle config.php parsing
# ──────────────────────────────────────────────────────────────────────
def test_parse_literal_single_quotes():
    assert bs._parse_config_var("$CFG->dbname = 'psc';", "dbname") == "psc"


def test_parse_literal_double_quotes():
    assert bs._parse_config_var('$CFG->dbuser = "udaya";', "dbuser") == "udaya"


def test_parse_variable_reference():
    """$CFG->dbname = $app_name;  →  resolve $app_name = 'psc';"""
    src = "$app_name = 'psc';\n$CFG->dbname = $app_name;\n"
    assert bs._parse_config_var(src, "dbname") == "psc"


def test_parse_missing_returns_none():
    assert bs._parse_config_var("$CFG->foo = 'bar';", "missing") is None


def test_parse_empty_returns_none():
    assert bs._parse_config_var("", "dbname") is None


# ──────────────────────────────────────────────────────────────────────
# _server_restore_order — topological sort
# ──────────────────────────────────────────────────────────────────────
def test_restore_order_no_deps():
    order = bs._server_restore_order({"a": {}, "b": {}, "c": {}})
    assert set(order) == {"a", "b", "c"}
    assert len(order) == 3


def test_restore_order_linear():
    servers = {
        "a": {},
        "b": {"clone_web_root_from": "a"},
        "c": {"clone_web_root_from": "b"},
    }
    assert bs._server_restore_order(servers) == ["a", "b", "c"]


def test_restore_order_independent_preserves_insertion_order():
    servers = {"x": {}, "y": {}, "z": {}}
    assert bs._server_restore_order(servers) == ["x", "y", "z"]


def test_restore_order_diamond():
    """a -> b, a -> c, b -> d"""
    servers = {
        "a": {},
        "b": {"clone_web_root_from": "a"},
        "c": {"clone_web_root_from": "a"},
        "d": {"clone_web_root_from": "b"},
    }
    order = bs._server_restore_order(servers)
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")


def test_restore_order_cycle_warns_but_does_not_crash(capsys):
    servers = {
        "a": {"clone_web_root_from": "b"},
        "b": {"clone_web_root_from": "a"},
    }
    order = bs._server_restore_order(servers)   # must not raise
    assert set(order) == {"a", "b"}
    captured = capsys.readouterr()
    assert "Circular" in (captured.out + captured.err)


def test_restore_order_missing_source_warns(capsys):
    servers = {"a": {"clone_web_root_from": "ghost"}}
    order = bs._server_restore_order(servers)
    assert order == ["a"]
    captured = capsys.readouterr()
    assert "ghost" in (captured.out + captured.err)


# ──────────────────────────────────────────────────────────────────────
# _human_size / _human_time — boundary formatting
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n,expected_unit", [
    (0,              "B"),
    (1023,           "B"),
    (1024,           "KiB"),
    (1024 * 1024,    "MiB"),
    (1024 ** 3,      "GiB"),
])
def test_human_size_units(n, expected_unit):
    assert bs._human_size(n).endswith(expected_unit)


def test_human_time_sub_minute():
    assert bs._human_time(5.0) == "5.0s"
    assert bs._human_time(59.9).endswith("s")


def test_human_time_minutes():
    assert bs._human_time(125) == "2m5s"


def test_human_time_hours():
    assert bs._human_time(3700) == "1h1m40s"


# ──────────────────────────────────────────────────────────────────────
# load_server_config — config.php tolerance
# ──────────────────────────────────────────────────────────────────────
def test_load_server_config_tolerates_missing_file(tmp_path, monkeypatch):
    """require_config_file=False must not raise on a missing config.php
    (this is what restore() relies on — config.php lands after web root)."""
    fake = {
        "config_file": str(tmp_path / "missing.php"),
        "backup_web_root": True,
    }
    monkeypatch.setitem(bs.SERVERS, "test-srv", fake)

    result = bs.load_server_config("test-srv", require_config_file=False)
    assert result["config_file"] == str(tmp_path / "missing.php")
    assert result["db_user"] == ""    # no config.php → no creds parsed


# ──────────────────────────────────────────────────────────────────────
# detect helpers — shape only, no system assertions
# ──────────────────────────────────────────────────────────────────────
def test_detect_os_family_shape():
    assert bs.detect_os_family() in (None, "debian", "rhel")


def test_detect_chroot_shape():
    is_chroot, detail = bs.detect_chroot()
    assert isinstance(is_chroot, bool)
    assert isinstance(detail, str)
