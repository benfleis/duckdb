"""
Reusable pytest collector for DuckDB SQLLogic tests.

Each .test file maps to one SqlLogicItem; the item invokes the extension's
Catch2-based test binary with the test path as a name filter.

Binary selection (in priority order):
  --catch2-binary PATH     explicit path to any Catch2-compatible binary
  $BUILD_DIR               env var; binary resolved as $BUILD_DIR/test/unittest
  --build debug|release|reldebug  (default: debug) → build/TYPE/test/unittest

Batching (--batch-size N):
  Consecutive SqlLogicItems are assigned a shared batch_id.  The first item
  in a batch runs the whole batch via `-f file --start-offset 0 --end-offset N`
  and caches per-test results.  Subsequent items in the batch read the cache.
  On batch failure the batch is re-run one test at a time so each item gets
  an accurate individual result.

  Each batch is marked with pytest.mark.xdist_group so xdist --dist=loadgroup
  routes all items in a batch to the same worker, keeping the per-process
  cache coherent and ensuring each batch is invoked only once.

Usage in an extension's conftest.py:

    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "test", "pytest"))
    from sqllogic import register_options, find_binary, SqlLogicFile
    from sqllogic import pytest_collection_modifyitems    # noqa: F401

    _WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
    _TEST_ROOT   = os.path.join(_WORKING_DIR, "test")

    collect_ignore = ["duckdb", "build"]

    def pytest_addoption(parser):
        register_options(parser)

    def pytest_collect_file(parent, file_path):
        if file_path.suffix != ".test":
            return None
        if not str(file_path).startswith(_TEST_ROOT + os.sep):
            return None
        binary = find_binary(parent.config, _WORKING_DIR)
        return SqlLogicFile.from_parent(parent, path=file_path,
                                        binary=binary, working_dir=_WORKING_DIR)
"""

import os
import shutil
import subprocess
import tempfile
import threading

import pytest

from mnemonic import run_id as _make_run_id

# ---------------------------------------------------------------------------
# Per-process batch cache (coherent within a worker; xdist_group keeps a
# batch on one worker, so this is always correct).
# ---------------------------------------------------------------------------

_batch_cache: dict = {}  # batch_id → {test_name: result_dict}
_batch_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Option registration
# ---------------------------------------------------------------------------


def register_options(parser):
    parser.addoption(
        "--build",
        default="debug",
        choices=["debug", "release", "reldebug"],
        help="Build type to test (default: debug). Resolves to "
        "build/{type}/test/unittest under the repo root, or "
        "$BUILD_DIR/test/unittest if $BUILD_DIR is set. "
        "Ignored when --catch2-binary is given.",
    )
    parser.addoption(
        "--catch2-binary",
        default=None,
        metavar="PATH",
        help="Explicit path to a Catch2-compatible test binary. "
        "Overrides --build and $BUILD_DIR. Use this when the binary "
        "lives outside the standard CMake build tree.",
    )
    parser.addoption(
        "--batch-size",
        default=10,
        type=int,
        metavar="N",
        help="Tests per unittest invocation (default: 10). Reduces subprocess "
        "overhead; use with -n for parallel batches.",
    )
    parser.addoption(
        "--external-test-dir",
        default=None,
        metavar="BASE",
        help="Caller-owned base dir passed to the binary's --external-test-dir. "
        "Tests run under BASE/<run-id>/<uuid> (run-id = timestamp--mnemonic, one per "
        "pytest run; <uuid> per binary invocation). Disposition of BASE/<run-id> is "
        "controlled by --external-test-dir-destroy. BASE may be local or remote (e.g. "
        "s3://). NOTE: the <uuid> is per-invocation, so a batch shares one TEST_DIR; "
        "use --batch-size 1 for a distinct dir per test.",
    )
    parser.addoption(
        "--external-test-dir-destroy",
        default="on-success",
        choices=["never", "on-success", "always"],
        metavar="{never,on-success,always}",
        help="Destroy disposition for the per-run external dir (BASE/<run-id>): "
        "never (NEVER_DESTROY) | on-success (MAY_DESTROY, default — remove only when the "
        "run has no failures) | always (ALWAYS_DESTROY — remove regardless). Currently "
        "owned by pytest; the same disposition may be pushed down into the binary.",
    )


