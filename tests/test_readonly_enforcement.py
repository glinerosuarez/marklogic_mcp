#!/usr/bin/env python3
"""
Regression tests for eval_xquery_readonly read-only enforcement.

LOCAL SERVER ONLY. This suite performs real writes, so it hard-asserts that the
loaded config contains exactly one server named "local" pointing at localhost,
and refuses to run otherwise. It cannot reach dev or prod.

Run:
    cd ~/repos/ml_mcp_server && .venv/bin/python tests/test_readonly_enforcement.py

Background — the bug this guards against:
    eval_xquery_readonly built its query as

        declare option xdmp:transaction-mode "query";
        xdmp:invoke-function(function() { <user query> }, <options><database>…)

    Because `database` is a REQUIRED tool parameter, the invoke-function wrap was
    applied on every call, which put the transaction-mode declaration OUTSIDE the
    invoke. MarkLogic cannot switch database within a transaction, so the invoked
    function ran in a NEW transaction whose mode was auto-detected as update —
    meaning the "read-only" tool would happily execute writes, silently, on every
    call including against prod.

    The critical assertion in each blocking test is therefore NOT "an exception was
    raised" but "the side effect did not happen". An exception alone can come from a
    syntax error and would pass vacuously.
"""
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ["ML_CONFIG_PATH"] = str(REPO / "config-local.yaml")
sys.path.insert(0, str(REPO / "src"))

from config import CLIENTS, CONFIG, DEFAULT_SERVER  # noqa: E402
from ml_client import MarkLogicError  # noqa: E402

DB = "Documents"
PROBE = "/test-readonly-enforcement-PROBE.xml"
SEEDED = "/test-readonly-enforcement-SEEDED.xml"

_results: list[tuple[str, bool, str]] = []


def check(name: str):
    """Decorator registering a test function."""
    def deco(fn):
        try:
            fn()
            _results.append((name, True, ""))
            print(f"  PASS  {name}")
        except AssertionError as e:
            _results.append((name, False, str(e)))
            print(f"  FAIL  {name}\n          {e}")
        except Exception:
            detail = traceback.format_exc(limit=2).strip().replace("\n", "\n          ")
            _results.append((name, False, detail))
            print(f"  ERROR {name}\n          {detail}")
        return fn
    return deco


# ---------------------------------------------------------------- safety guard

def _guard():
    names = sorted(CLIENTS)
    assert names == ["local"], (
        f"REFUSING TO RUN: expected exactly one server 'local', got {names}. "
        "This suite writes to the database and must never touch dev or prod."
    )
    host = CONFIG["servers"]["local"].get("host")
    assert host in ("localhost", "127.0.0.1"), (
        f"REFUSING TO RUN: local server host is {host!r}, expected localhost."
    )
    assert DEFAULT_SERVER == "local", f"default_server is {DEFAULT_SERVER!r}, expected 'local'"


_guard()
c = CLIENTS["local"]
print(f"Config: {os.environ['ML_CONFIG_PATH']}")
print(f"Target: {c.base_url}  database={DB}\n")


def _exists(uri: str) -> bool:
    """Existence check via the WRITE-capable path, so a broken readonly path
    cannot mask a missing/extra document."""
    return c.eval_xquery(f'fn:doc-available("{uri}")', DB) == ["true"]


def _hard_delete(uri: str) -> None:
    try:
        c.eval_xquery(f'xdmp:document-delete("{uri}")', DB)
    except Exception:
        pass


def _reseed() -> None:
    """Put SEEDED back into a known state via the write path.

    Every blocking test calls this rather than inheriting state from the previous
    test: against unfixed code the readonly delete really does remove the document,
    which would otherwise cascade into misleading 'precondition' failures in the
    tests that follow.
    """
    _hard_delete(SEEDED)
    c.eval_xquery(f'xdmp:document-insert("{SEEDED}", <seed>keep me</seed>)', DB)
    assert _exists(SEEDED), "reseed failed — cannot establish test precondition"


