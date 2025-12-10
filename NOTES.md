# Quick Notes on hacking Templates (mustache) and Variables into tests

## Target

My initial thinking is that, given:

- config.yml
- template (`my_test.slt.mustache`)
- environment
- test engine switches

we can run dynamic tests, easy to read and reason about, with clearly spelled-out semantics.

### Example

For clarity, let's assume the following:

- `{{ foo }}` is a mustache-type variable, sourced from config.yml (or other conf); this is rendered in the "first pass" (before any semantic parsing), thus is purely text-to-text.
- `${ foo }` is a variable within our sqllogictest framework; these can only be rendered within whole token/params after parsing; most useful for secrets and dynamic temporaries, but would be less common than mustache vars above.

In practice these would be both rendered at run-time, although the template could be rendered to files.

```config.yml
account: &account duckdblabstestdatablob
az_data_dir: &data_dir az://duckdblabs-data/common/azure_data
az_temp_dir: &temp_root az://duckdblabs-write-testing/extension/azure

env:
    AZURE_STORAGE_ACCOUNT: *account
    DATA_DIR: *data_dir
    TEMP_ROOT: *temp_root
```

```azure_authn.mustache
require-env AZURE_AUTH_ENV 1

require-env AZURE_STORAGE_ACCOUNT

statement ok
CREATE OR REPLACE SECRET az_authn (
    TYPE AZURE,
    PROVIDER CREDENTIAL_CHAIN,
    ACCOUNT_NAME '${AZURE_STORAGE_ACCOUNT}'
);

```

```write.slt.mustache
# name: write.slt
# description: test azure extension writes
# group: [write]

{{ < azure_requires }}  # <- sources azure_requires.mustache
{{ < azure_authn }}     # <- sources azure_authn.mustache

## Basic write
query I
COPY (SELECT id: 42) TO '{{ test.TEMP_DIR }}/write-id-42.csv';
----
1
```

Which in memory at runtime would render down to:

```write.slt
# name: write.slt
# description: test azure extension writes
# group: [write]

require azure

require-env AZURE_AUTH_ENV 1

require-env AZURE_STORAGE_ACCOUNT

statement ok
CREATE OR REPLACE SECRET az_authn (
    TYPE AZURE,
    PROVIDER CREDENTIAL_CHAIN,
    ACCOUNT_NAME 'duckdblabstestdatablob'
);

## Basic write
query I
COPY (SELECT id: 42) TO 'az://duckdblabs-write-testing/extension/azure/20251210T125901Z--abc-123/write-id-42.csv';
----
1
```

## Discoveries

I sought out both a mustache library and a PEG parser that (a) are dep free and (b) license friendly, and found these:

- <https://github.com/kainjow/Mustache/blob/master/mustache.hpp>
- <https://github.com/no1msd/mstch>
- <https://github.com/yhirose/cpp-peglib> (c++17 only)
- <https://github.com/taocpp/PEGTL> (older versions support c++11)

Also I asked ChatGPT to make me a first pass PEG grammar for sqllogictest (for a py peg parser), and this is the key bit, just as a reference:

```PEG
File            = WS0 (Record (BlankLine+ Record)*)? WS0 EOF
Record          = Prefix* (Statement / Query / Control)

# ----- Prefix conditionals -----
Prefix          = (SkipIf / OnlyIf) EOL
SkipIf          = "skipif" SP1 DatabaseName
OnlyIf          = "onlyif" SP1 DatabaseName
DatabaseName    = Ident

# ----- Statement records -----
Statement       = "statement" SP1 ( "ok" / "error" ) EOL SqlBlock

# ----- Query records -----
Query           = "query" SP1 TypeString (SP1 SortMode)? (SP1 Label)? EOL QueryBody
QueryBody       = SqlUntilResults (ResultsBlock / EmptyResults)
SqlUntilResults = SqlLine*
ResultsBlock    = SepLine ResultLine*
EmptyResults    = (BlankLine / &EOF)

SepLine         = "----" EOL
ResultLine      = !BlankLine !CommentLine Line

TypeString      = TypeChar+
TypeChar        = ~r"[TIR]"
SortMode        = "nosort" / "rowsort" / "valuesort"
Label           = LabelChar+
LabelChar       = ~r"[A-Za-z0-9._-]"

# ----- Control records -----
Control         = Halt / HashThreshold
Halt            = "halt" EOL
HashThreshold   = "hash-threshold" SP1 Integer EOL

# ----- SQL & lines -----
SqlBlock        = SqlLine+
SqlLine         = !BlankLine !CommentLine !SepLine Line
Line            = ~r"[^\r\n]*" EOL

# ----- Comments, spacing, separators -----
CommentLine     = WS0 "#" ~r"[^\r\n]*" EOL
BlankLine       = WS0 EOL
WS0             = ~r"[ \t]*"
SP1             = ~r"[ \t]+"
EOL             = ~r"\r\n|\n|\r"
EOF             = !~r"."

# ----- Lexical helpers -----
Ident           = ~r"[A-Za-z_][A-Za-z0-9_]*"
Integer         = ~r"[0-9]+"
```
