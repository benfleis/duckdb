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
import subprocess
import tempfile
import threading

import pytest

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
# Sidecar (.py) support
#
# A same-stem `.py` "claims" its `.test`: the `.py` is collected as a normal
# pytest module (so it gets native setup/teardown via fixtures) and drives the
# `.test` through the binary via run_paired(); the standalone `.test` is then
# suppressed from collection (see conftest) to avoid a double run.
# ---------------------------------------------------------------------------


def _stem_path(path, suffix):
    return os.path.splitext(str(path))[0] + suffix


def has_sidecar(test_path) -> bool:
    """True if a same-stem `.py` sits next to this `.test` (claims it)."""
    return os.path.exists(_stem_path(test_path, ".py"))


def is_sidecar(py_path) -> bool:
    """True if this `.py` has a same-stem `.test` to drive."""
    return os.path.exists(_stem_path(py_path, ".test"))


def run_paired(request, *, external_test_dir=None):
    """Drive the `.test` paired with the calling sidecar `.py` through the binary.

    Call from the sidecar's test body once setup has run. Raises SqlLogicFailure
    on failure and pytest.skip on a skipped test, so the sidecar test reflects the
    real SQL result. Pass external_test_dir to hand the binary a caller-owned base
    dir (--external-test-dir) that setup has already staged into.
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
        )


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class SqlLogicItem(pytest.Item):
    @classmethod
    def from_parent(cls, parent, *, test_name, binary, working_dir, **kwargs):
        obj = super().from_parent(parent, **kwargs)
        obj._test_name = test_name
        obj._binary = binary
        obj._working_dir = working_dir
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
        result = _invoke(self._binary, [self._test_name], self._working_dir)
        _raise_for_result(_parse_result(result))

    # -- batch path (batch_size > 1) -----------------------------------------

    def _run_batched(self):
        with _batch_cache_lock:
            if self._batch_id not in _batch_cache:
                _batch_cache[self._batch_id] = _execute_batch(
                    self._batch_test_names, self._binary, self._working_dir
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
    # Dedupe by nodeid: a sidecar .py named explicitly on the CLI is collected
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
# Batch execution
# ---------------------------------------------------------------------------


def _execute_batch(test_names: list, binary: str, working_dir: str) -> dict:
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
        )
    finally:
        os.unlink(tmpfile)

    if result.get("returncode", 1) == 0:
        # All passed (skips indistinguishable from passes at batch level;
        # use --batch-size 1 for per-test skip attribution).
        return {name: {"status": "pass"} for name in test_names}

    # Re-run individually so each item gets an accurate result.
    return {name: _invoke_single(name, binary, working_dir) for name in test_names}


def _invoke_single(test_name: str, binary: str, working_dir: str) -> dict:
    result = _invoke(binary, [test_name], working_dir)
    return _parse_result(result)


# ---------------------------------------------------------------------------
# Low-level subprocess helpers
# ---------------------------------------------------------------------------


def _invoke(binary: str, args: list, working_dir: str) -> dict:
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


def _parse_result(result: dict) -> dict:
    """Classify a single-test invocation result as pass / skip / fail."""
    if result["returncode"] != 0:
        return {"status": "fail", "output": result["stdout"] + result["stderr"]}
    combined = result["stdout"] + result["stderr"]
    if "tests were skipped" in combined:
        return {"status": "skip", "reason": _extract_skip_reason(combined)}
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


def _extract_skip_reason(output: str) -> str:
    for line in output.splitlines():
        s = line.strip()
        if s.startswith(("require-env ", "require ")):
            return s
        if "skipped" in s.lower() and s:
            return s
    return "skipped"