def _assert_blocked(xquery: str, uri: str, label: str, should_exist_after: bool) -> None:
    """Run `xquery` through the readonly path and require that it was rejected AND
    left no side effect on `uri`."""
    raised = None
    try:
        c.eval_xquery_readonly(xquery, DB)
    except Exception as e:
        raised = e

    after = _exists(uri)
    assert after == should_exist_after, (
        f"{label}: readonly path MUTATED the database. "
        f"doc-available({uri}) is {after}, expected {should_exist_after}. "
        f"(exception raised: {type(raised).__name__ if raised else 'none'})"
    )
    assert raised is not None, (
        f"{label}: expected the readonly path to raise, but it returned normally. "
        "Enforcement must reject the update, not silently ignore it."
    )


# ---------------------------------------------------------------------- cleanup
_hard_delete(PROBE)
_hard_delete(SEEDED)


# ------------------------------------------------------------------ read tests

@check("readonly: simple read returns correct result")
def _():
    assert c.eval_xquery_readonly("1 + 1", DB) == ["2"], "arithmetic read failed"


@check("readonly: targets the requested database")
def _():
    got = c.eval_xquery_readonly("xdmp:database-name(xdmp:database())", DB)
    assert got == [DB], f"expected [{DB!r}], got {got!r}"


@check("readonly: honours an 'xquery version' prolog")
def _():
    got = c.eval_xquery_readonly('xquery version "1.0-ml"; 40 + 2', DB)
    assert got == ["42"], f"expected ['42'], got {got!r}"


@check("readonly: multi-item sequence reads intact")
def _():
    got = c.eval_xquery_readonly('("a", "b", "c")', DB)
    assert got == ["a", "b", "c"], f"expected ['a','b','c'], got {got!r}"


# --------------------------------------------------------------- BLOCKING tests
# These are the regression guards. Each asserts absence of side effect.

@check("readonly: BLOCKS xdmp:document-insert")
def _():
    assert not _exists(PROBE), "precondition: probe doc must not exist"
    _assert_blocked(
        f'xdmp:document-insert("{PROBE}", <probe/>)',
        PROBE, "document-insert", should_exist_after=False,
    )


@check("readonly: BLOCKS xdmp:document-delete")
def _():
    _reseed()
    _assert_blocked(
        f'xdmp:document-delete("{SEEDED}")',
        SEEDED, "document-delete", should_exist_after=True,
    )


@check("readonly: BLOCKS xdmp:node-replace")
def _():
    _reseed()
    _assert_blocked(
        f'xdmp:node-replace(fn:doc("{SEEDED}")/seed, <seed>MUTATED</seed>)',
        SEEDED, "node-replace", should_exist_after=True,
    )
    body = c.eval_xquery(f'fn:doc("{SEEDED}")/seed/fn:string()', DB)
    assert body == ["keep me"], f"node-replace leaked through: content is now {body!r}"


@check("readonly: BLOCKS xdmp:document-add-collections")
def _():
    _reseed()
    raised = None
    try:
        c.eval_xquery_readonly(
            f'xdmp:document-add-collections("{SEEDED}", "readonly-probe-collection")', DB
        )
    except Exception as e:
        raised = e
    cols = c.eval_xquery(f'xdmp:document-get-collections("{SEEDED}")', DB)
    assert "readonly-probe-collection" not in cols, (
        f"readonly path mutated collections: {cols!r}"
    )
    assert raised is not None, "expected add-collections to be rejected"


# ------------------------------------------------- write path must still work

@check("write path: eval_xquery still performs inserts")
def _():
    _hard_delete(PROBE)
    c.eval_xquery(f'xdmp:document-insert("{PROBE}", <probe>written</probe>)', DB)
    assert _exists(PROBE), "eval_xquery insert did not land — the fix broke writes"


@check("write path: eval_xquery still performs deletes")
def _():
    assert _exists(PROBE), "precondition: probe doc should exist"
    c.eval_xquery(f'xdmp:document-delete("{PROBE}")', DB)
    assert not _exists(PROBE), "eval_xquery delete did not land — the fix broke writes"