def find_binary(config, working_dir):
    """Return binary path. Never returns None — callers check os.path.exists()."""
    explicit = config.getoption("--catch2-binary", default=None)
    if explicit:
        return os.path.abspath(explicit)
    build_dir = os.environ.get("BUILD_DIR")
    if not build_dir:
        build_type = config.getoption("--build", default="debug")
        build_dir = os.path.join(working_dir, "build", build_type)
    return os.path.join(build_dir, "test", "unittest")


# ---------------------------------------------------------------------------
# External test dir: per-run id ($INVOCATION level) + cleanup
#
# When --external-test-dir BASE is given, tests run under BASE/<run-id>/<uuid>:
#   <run-id>  = timestamp--mnemonic, ONE per pytest run (this level), and
#   <uuid>    = appended by the binary, one per invocation (the current C++).
# The run-id is computed once on the controller and shared to xdist workers so
# every worker writes under the same BASE/<run-id>. On a clean run it is removed.
# ---------------------------------------------------------------------------


def _run_id(config):
    """Return this run's id, cached on config; shared across xdist workers."""
    cached = getattr(config, "_sqllogic_run_id", None)
    if cached is not None:
        return cached
    wi = getattr(config, "workerinput", None)
    run_id = wi["sqllogic_run_id"] if wi and "sqllogic_run_id" in wi else _make_run_id()
    config._sqllogic_run_id = run_id
    return run_id


def _external_dir(config):
    """BASE/<run-id> for this run, or None if --external-test-dir was not given."""
    base = config.getoption("--external-test-dir", default=None)
    return os.path.join(base, _run_id(config)) if base else None


def pytest_configure_node(node):
    # xdist controller hook: hand each worker the controller's run-id so all
    # workers share one BASE/<run-id>.
    node.workerinput["sqllogic_run_id"] = _run_id(node.config)


def pytest_sessionfinish(session, exitstatus):
    # Controller-only: drop the per-run external dir on a clean run (no failures),
    # unless asked to keep it. Always kept on failure/interruption for debugging.
    config = session.config
    if getattr(config, "workerinput", None) is not None:
        return  # this is a worker
    destroy = config.getoption("--external-test-dir-destroy", default="on-success")
    if destroy == "never":
        return
    if destroy == "on-success" and int(exitstatus) != 0:
        return  # keep on failure/interruption for debugging
    run_dir = _external_dir(config)
    if run_dir and os.path.isdir(run_dir):  # isdir() also skips remote (s3://) bases
        shutil.rmtree(run_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Members, roles, and drivers
#
# A *test* is the set of same-stem *members* under test/** (e.g. foo.test +
# foo.py). Each member plays one or more *roles*:
#   - body   : holds test logic that is run — a `.test`/`.sql`, or a `.py` that
#              carries its own assertions (the py-exclusive case).
#   - driver : orchestrates execution (initialize/finalize) around a body — a `.py`.
# Roles are not file types: a `.py` can be a driver, a body, or BOTH (drive a
# same-stem body AND carry its own assertions). A `.test`/`.sql` is always a body.
#
# This module handles the *driver* case: a `.py` with a same-stem `.test` body.
# The `.py` is collected and drives that body through the binary via run_paired();
# the standalone body is suppressed (see conftest) so it runs only via its driver.
#
# CURRENT LIMITATION: the driving `.py` is the reported unit, not the body —
# flipping so the body is reported (driver only contributes hooks) is the pending
# rework. The py-as-body-only case (a `.py` with no same-stem body) is the
# deferred py-exclusive lane — not collected today (python_files is off).
# ---------------------------------------------------------------------------


def _stem_path(path, suffix):
    return os.path.splitext(str(path))[0] + suffix


def has_driver(body_path) -> bool:
    """True if this body (`.test`/`.sql`) has a same-stem `.py` driver."""
    return os.path.exists(_stem_path(body_path, ".py"))


def is_driver(py_path) -> bool:
    """True if this `.py` is a driver — it has a same-stem body (`.test`) to drive.

    A `.py` with no same-stem body is itself a body (the py-exclusive case), not a
    driver; that lane is not handled here yet.
    """
    return os.path.exists(_stem_path(py_path, ".test"))


def run_paired(request, *, external_test_dir=None):
    """Drive the calling driver `.py`'s same-stem body (`.test`) through the binary.

    Call from the driver's test function once initialization has run. Raises
    SqlLogicFailure on failure and pytest.skip on a skipped test, so the driver
    reflects the real SQL result. Pass external_test_dir to hand the binary a
    caller-owned base dir (--external-test-dir) that initialization already staged into.
    """
    working_dir = request.config.sqllogic_working_dir
    binary = find_binary(request.config, working_dir)
    test_name = os.path.relpath(_stem_path(request.path, ".test"), working_dir)
    args = [test_name]
    if external_test_dir is not None:
        args = ["--external-test-dir", str(external_test_dir), *args]
    _raise_for_result(_parse_result(_invoke(binary, args, working_dir)))


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class SqlLogicFile(pytest.File):
    """One .test file → one SqlLogicItem."""

    @classmethod
    def from_parent(cls, parent, *, binary, working_dir, **kwargs):
        obj = super().from_parent(parent, **kwargs)
        obj._binary = binary
        obj._working_dir = working_dir
        return obj

    def collect(self):
        test_name = os.path.relpath(str(self.path), self._working_dir)
        yield SqlLogicItem.from_parent(
            self,
            name=self.path.stem,
            test_name=test_name,
            binary=self._binary,
            working_dir=self._working_dir,
            external_test_dir=_external_dir(self.config),
        )


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class SqlLogicItem(pytest.Item):
    @classmethod
    def from_parent(cls, parent, *, test_name, binary, working_dir, external_test_dir=None, **kwargs):
        obj = super().from_parent(parent, **kwargs)
        obj._test_name = test_name
        obj._binary = binary
        obj._working_dir = working_dir
        obj._external_test_dir = external_test_dir
        obj._batch_id = None
        obj._batch_test_names = None
        return obj

    def runtest(self):
        if self._batch_id is not None:
            self._run_batched()
        else:
            self._run_single()

    # -- single-test path (batch_size == 1) ----------------------------------

    def _run_single(self):
        result = _invoke(
            self._binary, [self._test_name], self._working_dir, self._external_test_dir
        )
        _raise_for_result(_parse_result(result))

    # -- batch path (batch_size > 1) -----------------------------------------

    def _run_batched(self):
        with _batch_cache_lock:
            if self._batch_id not in _batch_cache:
                _batch_cache[self._batch_id] = _execute_batch(
                    self._batch_test_names, self._binary, self._working_dir,
                    self._external_test_dir,
                )
        r = _batch_cache[self._batch_id].get(
            self._test_name, {"status": "internal_error"}
        )
        _raise_for_result(r)

    # -- pytest protocol -------------------------------------------------

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, SqlLogicFailure):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, None, self._test_name


