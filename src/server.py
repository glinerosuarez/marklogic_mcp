#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from config import CLIENTS, CONFIG, DEFAULT_SERVER

mcp = FastMCP(
    "ml-mcp-server",
    instructions=(
        "Read, write, search, and query MarkLogic databases. "
        f"Available servers: {', '.join(CLIENTS)}. Default: {DEFAULT_SERVER}. "
        "Pass server= and database= on each tool call to target a specific environment and database. "
        "Failures are raised as tool errors, never returned as a normal result: a call "
        "that comes back without an error succeeded. Error text carries the MarkLogic "
        "message code (e.g. XDMP-NOSUCHDB, RESTAPI-NODOCUMENT, XDMP-UPDATEFUNCTIONFROMQUERY) "
        "and, for eval, the stack frames MarkLogic reported. A TIMEOUT error is a load "
        "symptom rather than a wrong query — retry it, and split expensive "
        "cts:search/xdmp:estimate work across separate calls instead of bundling it."
    ),
)

_FMT_MAP = {"json": "application/json", "xml": "application/xml", "text": "text/plain"}


def _client(server: str):
    name = server or DEFAULT_SERVER
    if name not in CLIENTS:
        raise ValueError(f"Unknown server '{name}'. Available: {list(CLIENTS)}")
    return CLIENTS[name]


@mcp.tool()
def read_document(uri: str, database: str, server: str = "") -> str:
    """Read a MarkLogic document by URI. Returns raw document content.

    Args:
        uri: Document URI, e.g. /protein/P12345.xml or /config/settings.json
        database: Database name, e.g. rnatherapeutic or Documents.
        server: Server name (local/dev/prod). Defaults to configured default.
    """
    return _client(server).read_document(uri, database)


@mcp.tool()
def write_document(
    uri: str,
    content: str,
    database: str,
    format: str = "json",
    collections: list[str] | None = None,
    permissions: list[str] | None = None,
    server: str = "",
) -> str:
    """Write a document to MarkLogic. Returns "OK" on success.

    Args:
        uri: Target URI, e.g. /notes/entry.json
        content: Document content as string (JSON, XML, or plain text).
        database: Database name, e.g. rnatherapeutic or Documents.
        format: "json", "xml", or "text". Determines Content-Type.
        collections: Collection URIs to assign, e.g. ["notes", "project/alpha"].
        permissions: ["role:capability"] pairs, e.g. ["rest-reader:read", "rest-writer:update"].
                     Defaults to server's default_permissions if omitted.
                     WARNING: omitting permissions makes the document admin-only visible.
        server: Server name (local/dev/prod).
    """
    content_type = _FMT_MAP.get(format, "application/json")
    parsed_perms = None
    if permissions:
        # Validate explicitly: "badformat".split(":", 1) yields a 1-element list rather
        # than raising, so a try/except around the split cannot catch a missing colon —
        # it surfaces much later as "not enough values to unpack" from inside
        # write_document, which tells the caller nothing about what they got wrong.
        parsed_perms = []
        for p in permissions:
            if ":" not in p:
                raise ValueError(
                    f"invalid permissions format — expected 'role:capability', got {p!r}. "
                    "Example: ['rest-reader:read', 'rest-writer:update']"
                )
            role, cap = p.split(":", 1)
            if not role.strip() or not cap.strip():
                raise ValueError(
                    f"invalid permissions entry {p!r} — both role and capability must be "
                    "non-empty, e.g. 'rest-reader:read'"
                )
            parsed_perms.append((role.strip(), cap.strip()))
    _client(server).write_document(uri, content, content_type, collections, parsed_perms, database)
    return "OK"


@mcp.tool()
def search_documents(
    database: str,
    query: str = "",
    collection: str = "",
    limit: int = 10,
    offset: int = 1,
    snippets: bool = False,
    server: str = "",
) -> dict:
    """Search MarkLogic documents. Returns {"n": total, "uris": [...]}.

    Args:
        database: Database name, e.g. rnatherapeutic or Documents.
        query: Full-text query string. Empty string returns all documents.
        collection: Filter to a specific collection URI.
        limit: Max results (1–500). Default 10.
        offset: Starting position for pagination. Default 1.
        snippets: Include text snippets alongside URIs.
        server: Server name (local/dev/prod).
    """
    return _client(server).search_documents(query, collection, limit, offset, snippets, database)


@mcp.tool()
def eval_xquery_readonly(xquery: str, database: str, server: str = "") -> list[str]:
    """Execute read-only XQuery against MarkLogic. Enforces query transaction mode — any write attempt raises an error.

    Use this for all read/inspect queries. Use eval_xquery only when you need to write.

    Args:
        xquery: XQuery expression, e.g.:
                "xdmp:estimate(fn:doc())"
                'fn:collection("notes")/note/title/fn:string()'
                'for $d in fn:doc() where $d//status = "active" return base-uri($d)'
        database: Database name, e.g. rnatherapeutic or Documents.
        server: Server name (local/dev/prod).
    """
    return _client(server).eval_xquery_readonly(xquery, database)