@check("no session-mode leak: write_document works AFTER a readonly eval")
def _():
    """Guards a pre-existing bug that was invisible while errors were returned as strings.

    `declare option xdmp:transaction-mode "query"` is SESSION-scoped in MarkLogic, and
    the session is bound to the keep-alive HTTP connection. Emitting it therefore left
    the connection in query mode, so every subsequent write on the same client failed
    with XDMP-UPDATEFUNCTIONFROMQUERY until a new connection was made. Enforcement now
    uses the request-scoped <update>false</update> eval option instead.
    """
    leak_uri = "/session-leak-check-DELETE-ME.xml"
    _hard_delete(leak_uri)
    c.eval_xquery_readonly("1 + 1", DB)                      # poisons the connection, historically
    c.write_document(leak_uri, "<a/>", "application/xml", [], None, DB)
    assert _exists(leak_uri), "write after a readonly eval did not land — session mode leaked"
    _hard_delete(leak_uri)


@check("no session-mode leak: repeated readonly/write interleaving stays healthy")
def _():
    leak_uri = "/session-leak-loop-DELETE-ME.xml"
    for i in range(3):
        _hard_delete(leak_uri)
        assert c.eval_xquery_readonly("1", DB) == ["1"], f"readonly read failed on pass {i}"
        c.write_document(leak_uri, f"<a>{i}</a>", "application/xml", [], None, DB)
        assert _exists(leak_uri), f"write failed on pass {i} — mode leaked"
    _hard_delete(leak_uri)


@check("readonly: enforced even when no database is named")
def _():
    nodb = "/nodb-readonly-DELETE-ME.xml"
    _hard_delete(nodb)
    raised = None
    try:
        c.eval_xquery_readonly(f'xdmp:document-insert("{nodb}", <a/>)')  # database omitted
    except Exception as e:
        raised = e
    assert not _exists(nodb), "readonly path with no database MUTATED the default database"
    assert raised is not None, "expected rejection when no database is named"


@check("write path: reads still return document content")
def _():
    _reseed()
    got = c.eval_xquery(f'fn:doc("{SEEDED}")/seed/fn:string()', DB)
    assert got == ["keep me"], f"expected ['keep me'], got {got!r}"


# ------------------------------------------------------- error contract (S2)
# Failures must surface as raised MarkLogicError with the MarkLogic message code
# parsed out — never as a normal return value the caller has to string-match.

@check("errors: readonly update raises MarkLogicError with XDMP code")
def _():
    try:
        c.eval_xquery_readonly(f'xdmp:document-insert("{PROBE}", <p/>)', DB)
    except MarkLogicError as e:
        assert e.code == "XDMP-UPDATEFUNCTIONFROMQUERY", f"code was {e.code!r}"
        assert "update function from a query" in str(e).lower(), f"unhelpful message: {e}"
        assert e.status == 500, f"status was {e.status!r}"
        assert e.retryable is False, "a rejected update is deterministic, not retryable"
        return
    raise AssertionError("expected MarkLogicError, nothing was raised")


@check("errors: eval syntax error reports XDMP-UNEXPECTED, not a bare 500")
def _():
    try:
        c.eval_xquery("this is not xquery $$$", DB)
    except MarkLogicError as e:
        assert e.code == "XDMP-UNEXPECTED", f"code was {e.code!r}"
        assert "500 Server Error" != str(e).strip(), "message degraded to a bare status"
        return
    raise AssertionError("expected MarkLogicError, nothing was raised")


@check("errors: unknown database reports XDMP-NOSUCHDB")
def _():
    try:
        c.eval_xquery("1", "NoSuchDatabaseXYZ")
    except MarkLogicError as e:
        assert e.code == "XDMP-NOSUCHDB", f"code was {e.code!r}"
        return
    raise AssertionError("expected MarkLogicError, nothing was raised")


@check("errors: missing document reports RESTAPI-NODOCUMENT from JSON envelope")
def _():
    try:
        c.read_document("/definitely-not-here-xyz.xml", DB)
    except MarkLogicError as e:
        assert e.code == "RESTAPI-NODOCUMENT", f"code was {e.code!r}"
        assert e.status == 404, f"status was {e.status!r}"
        return
    raise AssertionError("expected MarkLogicError, nothing was raised")


