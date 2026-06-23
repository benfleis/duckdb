"""Small, memorable run-id mnemonics for external test directories.

Standalone (not part of the sqllogic collector) so it can be reused and tested on
its own. Produces short, eyeball-able names like `brave-otter` for a run dir, so a
failure directory is easy to spot and recall from memory instead of a raw uuid.

The pure-python tests below are *not* auto-collected (the plugin sets
`python_files =` to keep pytest out of stray scripts); run them explicitly:
    pytest test/pytest/mnemonic.py
"""
import random
from datetime import datetime

_ADJECTIVES = [
    "amber", "brave", "calm", "clever", "cosmic", "eager", "fuzzy", "gentle",
    "happy", "jolly", "keen", "lively", "mellow", "nimble", "plucky", "quiet",
    "rapid", "shiny", "spry", "sunny", "swift", "tidy", "vivid", "witty", "zesty",
]
_NOUNS = [
    "otter", "puppy", "kitten", "falcon", "badger", "cobra", "dingo", "ferret",
    "gecko", "heron", "ibex", "jackal", "koala", "lemur", "marmot", "newt",
    "ocelot", "panda", "quokka", "raven", "seal", "tapir", "urchin", "viper",
]


def mnemonic(words=2, sep="-", rng=None):
    """A short memorable name like 'brave-otter' (adjective(s) + noun)."""
    rng = rng or random
    parts = [rng.choice(_ADJECTIVES) for _ in range(max(0, words - 1))]
    parts.append(rng.choice(_NOUNS))
    return sep.join(parts)


def run_id(now=None, words=2):
    """Sortable + memorable run id, e.g. 2026-06-23T08-48-12--brave-otter.

    ISO-8601 *basic*-style: the `T` separates date/time and colons are replaced with
    dashes so the name is a legal path component on Windows too. Still sorts lexically;
    the mnemonic is the human handle.
    """
    now = now or datetime.now()
    return f"{now.strftime('%Y-%m-%dT%H-%M-%S')}--{mnemonic(words)}"


# --- pure-python tests (run explicitly; not auto-collected) ------------------

def test_mnemonic_shape():
    name = mnemonic()
    assert name.count("-") == 1
    a, n = name.split("-")
    assert a in _ADJECTIVES and n in _NOUNS


def test_mnemonic_word_count():
    assert mnemonic(words=3).count("-") == 2


def test_run_id_sortable_and_memorable():
    rid = run_id(now=datetime(2026, 6, 23, 23, 44, 22))
    stamp, sep, mnem = rid.partition("--")
    assert sep == "--"
    assert stamp == "2026-06-23T23-44-22"  # ISO basic-style; no colons (Windows-safe)
    assert ":" not in rid
    assert "-" in mnem and mnem.split("-")[-1] in _NOUNS
