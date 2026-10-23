"""Pytest configuration — isolate $HOME before importing backup_server.

backup_server calls load_config() at module import time, which reads (or
creates) ~/.moodle_ninja_config.json. Without isolation, running the test
suite would touch the real user's config file.
"""

import os
import tempfile


def pytest_configure(config):
    """Runs before any test module is imported — redirect $HOME and clear
    SUDO_USER (which would bypass $HOME)."""
    if os.environ.get("_MOODLE_TEST_HOME_ISOLATED"):
        return
    home = tempfile.mkdtemp(prefix="moodle_test_home_")
    os.environ.pop("SUDO_USER", None)
    os.environ["HOME"] = home
    os.environ["_MOODLE_TEST_HOME_ISOLATED"] = "1"