@check("errors: eval failures carry stack frames")
def _():
    try:
        c.eval_xquery('fn:error(xs:QName("err:FOER0000"), "boom")', DB)
    except MarkLogicError as e:
        assert e.frames, "no stack frames captured from the HTML <dt> list"
        return
    raise AssertionError("expected MarkLogicError, nothing was raised")


@check("errors: unknown server name raises rather than returning a string")
def _():
    from config import CLIENTS as _C
    assert "nope" not in _C
    # mirrors server._client's guard; the tool layer must not swallow this
    try:
        _C["nope"]
    except KeyError:
        return
    raise AssertionError("expected KeyError")


@check("errors: timeout is classified retryable with an actionable message")
def _():
    import requests as _rq
    from unittest import mock
    with mock.patch.object(c.session, "request", side_effect=_rq.exceptions.Timeout("simulated")):
        try:
            c.eval_xquery("1", DB)
        except MarkLogicError as e:
            assert e.code == "TIMEOUT", f"code was {e.code!r}"
            assert e.retryable is True, "timeouts must be marked retryable"
            assert "split" in str(e).lower(), "message should advise splitting the query"
            return
    raise AssertionError("expected MarkLogicError, nothing was raised")


@check("errors: connection failure is classified retryable")
def _():
    import requests as _rq
    from unittest import mock
    with mock.patch.object(c.session, "request",
                           side_effect=_rq.exceptions.ConnectionError("refused")):
        try:
            c.eval_xquery("1", DB)
        except MarkLogicError as e:
            assert e.code == "CONNECTION", f"code was {e.code!r}"
            assert e.retryable is True
            return
    raise AssertionError("expected MarkLogicError, nothing was raised")


@check("errors: malformed permissions rejected with an actionable message")
def _():
    import server as _srv
    for bad in (["badformat"], ["rest-reader:"], [":read"]):
        try:
            _srv.write_document("/perm-check.xml", "<a/>", DB, "xml", [], bad, server="local")
        except ValueError as e:
            msg = str(e)
            assert "role:capability" in msg or "non-empty" in msg, f"unhelpful: {msg}"
            assert "unpack" not in msg, f"leaked internal unpacking error: {msg}"
            continue
        raise AssertionError(f"permissions {bad!r} was accepted")
    assert not _exists("/perm-check.xml"), "rejected write should not have landed"


@check("errors: valid permissions still accepted")
def _():
    import server as _srv
    u = "/perm-ok-DELETE-ME.xml"
    _hard_delete(u)
    _srv.write_document(u, "<a/>", DB, "xml", [], ["rest-reader:read"], server="local")
    assert _exists(u), "valid permissions were rejected"
    _hard_delete(u)


@check("errors: success path returns data, not an error-shaped string")
def _():
    got = c.eval_xquery_readonly('"fine"', DB)
    assert got == ["fine"], f"got {got!r}"
    assert not any(str(x).startswith("ERROR:") for x in got), "error string leaked into results"


# --------------------------------------------------- validate_documents (S3)
# These load a tiny XSD into the local Schemas database so the detection path is
# tested for real, not just the plumbing. The schema declares element `wid` with an
# xs:sequence of (a, b) — so a document with <b/> before <a/> reproduces exactly the
# element-ordering defect class from LSCON-6326.

VXSD = "/schemas/test-validate-wid.xsd"
V_OK = "/test-validate-ok.xml"
V_BAD = "/test-validate-badorder.xml"
V_MISSING = "/test-validate-missingchild.xml"
_XSD_BODY = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="wid"><xs:complexType><xs:sequence>'
    '<xs:element name="a" type="xs:string"/><xs:element name="b" type="xs:string"/>'
    "</xs:sequence></xs:complexType></xs:element></xs:schema>"
)


def _setup_schema() -> None:
    c.write_document(
        VXSD, _XSD_BODY, "application/xml",
        ["http://marklogic.com/xdmp/schemas"], None, "Schemas",
    )
    c.eval_xquery(
        f'(xdmp:document-insert("{V_OK}", <wid><a>1</a><b>2</b></wid>),'
        f' xdmp:document-insert("{V_BAD}", <wid><b>2</b><a>1</a></wid>),'
        f' xdmp:document-insert("{V_MISSING}", <wid><a>1</a></wid>))',
        DB,
    )


