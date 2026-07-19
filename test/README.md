This is the SQL and C++ DuckDB Unittests

Typical entrypoints:
- POSIX builds: `build/release/test/run`, `build/reldebug/test/run`, `build/debug/test/run`
- Windows native layout: `build/release/test/run.py` or `build/release/test/run.bat`

## Test Contract

Within SQL tests, the test runner (unittest) guarantees that these environment variables will be set. Many can be overridden when needed.

| Env Var          | Default Value              | Overrides                                                  | Notes                                                                              |
|------------------|----------------------------|-------------------------------------------------------------|------------------------------------------------------------------------------------|
| `TEST_NAME` | e.g. `test/path/filename.test` | N/A |                                                                                    |
| `TEST_NAME__NO_SLASH` | e.g. `test_path_filename.test` | N/A | is always `TEST_NAME s@/@_@g` (keeps the body suffix)                               |
| `TEST_ID` | e.g. `test_path_filename_test` | N/A | per-test identity: the full `TEST_NAME` sanitized to one path component (every char outside `[A-Za-z0-9_-]` → `_`, including `.`); the body suffix is kept so siblings differing only by suffix (`foo.test` vs `foo.test_slow`) stay distinct; the `[TEST_ID]` path level under `TEMP_DIR_BASE` |
| `TEST_UUID` | random UUID (as string) | N/A | defined per invocation                                                             |
| `RUN_ID` | generated (`<ts>--<mnemonic>`) | `unittest --run-id <id>`<br/>also `DUCKDB_TEST_RUN_ID`, config `run_id` | per-run identity; the `[RUN_ID]` path level; shared across a run's tests            |
| `WORKING_DIR`    | e.g., `/Users/me/src/duckdb`     | `unittest --test-dir <path>`                                | default: wherever duckdb was sourced and built <br/> AKA: `{WORKING_DIRECTORY}`    |
| `BUILD_DIR`      | `{WORKING_DIR}/build/release` | N/A                                                         | read-only; derived from unittest path <br/> AKA: `{BUILD_DIRECTORY}`               |
| `DATA_DIR`  | `{WORKING_DIR}/data`        | `unittest --data-dir <path>`<br/>also `DUCKDB_TEST_DATA_DIR`, config `data_dir`, or a `test_env` entry | copy of `duckdb/data/**`, to test AWS/Azure/other VFS reads; may be relative or absolute/remote (e.g. `az://...`). See [Data directory](#data-directory). |
| `TEMP_DIR_BASE` | `{WORKING_DIR}/duckdb_unittest_tempdir` | `unittest --temp-dir-base <path>`<br/>also `DUCKDB_TEST_TEMP_DIR_BASE`, config `temp_dir_base` | root of the temp-dir tree; may be local or remote (e.g. `s3://`)                    |
| `TEMP_DIR` | `{TEMP_DIR_BASE}/[RUN_ID]/[TEST_ID]` | `--temp-dir-run-id {on,off}` / `--temp-dir-test-id {on,off}` (include/omit each level); `--temp-dir-{create,destroy}` govern lifecycle | the per-test temp dir; use to test VFS writes <br/> AKA: `{TEST_DIR}`, deprecated `__TEST_DIR__` |
| `TEMP_DIR_ABSOLUTE` | absolute path to `{TEMP_DIR}` | N/A | use when a test requires an absolute path, e.g. `file://` URIs                     |
| `LOCAL_TEMP_DIR` | `{TEMP_DIR}` | `unittest --local-temp-dir-base <path>` (root only; must be local, and requires a remote `TEMP_DIR_BASE` — pins the local tree somewhere chosen instead of the default)<br/>also `DUCKDB_TEST_LOCAL_TEMP_DIR_BASE`, config `local_temp_dir_base`, or a `test_env` entry | guaranteed-**local** twin of `TEMP_DIR` — the same path when `TEMP_DIR_BASE` is local, a separately-managed local tree (same `[RUN_ID]/[TEST_ID]` levels, same lifecycle) when it is remote. Use to stage/spill locally in a test that targets a remote root. |
| `LOCAL_DATA_DIR` | `{DATA_DIR}` | a `test_env` entry | guaranteed-**local** twin of `DATA_DIR`; falls back to `{WORKING_DIR}/data` when `DATA_DIR` is remote |
| `CATALOG_DIR` | `{TEMP_DIR}/{TEST_UUID}` | N/A | a per-test catalog dir; NOT guaranteed to exist (create on demand)                 |
| *(any)* | N/A | `unittest --env-passthrough <NAME>` (repeatable, or comma-separated)<br/>also `DUCKDB_TEST_ENV_PASSTHROUGH`, config `env_passthrough` | pre-registers a **caller's** env var for `{NAME}` substitution in every test, with no `require-env`/`test-env` in the body. Unlike `require-env`'s per-test skip, a named-but-absent var fails the whole invocation at startup. Names above are reserved and rejected. |

Some tests require a particular environment variables to be set so they can properly run, usually this can be seen via `require-env VAR` in the test.

### Data directory

`DATA_DIR` is where tests read fixture data from (`{DATA_DIR}` in tests). Set it with `--data-dir <path>`, `DUCKDB_TEST_DATA_DIR=<path>`, a `data_dir` config option, or a `test_env` entry.

- **Default (unset):** `{WORKING_DIR}/data`. This follows the working directory: an out-of-tree extension test `chdir`s into its own source dir mid-run, so its `DATA_DIR` becomes `<extension>/data` — intentional, since those tests ship their own `data/` and address it relative to the repo root.
- **Set, relative:** used verbatim and resolved against the *current* cwd at read time, so it likewise follows a test's `chdir`.
- **Set, absolute or remote** (e.g. `az://…`, `s3://…`): used verbatim and never re-anchored — stable across `chdir`. It is the **caller's responsibility** to make an absolute `DATA_DIR` valid for every test in the run (or to exclude tests that change the working directory).

### Temp / test directory

Every test gets one scratch directory, written in-test as `{TEST_DIR}`. Its path is up to three nested levels:

    TEMP_DIR_BASE / [RUN_ID] / [TEST_ID]

- **`TEMP_DIR_BASE`** — the root (`duckdb_unittest_tempdir`, or `--temp-dir-base`); may be local or remote (`s3://…`). Normalized when parsed, so `.`/`..` segments are folded (lexically — no disk access) and `TEMP_DIR_BASE` reports the resolved form. A relative base stays relative. An empty base is rejected at startup.
- **`RUN_ID`** — per-run isolation, shared across a run's tests; toggle with `--temp-dir-run-id` (a fixed id co-locates many `unittest` batches under one dir; a bind-mount turns it off).
- **`TEST_ID`** — per-test isolation; toggle with `--temp-dir-test-id`. Resolved per-test (the name isn't known until the test runs) and materialized lazily on first use.

Lifecycle is one create × one destroy disposition, inherited down every level: `--temp-dir-create {never,on-absent,always}` and `--temp-dir-destroy {never,on-success,always}` (defaults `on-absent` / `on-success`). Both cover the HOME sandbox and the `LOCAL_TEMP_DIR` mirror too — `--temp-dir-destroy never` keeps everything a run made, for inspection.

A **remote** `TEMP_DIR_BASE` clamps create and destroy to `never`: object stores create-on-write, so the test owns materialization and `unittest` never mkdir/rm's a remote root. `LOCAL_TEMP_DIR` covers what still needs to be local in that configuration — see the contract table above. It is a full twin (`[RUN_ID]/[TEST_ID]`, same dispositions), rooted at the default local base; `create=never` does not apply to it, since `unittest` picks its base and no caller can have pre-provisioned it.

Every `--temp-dir-*` flag above (plus `--run-id`, `--database-destroy`, `--env-passthrough`) is a standard test-config option: each also takes a `DUCKDB_TEST_<NAME>` env var and can be set from a `--test-config` file. CLI beats env; a config file loaded later beats both.

The **loaded database files** under `TEMP_DIR` have their own disposition, `--temp-dir-destroy`'s independent sibling: `--database-destroy {on,off,on-success}` (default `on-success`). A DB's keep/destroy is not per-se a temp-dir property, so it is a separate knob — use `off` to retain the generated DB files inside a retained temp dir (e.g. to diff them after the run, as `test_zero_initialize.py` does); `on-success` mirrors `--temp-dir-destroy` so a failed test's DB survives for inspection.

**Naming — read once, then stop worrying:**
- `{TEST_DIR}` (the token nearly all tests use) and the `TEMP_DIR` env var are the **same directory** — synonyms. Prefer `{TEST_DIR}`; `{TEMP_DIR}` and the deprecated `__TEST_DIR__` also resolve to it.
- `{TEST_DIR}`/`TEMP_DIR` is **relative** (to `WORKING_DIR`) *by default* — deliberately, so absolute machine paths don't leak into compared test output. When a test genuinely needs an absolute path (e.g. a `file://` URI), use **`TEMP_DIR_ABSOLUTE`**.
- `TEMP_DIR_BASE` is the root only; `CATALOG_DIR` is `{TEST_DIR}/{TEST_UUID}` (create on demand).

**Extension tests are the one exception.** Out-of-tree extension tests `chdir` into their own source dir mid-run, so a *relative* `{TEST_DIR}` would resolve against the wrong directory. For those (only), the runner pins `{TEST_DIR}`/`TEMP_DIR` to the absolute path — captured before the chdir; everything else stays relative.

### Parallel CSV Reader

The tests located in `test/parallel_csv/test_parallel_csv.cpp` run the parallel CSV reader over multiple configurations of threads
and buffer sizes.

### ADBC

The ADBC tests are in `test/api/adbc/test_adbc.cpp`. To run them the DuckDB library path must be provided in the `DUCKDB_INSTALL_LIB` variable.