# ---------------------------------------------------------------------------
# Post-collection hook: assign batch IDs and xdist_group markers
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(session, config, items):
    # Dedupe by nodeid: a driver .py named explicitly on the CLI is collected
    # both natively and by our hook, and (later) a .test reachable via both the
    # filesystem scan and `unittest -l` overlaps. Keep the first of each.
    seen = set()
    deduped = []
    for it in items:
        if it.nodeid in seen:
            continue
        seen.add(it.nodeid)
        deduped.append(it)
    items[:] = deduped

    batch_size = config.getoption("--batch-size", default=10)
    if batch_size <= 1:
        return

    batch_id = 0
    i = 0
    while i < len(items):
        item = items[i]
        if not isinstance(item, SqlLogicItem):
            i += 1
            continue

        batch: list[SqlLogicItem] = []
        while (
            i < len(items)
            and isinstance(items[i], SqlLogicItem)
            and items[i]._binary == item._binary
            and items[i]._working_dir == item._working_dir
            and len(batch) < batch_size
        ):
            batch.append(items[i])
            i += 1

        test_names = [b._test_name for b in batch]
        for b in batch:
            b._batch_id = batch_id
            b._batch_test_names = test_names
            b.add_marker(pytest.mark.xdist_group(f"sqllogic_batch_{batch_id}"))

        batch_id += 1