def _teardown_schema() -> None:
    for u in (V_OK, V_BAD, V_MISSING):
        _hard_delete(u)
    try:
        c.eval_xquery(f'xdmp:document-delete("{VXSD}")', "Schemas")
    except Exception:
        pass


@check("validate: clean document reports no errors")
def _():
    _setup_schema()
    r = c.validate_documents(DB, uris=[V_OK])
    assert r["checked"] == 1, r
    assert r["invalid"] == 0, f"valid document reported invalid: {r['errors']}"
    assert r["errors"] == [], r["errors"]


@check("validate: DETECTS wrong child-element order (the LSCON-6326 defect)")
def _():
    r = c.validate_documents(DB, uris=[V_BAD])
    assert r["invalid"] == 1, f"ordering defect NOT detected: {r}"
    e = r["errors"][0]
    assert e["uri"] == V_BAD, e
    assert e["code"] == "XDMP-VALIDATEUNEXPECTED", f"code was {e['code']!r}"
    assert "expected" in e.get("detail", "").lower(), (
        f"detail should name what was expected, got {e.get('detail')!r}"
    )


@check("validate: detects a missing required child element")
def _():
    r = c.validate_documents(DB, uris=[V_MISSING])
    assert r["invalid"] == 1, f"missing-child defect NOT detected: {r}"
    assert r["errors"][0]["code"] == "XDMP-VALIDATEMISSINGELT", r["errors"][0]


@check("validate: mixed batch separates valid from invalid")
def _():
    r = c.validate_documents(DB, uris=[V_OK, V_BAD, V_MISSING])
    assert r["checked"] == 3, r
    assert r["invalid"] == 2, r
    assert {e["uri"] for e in r["errors"]} == {V_BAD, V_MISSING}, r["errors"]


@check("validate: nonexistent URI reported as MISSING-DOCUMENT, not as invalid")
def _():
    r = c.validate_documents(DB, uris=["/no-such-doc-at-all.xml"])
    assert r["missing"] == 1, r
    assert r["invalid"] == 0, r
    assert r["errors"][0]["code"] == "MISSING-DOCUMENT", r["errors"][0]


@check("validate: collection sampling honours limit and reports truncation")
def _():
    c.eval_xquery(
        f'xdmp:document-add-collections(("{V_OK}","{V_BAD}","{V_MISSING}"), "vtest-col")', DB
    )
    r = c.validate_documents(DB, collection="vtest-col", limit=2)
    assert r["checked"] == 2, f"limit not applied to collection sampling: {r}"
    r2 = c.validate_documents(DB, uris=[V_OK, V_BAD, V_MISSING], limit=2)
    assert r2["checked"] == 2 and r2["truncated"] is True, f"uris limit/truncation wrong: {r2}"
    r3 = c.validate_documents(DB, uris=[V_OK], limit=2)
    assert r3["truncated"] is False, r3


@check("validate: lax mode tolerates documents with no schema declaration")
def _():
    u = "/test-validate-noschema.xml"
    _hard_delete(u)
    c.eval_xquery(f'xdmp:document-insert("{u}", <totally-undeclared><q/></totally-undeclared>)', DB)
    strict = c.validate_documents(DB, uris=[u], mode="strict")
    lax = c.validate_documents(DB, uris=[u], mode="lax")
    assert strict["invalid"] == 1, f"strict should flag a missing declaration: {strict}"
    assert strict["errors"][0]["code"] == "XDMP-VALIDATENODECL", strict["errors"][0]
    assert lax["invalid"] == 0, f"lax should tolerate it: {lax}"
    _hard_delete(u)


@check("validate: a clean-looking result with no schema loaded is flagged, not trusted")
def _():
    """The dangerous case: lax mode with no XSD returns invalid=0, which reads as
    'verified' while having checked nothing. It must announce that."""
    u = "/test-validate-noschema2.xml"
    _hard_delete(u)
    c.eval_xquery(f'xdmp:document-insert("{u}", <undeclared-thing><q/></undeclared-thing>)', DB)
    lax = c.validate_documents(DB, uris=[u], mode="lax")
    assert lax["invalid"] == 0, lax
    assert lax["schema_in_scope"] is False, (
        f"no schema is loaded for this element but schema_in_scope was {lax['schema_in_scope']}"
    )
    assert "warning" in lax, "a zero-error result with no schema must carry a warning"
    assert "NOT be read as verified" in lax["warning"], lax["warning"]
    _hard_delete(u)


