# marklogic_mcp

An MCP server for MarkLogic: read, write, search, query, and schema-validate documents
across configured environments.

## Tools

| Tool | Purpose |
|---|---|
| `read_document` | Fetch a document by URI. |
| `write_document` | Insert/replace a document, with collections and permissions. |
| `search_documents` | Search by full-text query and/or collection; returns total + URIs. |
| `eval_xquery` | Run XQuery. Updates permitted. |
| `eval_xquery_readonly` | Run XQuery with updates **rejected**. Use for all read/inspect work. |
| `eval_xquery_to_file` | Run XQuery with updates **rejected** and write the results to a local file. Always read-only — there is no write mode. |
| `validate_documents` | Validate stored documents against their in-scope XSD. |

`database` is required on every tool. `server` selects the environment and defaults to
`default_server` from the config.

## Setup

```bash
uv sync                                   # or: python -m venv .venv && pip install -e .
cp config.yaml.example config.yaml        # then fill in, or export the ${...} vars
cp config-local.yaml.example config-local.yaml
```

Both real config files are **gitignored because they hold credentials**. `config.py`
interpolates `${VAR}` from the environment and raises if a referenced variable is unset,
so prefer environment variables over writing passwords into the file.

Select a config with `ML_CONFIG_PATH`:

```bash
ML_CONFIG_PATH=config-local.yaml .venv/bin/python src/server.py
```

Register with Claude Code as a stdio server, pointing `ML_CONFIG_PATH` at the config you
want that entry to use. Two entries against different configs (e.g. `local` vs
`dev`/`prod`) is a convenient way to keep environments apart.

> **Changes require a restart.** The server is a long-lived stdio process; edits to
> `src/` do not take effect until the MCP client restarts it.

## Tests

```bash
.venv/bin/python tests/test_readonly_enforcement.py
```

The suite performs **real writes** and therefore hard-asserts that the loaded config
declares exactly one server named `local` on localhost, refusing to run otherwise. It
cannot reach dev or prod. It cleans up after itself and verifies it has done so.

Each blocking test asserts the **absence of a side effect**, not merely that an exception
was raised — an exception alone can come from a syntax error and would pass vacuously.
That distinction is what caught the read-only bug described below.

## Behaviour worth knowing

**Errors are raised, never returned.** A call that returns without error succeeded.
Failures surface as `MarkLogicError` carrying the parsed MarkLogic message code
(`XDMP-NOSUCHDB`, `RESTAPI-NODOCUMENT`, `XDMP-UPDATEFUNCTIONFROMQUERY`, …), the primary
diagnostic, and any stack frames — MarkLogic reports faults as HTML from `/v1/eval` and as
JSON from `/v1/documents`, and neither is readable raw. `TIMEOUT` and `CONNECTION` are
flagged `retryable`.

**Read-only enforcement uses the `<update>false</update>` eval option**, not a
`declare option xdmp:transaction-mode "query"` prolog. Do not "simplify" it back; two
separate bugs live down that road, and both are covered by tests:

1. The prolog does not reach the query body when a database is named — and because
   `database` is a required parameter, that was *every* call, so `eval_xquery_readonly`
   silently permitted writes.
2. The prolog is **session**-scoped, and the session is bound to the keep-alive HTTP
   connection — so one read-only query left the connection in query mode and broke every
   subsequent write on the same client.

**XQuery string escaping doubles quotes** (`"` → `""`); XQuery has no backslash escapes.
All interpolation goes through `_xq_str()`.

**`validate_documents` needs an XSD loaded in the schema database** attached to the target
database (e.g. `protein-schemas`). If none is loaded, the result carries
`schema_in_scope: false` plus a `warning`, because otherwise `lax` mode would report
`invalid: 0` — a clean-looking result that verified nothing. Check `schema_in_scope`
before trusting a clean run.
