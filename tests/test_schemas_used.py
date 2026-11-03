#!/usr/bin/env python3
"""
Offline tests for _schemas_used (validate_documents' schema-version reporting).

No server needed. Run:
    cd ~/repos/ml_mcp_server && .venv/bin/python tests/test_schemas_used.py

Background: validate_documents infers schema_in_scope from the first document's
errors. On 2026-11-03 prod `protein` resolved to the stale v3 XSD; the same database
returned schema_in_scope=true for three new v5 records (XDMP-VALIDATEUNEXPECTED) and
false for a collection sample whose first document hit XDMP-VALIDATENODECL. Neither
result said which schema was used. These details are verbatim from those calls.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from ml_client import _no_schema_warning, _schemas_used  # noqa: E402

V3 = "/xsd/base/protein/3/protein-bmv.xsd"

UNEXPECTED = {
    "uri": "protein/0a5c76ae-2914-350f-a653-ba52df993461",
    "code": "XDMP-VALIDATEUNEXPECTED",
    "detail": 'XDMP-VALIDATEUNEXPECTED: (err:XQDY0027) validate full strict { () } -- Invalid node: '
              'Found collection but expected (context,id,match-string?,is-displayable,is-matchable,'
              'is-preferred,evidence*) at fn:doc("protein/0a5c76ae-2914-350f-a653-ba52df993461")'
              '/protein/protein-identifier[1]/collection using schema "' + V3 + '"',
}
NODECL_V3 = {
    "uri": "protein/000001af-7637-3aff-94fc-e737791cc553",
    "code": "XDMP-VALIDATENODECL",
    "detail": 'XDMP-VALIDATENODECL: (err:XQDY0084) validate full strict { () } -- Missing element '
              'declaration: Expected declaration for node fn:doc("protein/000001af-7637-3aff-94fc-'
              'e737791cc553")/protein/encoding-gene/encoding-gene-uri in non-lax mode using schema "'
              + V3 + '"',
}
NODECL_EMPTY = {  # local, no schema loaded at all
    "uri": "protein/7ee946b5-16ae-36a7-a155-d4cb4dd508f9",
    "code": "XDMP-VALIDATENODECL",
    "detail": 'XDMP-VALIDATENODECL: (err:XQDY0084) validate full strict { () } -- Missing element '
              'declaration: Expected declaration for node fn:doc("protein/7ee946b5-16ae-36a7-a155-'
              'd4cb4dd508f9")/protein in non-lax mode using schema ""',
}


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r}")
    return ok


results = [
    check("stale v3, unexpected-node errors", _schemas_used([UNEXPECTED] * 3), [V3]),
    check("stale v3, mixed codes dedupe to one path", _schemas_used([NODECL_V3, UNEXPECTED]), [V3]),
    check("no schema loaded -> empty path dropped", _schemas_used([NODECL_EMPTY]), []),
    check("missing detail key tolerated", _schemas_used([{"code": "MISSING-DOCUMENT"}]), []),
    check("two versions both reported, in order",
          _schemas_used([UNEXPECTED, {"detail": 'x using schema "/xsd/base/protein/5/protein-bmv.xsd"'}]),
          [V3, "/xsd/base/protein/5/protein-bmv.xsd"]),
    # The prod 5-doc collection sample: schema_in_scope=false, yet v3 IS loaded.
    check("stale-version warning names the loaded XSD and does not claim 'no XSD loaded'",
          (lambda w: "DOES hold an XSD" in w and V3 in w and "no XSD loaded" not in w)(
              _no_schema_warning("protein", _schemas_used([NODECL_V3]))),
          True),
    check("genuinely empty schema DB keeps the 'no XSD loaded' wording",
          (lambda w: w.startswith("NO SCHEMA IN SCOPE") and "no XSD loaded" in w
                     and "'protein-schemas'" in w)(
              _no_schema_warning("protein", _schemas_used([NODECL_EMPTY]))),
          True),
]
sys.exit(0 if all(results) else 1)