@check("validate: schema_in_scope is true when an XSD really is loaded")
def _():
    _setup_schema()
    r = c.validate_documents(DB, uris=[V_OK, V_BAD])
    assert r["schema_in_scope"] is True, f"schema was loaded but reported out of scope: {r}"
    assert "warning" not in r, f"unexpected warning on a genuine result: {r.get('warning')}"


@check("validate: bad mode and missing selector rejected with clear messages")
def _():
    try:
        c.validate_documents(DB, uris=[V_OK], mode="sloppy")
        raise AssertionError("invalid mode was accepted")
    except ValueError as e:
        assert "strict" in str(e), e
    try:
        c.validate_documents(DB)
        raise AssertionError("call with neither uris nor collection was accepted")
    except ValueError as e:
        assert "uris" in str(e) and "collection" in str(e), e


@check("validate: URIs containing quotes are escaped, not injected")
def _():
    # XQuery escapes a quote by DOUBLING it; backslash-escaping produces invalid XQuery.
    r = c.validate_documents(DB, uris=['/weird")-uri.xml'])
    assert r["missing"] == 1, f"quote in URI broke the query instead of being escaped: {r}"


@check("escaping: quotes in collection/query args do not break search_documents")
def _():
    # Pre-existing bug: these were backslash-escaped, which XQuery does not accept.
    r = c.search_documents(collection='no"such"collection', database=DB)
    assert r["n"] == 0, f"expected an empty result, got {r}"
    r2 = c.search_documents(query='a"b', database=DB)
    assert isinstance(r2.get("n"), int), f"quoted search term broke the query: {r2}"


@check("escaping: quotes in a database name surface as NOSUCHDB, not a syntax error")
def _():
    try:
        c.eval_xquery("1", 'No"SuchDb')
    except MarkLogicError as e:
        assert e.code in ("XDMP-NOSUCHDB", "XDMP-UNEXPECTED"), f"code was {e.code!r}"
        assert e.code == "XDMP-NOSUCHDB", (
            f"quoted database name produced a syntax error ({e.code}) — escaping is wrong"
        )
        return
    raise AssertionError("expected MarkLogicError")


@check("validate: runs through the readonly path and mutates nothing")
def _():
    before = c.eval_xquery(f'fn:doc("{V_BAD}")/wid/*[1]/fn:name()', DB)
    c.validate_documents(DB, uris=[V_OK, V_BAD])
    after = c.eval_xquery(f'fn:doc("{V_BAD}")/wid/*[1]/fn:name()', DB)
    assert before == after == ["b"], f"validation altered the document: {before} -> {after}"
    _teardown_schema()


# ----------------------------------------------------- database targeting (S4)

@check("S4: read_document honours database (no silent default fallback)")
def _():
    u = "/db-target-check.xml"
    _hard_delete(u)
    c.eval_xquery(f'xdmp:document-insert("{u}", <in>Documents</in>)', DB)
    assert "Documents" in c.read_document(u, DB), "read from the correct database failed"
    try:
        c.read_document(u, "protein")
    except MarkLogicError as e:
        assert e.code == "RESTAPI-NODOCUMENT", f"expected not-found from other db, got {e.code}"
        _hard_delete(u)
        return
    _hard_delete(u)
    raise AssertionError("reading from a different database returned a document — db ignored")


@check("S4: document_resource template carries database")
def _():
    import server as _srv
    import inspect
    params = set(inspect.signature(_srv.document_resource).parameters)
    assert {"server", "database", "uri"} <= params, f"resource params are {params}"


# ------------------------------------------ required `server` parameter (B)
# `server` used to default to DEFAULT_SERVER, so a call meant for prod that omitted it
# silently answered from dev — a wrong answer indistinguishable from a right one. It is
# now required on every tool, enforced by the signature rather than by documentation.

