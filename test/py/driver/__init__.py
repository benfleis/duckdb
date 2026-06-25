"""The generic pytest driver framework for duckdb test suites.

Layout, model, and extension (symlink) setup: see README.md in this package.

Public API re-exported here so conftest (and drivers) can `from driver import ...`.
The pytest hooks live in `driver.sqllogic` and are registered via
`pytest_plugins = ["driver.sqllogic"]` in the rootdir conftest.
"""
from .sqllogic import (  # noqa: F401
    register_options,
    find_binary,
    SqlLogicFile,
    has_driver,
    is_driver,
    run_paired,
)
