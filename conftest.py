# pytest configuration for duckdb test suites — core or any extension.
# For a full description of options, parallelism, and plugin internals see pytest.ini.

import os
import sys

import pytest

# Anchor on the EXTENSION root. abspath (never realpath): when this conftest is a
# symlink into an extension, __file__ stays the symlink path, so test/py + scripts
# resolve to the extension's own dirs — not the SoT this file points at.
_ROOT = os.path.dirname(os.path.abspath(__file__))

# Make the driver framework, repo helpers, and generators importable. Conventional
# roots; isdir-guarded so missing ones are harmless across extensions.
for _sub in ("test/py", "scripts", "scripts/data_generator"):
    _p = os.path.join(_ROOT, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# Rewrite the plugin module's asserts before it is first imported (below), so
# registering it as a plugin doesn't warn about lost assertion rewriting.
pytest.register_assert_rewrite("driver.sqllogic")
from driver import register_options, find_binary, SqlLogicFile, has_driver, is_driver

# Register the framework as a plugin so ALL its pytest_* hooks fire automatically
# (pytest_collection_modifyitems, pytest_terminal_summary, …) — no per-hook re-export.
pytest_plugins = ["driver.sqllogic"]

_WORKING_DIR = _ROOT
_TEST_ROOT = os.path.join(_WORKING_DIR, "test")

collect_ignore = ["duckdb", "build"]


def pytest_addoption(parser):
    register_options(parser)


def pytest_configure(config):
    # run_paired() reads this to locate the binary and build relative test names
    config.sqllogic_working_dir = _WORKING_DIR


def pytest_collect_file(parent, file_path):
    if not str(file_path).startswith(_TEST_ROOT + os.sep):
        return None
    # a driver .py (same-stem .test exists) is collected as a normal pytest module
    # so its fixtures wrap the .test with Python initialize/finalize
    if file_path.suffix == ".py":
        if is_driver(file_path):
            return pytest.Module.from_parent(parent, path=file_path)
        return None
    if file_path.suffix != ".test":
        return None
    # a .test with a same-stem .py driver runs only via that driver (suppressed
    # here to avoid a double run)
    if has_driver(file_path):
        return None
    binary = find_binary(parent.config, _WORKING_DIR)
    return SqlLogicFile.from_parent(
        parent, path=file_path, binary=binary, working_dir=_WORKING_DIR
    )