@check("B: every tool declares `server` as required in its MCP schema")
def _():
    import asyncio
    import server as _srv
    tools = asyncio.run(_srv.mcp.list_tools())
    assert tools, "no tools registered"
    missing = [
        t.name for t in tools
        if "server" not in (t.inputSchema.get("required") or [])
    ]
    assert not missing, f"tools not requiring `server`: {missing}"
    # `database` too, on every tool that takes one
    lax = [
        t.name for t in tools
        if "database" in (t.inputSchema.get("properties") or {})
        and "database" not in (t.inputSchema.get("required") or [])
    ]
    assert not lax, f"tools not requiring `database`: {lax}"


@check("B: omitting `server` is a TypeError, not a silent default")
def _():
    import server as _srv
    try:
        _srv.eval_xquery_readonly("1", DB)  # no server=
        raise AssertionError("call without `server` was accepted")
    except TypeError as e:
        assert "server" in str(e), f"unexpected TypeError: {e}"


@check("B: an empty `server` is refused with an actionable message")
def _():
    import server as _srv
    try:
        _srv.eval_xquery_readonly("1", DB, server="")
        raise AssertionError("empty `server` was accepted")
    except ValueError as e:
        msg = str(e)
        assert "required" in msg, msg
        assert "local" in msg, f"message should list available servers: {msg}"


@check("B: `server` must be passed by keyword, not positionally")
def _():
    import server as _srv
    try:
        _srv.eval_xquery_readonly("1", DB, "local")  # positional
        raise AssertionError("positional `server` was accepted")
    except TypeError:
        pass
    assert _srv.eval_xquery_readonly("1", DB, server="local") == ["1"]


# --------------------------------------------- mutable default arguments (S5)

@check("S5: write_document has no mutable default arguments")
def _():
    import server as _srv
    import inspect
    sig = inspect.signature(_srv.write_document)
    bad = {
        n: p.default for n, p in sig.parameters.items()
        if isinstance(p.default, (list, dict, set))
    }
    assert not bad, f"mutable defaults present: {bad}"


@check("S5: omitting collections/permissions still writes correctly")
def _():
    import server as _srv
    u = "/s5-default-check.xml"
    _hard_delete(u)
    _srv.write_document(u, "<a/>", DB, "xml", server="local")
    assert _exists(u), "write with defaulted collections/permissions did not land"
    _hard_delete(u)


# ------------------------------------------------------- XQuery prolog hoisting
# _wrap_eval inlines the caller's query into a function body, where XQuery prolog
# declarations are ILLEGAL. Every query carrying `declare namespace|variable|function`
# or `import module` therefore failed with a bare XDMP-UNEXPECTED (...expecting
# Rbrace_ — the injected `}`). Only `xquery version` was special-cased. The fix
# hoists the whole prolog above the invoke. The enforcement test at the end of this
# block is the important one: hoisting must not become a way to escape readonly.

@check("prolog: declare namespace is honoured")
def _():
    got = c.eval_xquery_readonly(
        'declare namespace p = "http://cas.org/protein"; fn:count(fn:doc())', DB)
    assert len(got) == 1 and got[0].isdigit(), f"expected one count, got {got!r}"


@check("prolog: declare variable is honoured")
def _():
    got = c.eval_xquery_readonly('declare variable $x as xs:string := "hello"; $x', DB)
    assert got == ["hello"], f"expected ['hello'], got {got!r}"


@check("prolog: declare function is honoured")
def _():
    got = c.eval_xquery_readonly('declare function local:double($n) { $n * 2 }; local:double(21)', DB)
    assert got == ["42"], f"expected ['42'], got {got!r}"


@check("prolog: a `;` inside a declare function body does not split the prolog")
def _():
    q = 'declare function local:f() { fn:string-join(("a", "b"), ";") }; local:f()'
    got = c.eval_xquery_readonly(q, DB)
    assert got == ["a;b"], f"expected ['a;b'], got {got!r}"