# ---------------------------------------------------------------------------
# Terminal summary: aggregate skip reasons across the whole run
# ---------------------------------------------------------------------------


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Consolidate per-test skip reasons into one counted digest for the run.

    pytest's -rs lists every skipped test individually; for a big suite this groups
    them by reason (exact match) so a missing env var / extension is obvious at a glance.
    """
    from collections import Counter

    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    counts = Counter()
    for rep in skipped:
        longrepr = getattr(rep, "longrepr", None)
        reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
        counts[reason.removeprefix("Skipped: ")] += 1

    # yellow to match pytest's own skip coloring (markup is honored per --color)
    terminalreporter.write_sep("-", f"skipped: {len(skipped)} by reason", yellow=True, bold=True)
    for reason, n in counts.most_common():
        terminalreporter.write_line(f"  {n:>4}  {reason}", yellow=True)


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------


def _execute_batch(test_names: list, binary: str, working_dir: str,
                   external_test_dir: str = None) -> dict:
    """Run a batch.  On failure, re-run individually for per-test attribution."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        tmpfile = f.name
        for name in test_names:
            f.write(name + "\n")
    try:
        result = _invoke(
            binary,
            [
                "-f",
                tmpfile,
                "--start-offset",
                "0",
                "--end-offset",
                str(len(test_names)),
            ],
            working_dir,
            external_test_dir,
        )
    finally:
        os.unlink(tmpfile)

    if result.get("returncode", 1) == 0:
        # Batch succeeded. Skips are still attributable per-test via the
        # [DUCKDB_SKIP] markers the binary emits (one per skipped test).
        skips = _scan_batch_skips(result["stdout"] + result["stderr"])
        return {
            name: ({"status": "skip", "reason": skips[name]} if name in skips
                   else {"status": "pass"})
            for name in test_names
        }

    # Re-run individually so each item gets an accurate result.
    return {
        name: _invoke_single(name, binary, working_dir, external_test_dir)
        for name in test_names
    }


def _invoke_single(test_name: str, binary: str, working_dir: str,
                   external_test_dir: str = None) -> dict:
    result = _invoke(binary, [test_name], working_dir, external_test_dir)
    return _parse_result(result)


# ---------------------------------------------------------------------------
# Low-level subprocess helpers
# ---------------------------------------------------------------------------


def _invoke(binary: str, args: list, working_dir: str,
            external_test_dir: str = None) -> dict:
    if external_test_dir:
        # caller-owned base; the binary makes TEST_DIR = <base>/<uuid> per invocation
        args = ["--external-test-dir", str(external_test_dir), *args]
    try:
        proc = subprocess.run(
            [binary] + args,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"unittest binary not found: {binary}\n"
            "Build the extension first (e.g. make debug).",
        }
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", errors="replace"),
        "stderr": proc.stderr.decode("utf-8", errors="replace"),
    }


# Stable marker the unittest binary emits per skipped test (SQLLogicTestLogger::PrintSkip):
#   [DUCKDB_SKIP] <test_name> :: <reason>
_SKIP_MARKER = "[DUCKDB_SKIP]"


def _parse_result(result: dict) -> dict:
    """Classify a single-test invocation result as pass / skip / fail."""
    combined = result["stdout"] + result["stderr"]
    # The skip marker is authoritative (a skipped run still exits 0, but check it
    # first so it wins regardless of exit code).
    skips = _scan_batch_skips(combined)
    if skips:
        return {"status": "skip", "reason": next(iter(skips.values()))}
    if result["returncode"] != 0:
        return {"status": "fail", "output": combined}
    return {"status": "pass"}


def _raise_for_result(result: dict) -> None:
    # missing status is itself a harness bug, not a pass
    status = result.get("status", "internal_error")
    if status == "pass":
        return
    if status == "skip":
        pytest.skip(result.get("reason", "skipped"))
    if status == "fail":
        raise SqlLogicFailure(result.get("output", ""))
    # internal_error (e.g. test absent from batch results) or any unexpected
    # status: surface as a failure rather than silently passing
    raise SqlLogicFailure(
        result.get("output", f"internal error: unexpected status {status!r}")
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class SqlLogicFailure(Exception):
    pass


def _scan_batch_skips(output: str) -> dict:
    """Map test_name -> reason for every [DUCKDB_SKIP] marker in (batch) output.

    Because the binary emits one marker per skipped test, this attributes skips
    per-test even inside a single batched invocation.
    """
    skips = {}
    for line in output.splitlines():
        idx = line.find(_SKIP_MARKER)
        if idx == -1:
            continue
        rest = line[idx + len(_SKIP_MARKER):].strip()
        if " :: " in rest:
            name, reason = rest.split(" :: ", 1)
            skips[name.strip()] = reason.strip() or "skipped"
        elif rest:
            skips[rest] = "skipped"
    return skips