@mcp.tool()
def eval_xquery(xquery: str, database: str, server: str = "") -> list[str]:
    """Execute XQuery against MarkLogic. Returns one string per result item.

    Args:
        xquery: XQuery expression, e.g.:
                "xdmp:estimate(fn:doc())"
                'fn:collection("notes")/note/title/fn:string()'
                'for $d in fn:doc() where $d//status = "active" return base-uri($d)'
        database: Database name, e.g. rnatherapeutic or Documents.
        server: Server name (local/dev/prod).
    """
    return _client(server).eval_xquery(xquery, database)


@mcp.tool()
def eval_xquery_to_file(
    xquery: str,
    database: str,
    file_path: str,
    readonly: bool = False,
    server: str = "",
) -> str:
    """Execute XQuery against MarkLogic and save the result to a local file.

    Joins the result items (one per XQuery result, MIME boundaries already stripped)
    with newlines and writes the plain text to file_path. Useful when a result set
    is too large or unwieldy to return directly through MCP.

    Args:
        xquery: XQuery expression, e.g.:
                "xdmp:estimate(fn:doc())"
                'fn:collection("notes")/note/title/fn:string()'
                'for $d in fn:doc() where $d//status = "active" return base-uri($d)'
        database: Database name, e.g. rnatherapeutic or Documents.
        file_path: Local filesystem path to write the output to, e.g. /tmp/results.txt.
        readonly: If true, enforce query transaction mode — any write attempt raises an error.
        server: Server name (local/dev/prod). Defaults to configured default.
    """
    client = _client(server)
    results = (
        client.eval_xquery_readonly(xquery, database)
        if readonly
        else client.eval_xquery(xquery, database)
    )
    content = "\n".join(results)
    Path(file_path).write_text(content, encoding="utf-8")
    return f"OK: wrote {len(results)} result item(s) ({len(content)} chars) to {file_path}"


@mcp.tool()
def validate_documents(
    database: str,
    uris: list[str] | None = None,
    collection: str = "",
    limit: int = 100,
    mode: str = "strict",
    server: str = "",
) -> dict:
    """Validate stored documents against their in-scope schema (XSD). Returns per-URI errors.

    **MarkLogic does not validate on insert.** A schema-invalid document is written
    silently — no error, no failed record — and is indistinguishable from a valid one
    by any count, existence, or "job succeeded" check. Run this after any job that
    constructs, rebuilds, reorders, or inserts elements, and before reporting the work
    verified. Validating a small sample first, then the full set, is much cheaper than
    discovering the problem from a corpus-wide report days later.

    Typical findings and what they mean:
      XDMP-VALIDATEUNEXPECTED   a child element is in the wrong position in an
                                xs:sequence — `detail` names what was found and what
                                was expected, e.g. "Found b but expected (a,b)"
      XDMP-VALIDATEMISSINGELT   a required child element is absent
      XDMP-VALIDATENODECL       no schema declaration is in scope for this element;
                                in "strict" mode that is reported for every document,
                                so it usually means the schema is not loaded rather
                                than that the data is wrong

    Args:
        database: Database name to validate in, e.g. protein.
        uris: Explicit document URIs to validate. Takes precedence over `collection`.
        collection: Sample up to `limit` documents from this collection instead.
                    Either `uris` or `collection` is required.
        limit: Max documents to validate (1-1000, default 100). Also caps an
               over-long `uris` list, in which case `truncated` comes back true.
        mode: "strict" (default) requires a schema declaration for every element —
              use it to detect ordering and missing-child defects. "lax" validates
              only what it can find a declaration for, which is the right choice when
              part of the corpus is deliberately schema-less.
        server: Server name (local/dev/prod).

    Returns {"checked": n, "invalid": n, "missing": n, "mode": str,
             "schema_in_scope": bool, "truncated": bool,
             "errors": [{"uri", "code", "message", "detail", "error_count"}, ...]}.

    **Check `schema_in_scope` before believing a clean result.** If it is false, no XSD
    is loaded in the schema database attached to `database`, a `warning` key explains
    what to do, and the numbers are meaningless: "lax" reports invalid=0 while checking
    nothing, and "strict" reports XDMP-VALIDATENODECL for every document. As of this
    writing the `Schemas` and `<db>-schemas` databases are empty in this org's local and
    dev environments — validation there is done out-of-band against the authoritative
    XSD service (see the `ent-ref-xsd` MCP server), not by MarkLogic. Confirm
    `schema_in_scope` is true before using this tool as your verification step.
    """
    return _client(server).validate_documents(
        database=database, uris=uris, collection=collection, limit=limit, mode=mode
    )


@mcp.resource("marklogic://servers")
def list_servers() -> dict:
    """All configured MarkLogic server profiles and the default server."""
    return {
        "default": DEFAULT_SERVER,
        "servers": {
            name: cfg.get("host", "unknown")
            for name, cfg in CONFIG.get("servers", {}).items()
        },
    }


@mcp.resource("marklogic://{server}/{database}/document/{uri}")
def document_resource(server: str, database: str, uri: str) -> str:
    """Retrieve a MarkLogic document as an MCP resource.

    `database` is part of the template on purpose. It used to be omitted, which made
    every read silently fall through to the App-Services default database instead of
    the one the caller meant — returning either the wrong document or a spurious
    "not found".
    """
    return _client(server).read_document(uri, database)


if __name__ == "__main__":
    mcp.run()
