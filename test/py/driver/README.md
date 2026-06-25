# pytest driver framework for duckdb test suites

This package (`test/py/driver/`) is the **generic** pytest front-end over the duckdb
`unittest` (Catch2) binary. It is the single source of truth; extensions consume it by
**symlink** (see *Extension setup* below). Comments in `sqllogic.py` / `conftest.py` carry
the per-feature detail — this file is the map.

## Model

- A **test** is the set of same-stem **members** under `test/**` (e.g. `foo.test` + `foo.py`).
- A member has role(s): a **body** (`.test`/`.sql`, or a `.py` carrying its own assertions)
  and/or a **driver** (`.py` — Python `initialize`/`finalize` around the body). Roles are not
  file types: a `.py` can be both.
- A **driverless** body runs directly through the binary; a body with a same-stem `.py`
  **driver** runs only through that driver (the standalone body is suppressed).

## Resources & disposition

Everything a test needs is a **resource**: a temp dir, a table (source or copy), a catalog,
credentials, a connection/session. A driver `initialize`/`finalize` is, in the end, just
acquiring and releasing resources. Each resource has two lifecycles:

- **existence:** provision → destroy
- **access:** acquire → release

A resource is specced by one small, flat triple (+ identity, + optional source):

- **acquire-mode** — `shared` | `exclusive`. (This is `ro` | `rw`: an `rw` acquirer dirties
  the resource, so it needs its **own** instance — e.g. a `DEEP CLONE` rather than the source.)
- **create-disposition** — `NEVER_CREATE` | `MAY_CREATE` | `ALWAYS_CREATE` (governs *provision*).
- **destroy-disposition** — `NEVER_DESTROY` | `MAY_DESTROY` (on success) | `ALWAYS_DESTROY`
  (governs *release/destroy*).

One vocabulary covers temp dirs *and* tables *and* catalogs:

| resource | mode | create | destroy |
|---|---|---|---|
| read-only source table | shared | `NEVER` (pre-staged) | `NEVER` |
| read-write copy | exclusive | `ALWAYS` (DEEP CLONE) | `ALWAYS` (drop) |
| per-run temp dir | — | `MAY` | `MAY` (on success) — `--external-test-dir-destroy` |
| local catalog (e.g. OSS UC) | shared | `MAY` (stand up if absent) | `MAY` |

**Keep it simple — the discipline:**
1. The spec stays a **flat declaration**; resist a class hierarchy of resource *types*. A table
   and a temp dir share the spec — only the *provisioner* differs.
2. **Don't hand-roll a resource manager.** pytest **fixture scopes** are the provision/share/
   release engine: `session` scope = provision-once-and-share; `function` scope = acquire-per-
   test; teardown = release. You *declare*; fixtures *execute*.
3. Verbs: test-facing **acquire / release**; underneath, provision/destroy are just the two
   disposition axes. Four verbs, no refcounting in your code.
4. **Match duckdb's existing resource words** where they exist (acquire/release, pin/unpin, …)
   so it reads as the same idea, not a parallel one.

**Isolation is the provisioner's choice, not a test knob.** Two ways to keep concurrent /
successive runs from colliding, picked by the *provisioner* (the test declares the same need
either way):

- **by lifecycle** — recreate fresh (`ALWAYS_CREATE`/`ALWAYS_DESTROY`), fixed names. Right when
  ops are cheap (e.g. local OSS UC: tear down service/tables between runs).
- **by namespace** — keep resources, scope them under a per-run name. Right when ops are slow
  (e.g. Databricks: drop everything into `schema = temp_<run-id>` instead of churning).

The **run-id is the shared namespace token**: `BASE/<run-id>` for temp dirs *is*
`temp_<run-id>` for schemas — same per-run id, SQL-safe rendering (`temp_brave_otter`:
underscores, no dashes/colons). And **namespace-via-default-schema** keeps bodies simple: point
the connection's default schema at `temp_<run-id>` and a body's logical `sales` resolves to
`temp_<run-id>.sales` with no name injection — so "fixed names in bodies" and "physical
isolation" coexist for free.

The temp-dir disposition (`--external-test-dir`, `--external-test-dir-destroy {never,on-success,
always}`) is the first concrete instance of this model; table/catalog provisioning in extension
helpers (`test/py/<repo>/`) follows the same spec.

## Layout

```
test/py/driver/      generic framework (THIS package) — symlinked into each extension
  sqllogic.py        collector + runner + hooks (the pytest plugin: driver.sqllogic)
  mnemonic.py        run-id mnemonics for external test dirs
  __init__.py        re-exports the public API (from driver import …)
test/py/<repo>/      real, per-extension helpers (e.g. test/py/uc/) — NOT symlinked
test/sql/<repo>/     the tests: body + optional same-stem driver .py
scripts/…            generators etc.; reused by repo helpers
```

## Extension setup (symlinks)

From an extension root, symlink the **generic** pieces to this SoT; keep everything else real:

```
conftest.py        -> ../d/conftest.py
pytest.ini         -> ../d/pytest.ini
test/py/driver     -> ../../../d/test/py/driver
```

Real (per-extension): `test/py/<repo>/` (helpers), `test/sql/<repo>/` (tests), `scripts/`.

**Why the symlinked conftest still works per-extension:** it anchors on
`os.path.dirname(os.path.abspath(__file__))` — **`abspath`, never `realpath`**. For a symlink,
`__file__` stays the symlink path, so the anchor is the *extension* root, not this SoT. The
conftest then puts the extension's own `test/py`, `scripts`, and `scripts/data_generator` on
`sys.path`. (If your environment ever resolves the symlink for `__file__`, the fallback is a
~10-line *real* conftest per extension that does the same `sys.path` setup and
`pytest_plugins = ["driver.sqllogic"]`.)

## Imports (from a driver or helper)

With `test/py` on `sys.path` (done by conftest) and each subdir a package:

```python
from driver import run_paired, require, Context   # generic framework (this package)
from uc import tables                              # real per-extension helpers (test/py/uc)
from delta_rs_generator import ...                 # generators (scripts/data_generator on path)
```

## Options (see `pytest.ini` for the full list)

`--build`, `--catch2-binary`, `--batch-size`, `--external-test-dir`,
`--external-test-dir-destroy {never,on-success,always}`.