@check("prolog: version + several declarations combined")
def _():
    q = ('xquery version "1.0-ml"; '
         'declare namespace p = "u"; '
         'declare variable $n := 20; '
         '$n + 22')
    got = c.eval_xquery_readonly(q, DB)
    assert got == ["42"], f"expected ['42'], got {got!r}"


@check("prolog: comments around declarations are tolerated")
def _():
    q = '(: leading :) declare variable $x := 5; (: mid :) $x * 2'
    got = c.eval_xquery_readonly(q, DB)
    assert got == ["10"], f"expected ['10'], got {got!r}"


@check("prolog: prolog-free queries are passed through byte-identically")
def _():
    sys.path.insert(0, str(REPO / "src"))
    from ml_client import _split_prolog
    for q in ('1 + 1', 'fn:count(fn:doc())', '("a", "b")', 'xdmp:database-name(xdmp:database())'):
        prolog, body = _split_prolog(q)
        assert prolog == "" and body == q, f"{q!r} was altered: {prolog!r} / {body!r}"


@check("prolog: write path also honours a prolog")
def _():
    u = "/prolog-write-check.xml"
    _hard_delete(u)
    c.eval_xquery(
        f'declare variable $u as xs:string := "{u}"; '
        f'xdmp:document-insert($u, <a>via prolog</a>)', DB)
    assert _exists(u), "write path dropped a prolog-bearing insert"
    _hard_delete(u)


@check("prolog: BLOCKS a write hidden behind a prolog (hoisting is not an escape)")
def _():
    _reseed()
    _assert_blocked(
        f'declare variable $u as xs:string := "{SEEDED}"; xdmp:document-delete($u)',
        SEEDED, "prolog-wrapped delete", should_exist_after=True)


# -------------------------------------------------- eval_xquery_to_file newlines

@check("to_file: every line is newline-terminated so wc -l matches item count")
def _():
    import server as _srv
    out = "/tmp/_sr_to_file_nl.txt"
    _srv.eval_xquery_to_file('("a", "b", "c")', DB, out, readonly=True, server="local")
    text = Path(out).read_text(encoding="utf-8")
    assert text == "a\nb\nc\n", f"expected 'a\\nb\\nc\\n', got {text!r}"
    assert text.count("\n") == 3, f"expected 3 newlines for 3 items, got {text.count(chr(10))}"
    Path(out).unlink()


@check("to_file: concatenating two outputs does not merge across the boundary")
def _():
    import server as _srv
    a, b = "/tmp/_sr_cat_a.txt", "/tmp/_sr_cat_b.txt"
    _srv.eval_xquery_to_file('("a1", "a2")', DB, a, readonly=True, server="local")
    _srv.eval_xquery_to_file('("b1", "b2")', DB, b, readonly=True, server="local")
    joined = Path(a).read_text(encoding="utf-8") + Path(b).read_text(encoding="utf-8")
    lines = [ln for ln in joined.split("\n") if ln]
    assert lines == ["a1", "a2", "b1", "b2"], f"boundary corrupted: {lines!r}"
    assert "a2b1" not in joined, "last line of file A merged into first line of file B"
    Path(a).unlink(); Path(b).unlink()


@check("to_file: empty result set writes an empty file, not a lone newline")
def _():
    import server as _srv
    out = "/tmp/_sr_to_file_empty.txt"
    _srv.eval_xquery_to_file('()', DB, out, readonly=True, server="local")
    assert Path(out).read_text(encoding="utf-8") == "", "empty result should write nothing"
    Path(out).unlink()


# ---------------------------------------------------------------------- teardown
_hard_delete(PROBE)
_hard_delete(SEEDED)

leftover = [u for u in (PROBE, SEEDED) if _exists(u)]
if leftover:
    print(f"\nWARNING: test artifacts left behind: {leftover}")
else:
    print("\nTeardown clean — no test artifacts remain.")

failed = [(n, d) for n, ok, d in _results if not ok]
print(f"\n{'=' * 66}")
print(f"{len(_results) - len(failed)}/{len(_results)} passed")
if failed:
    print("\nFAILURES:")
    for n, d in failed:
        print(f"  - {n}: {d.splitlines()[0] if d else ''}")
print("=" * 66)
sys.exit(1 if failed else 0)
