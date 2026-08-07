import html
import json
import re
import requests
from requests.auth import HTTPDigestAuth


class MarkLogicError(RuntimeError):
    """A MarkLogic request failed.

    Carries the *parsed* MarkLogic diagnostic rather than only an HTTP status, so a
    caller sees `XDMP-UPDATEFUNCTIONFROMQUERY: Cannot apply an update function from a
    query` instead of a bare `500 Server Error`. MarkLogic reports faults in two
    different shapes depending on endpoint (HTML from /v1/eval, JSON from
    /v1/documents), and neither is readable raw.

    Attributes:
        status:    HTTP status code, or None for transport-level failures.
        code:      MarkLogic message code, e.g. "XDMP-NOSUCHDB", "RESTAPI-NODOCUMENT",
                   or the synthetic "TIMEOUT" / "CONNECTION" for transport failures.
        detail:    The primary diagnostic line, without the stack frames.
        frames:    Stack frames MarkLogic reported, outermost first.
        retryable: True when retrying unchanged is a reasonable next step (timeouts,
                   connection failures). False for deterministic faults such as a
                   syntax error or a rejected update.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        frames: list[str] | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail
        self.frames = frames or []
        self.retryable = retryable


_DT_RE = re.compile(r"<dt>(.*?)</dt>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_CODE_RE = re.compile(
    r"\b((?:XDMP|RESTAPI|SEC|TS|ADMIN|MANAGE|JS|SQL|OPTIC|TDE|XSLT|SVC|XI)-[A-Z0-9]+)\b"
)


def _xq_str(s: str) -> str:
    """Escape a string for use inside an XQuery double-quoted string literal.

    XQuery has NO backslash escapes: a literal `"` is written by doubling it, and a
    backslash is an ordinary character. The backslash-escaping this module previously
    used produced invalid XQuery for any value containing a double quote — which for
    a caller-supplied collection name or search term is both a robustness bug and an
    injection vector.
    """
    return s.replace('"', '""')


class MarkLogicClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8000,
        user: str = "",
        password: str = "",
        use_https: bool = False,
        ssl_ca_bundle: str | bool = True,
        default_permissions: str = "rest-reader:read",
    ):
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"
        self.default_permissions = self._parse_permissions(default_permissions)
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(user, password)
        self.session.verify = ssl_ca_bundle

    @staticmethod
    def _parse_permissions(perm_str: str) -> list[tuple[str, str]]:
        result = []
        for pair in perm_str.split(","):
            pair = pair.strip()
            if ":" in pair:
                role, cap = pair.split(":", 1)
                result.append((role.strip(), cap.strip()))
        return result

    # ------------------------------------------------------------------ errors

    @staticmethod
    def _parse_error(resp: requests.Response) -> tuple[str | None, str, list[str]]:
        """Extract (code, primary message, stack frames) from a MarkLogic error body.

        Handles both shapes MarkLogic actually returns, and degrades to a cleaned
        text snippet for anything unrecognised rather than losing the body entirely.
        """
        text = resp.text or ""
        ctype = resp.headers.get("Content-Type", "")

        # /v1/documents, /v1/search and friends: JSON errorResponse envelope.
        if "json" in ctype.lower():
            try:
                er = json.loads(text).get("errorResponse") or {}
                msg = (er.get("message") or er.get("status") or "").strip()
                if msg or er.get("messageCode"):
                    return er.get("messageCode"), msg, []
            except (ValueError, AttributeError, TypeError):
                pass

        # /v1/eval: an HTML error page; the diagnostic and each stack frame are <dt>s.
        dts: list[str] = []
        for raw in _DT_RE.findall(text):
            cleaned = html.unescape(_TAG_RE.sub("", raw))
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if cleaned:
                dts.append(cleaned)
        if dts:
            primary = dts[0]
            m = _CODE_RE.search(primary)
            return (m.group(1) if m else None), primary, dts[1:]

        snippet = re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()
        return None, snippet[:300], []

    def _check(self, resp: requests.Response, action: str) -> requests.Response:
        if resp.status_code < 400:
            return resp
        code, detail, frames = self._parse_error(resp)
        if code and detail and not detail.startswith(code):
            head = f"{code}: {detail}"
        else:
            head = detail or f"HTTP {resp.status_code}"
        message = f"{action} failed (HTTP {resp.status_code}): {head}"
        if frames:
            message += "\n  reported at: " + " | ".join(frames[:2])
        raise MarkLogicError(
            message,
            status=resp.status_code,
            code=code,
            detail=detail,
            frames=frames,
        )

    def _request(self, method: str, path: str, *, action: str, timeout: int, **kwargs):
        """Issue a request, converting transport and HTTP failures into MarkLogicError."""
        try:
            resp = self.session.request(method, f"{self.base_url}{path}", timeout=timeout, **kwargs)
        except requests.exceptions.Timeout as e:
            raise MarkLogicError(
                f"{action} timed out after {timeout}s with no response from {self.base_url}. "
                "A timeout here is usually load, not a wrong query: MarkLogic under "
                "post-write merge/index pressure regularly exceeds this, and bundling "
                "several expensive cts:search/xdmp:estimate calls into one request makes "
                "it much more likely. Retry, and split the work into one or two expensive "
                "calls per request rather than concluding the query or the write failed.",
                code="TIMEOUT",
                retryable=True,
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise MarkLogicError(
                f"{action} could not reach {self.base_url}: {e}. Check the server is "
                "running and reachable — dev/prod additionally need VPN and a current "
                "AWS SSO session.",
                code="CONNECTION",
                retryable=True,
            ) from e
        return self._check(resp, action)

    # --------------------------------------------------------------- documents

    def read_document(self, uri: str, database: str = "") -> str:
        params: list = [("uri", uri)]
        if database:
            params.append(("database", database))
        r = self._request(
            "GET", "/v1/documents", action=f"read_document({uri})", timeout=60, params=params
        )
        return r.text

    def write_document(
        self,
        uri: str,
        content: str,
        content_type: str = "application/json",
        collections: list[str] | None = None,
        permissions: list[tuple[str, str]] | None = None,
        database: str = "",
    ) -> None:
        effective_perms = permissions if permissions is not None else self.default_permissions
        params: list = [("uri", uri)]
        if database:
            params.append(("database", database))
        for col in (collections or []):
            params.append(("collection", col))
        for role, cap in effective_perms:
            params.append((f"perm:{role}", cap))
        self._request(
            "PUT",
            "/v1/documents",
            action=f"write_document({uri})",
            timeout=60,
            params=params,
            data=content.encode("utf-8"),
            headers={"Content-Type": content_type},
        )

    # ------------------------------------------------------------------- query

    @staticmethod
    def _wrap_eval(xquery: str, database: str = "", readonly: bool = False) -> str:
        """Wrap a query in `xdmp:invoke-function` to apply eval options.

        Two options are expressed here, and both must be, because both are
        *request*-scoped:

        - `<database>` targets another database. Doing so forces MarkLogic to run the
          body in a NEW transaction (a transaction is bound to one database).
        - `<update>false</update>` makes that transaction a query transaction, so any
          update is rejected.

        Read-only intent CANNOT be expressed as `declare option
        xdmp:transaction-mode "query"` instead, for two independent reasons:

        1. It would not reach the body — a prolog declaration in the calling module
           does not cross the new transaction the `<database>` option creates. (Nor
           can it be moved inside the closure: `declare option` is a prolog
           declaration and is illegal in a function body.)
        2. It is SESSION-scoped, not request-scoped. MarkLogic keeps it against the
           session bound to the keep-alive HTTP connection, so one readonly eval
           poisoned every later write on the same client — `write_document` then
           failed with XDMP-UPDATEFUNCTIONFROMQUERY until a fresh connection was
           made. See tests/test_readonly_enforcement.py, "no session-mode leak".

        Returns `xquery` unchanged when neither option is needed, so a plain
        update-mode eval against the default database is not wrapped at all.
        """
        opts: list[str] = []
        if database:
            escaped_db = _xq_str(database)
            opts.append(f'<database>{{xdmp:database("{escaped_db}")}}</database>')
        if readonly:
            opts.append("<update>false</update>")
        if not opts:
            return xquery
        return (
            f'xdmp:invoke-function(function() {{ {xquery} }}, '
            f'<options xmlns="xdmp:eval">{"".join(opts)}</options>)'
        )

    def _search_via_xquery(
        self,
        query: str,
        collection: str,
        limit: int,
        offset: int,
        database: str,
    ) -> dict:
        conditions = []
        if query:
            safe_q = _xq_str(query)
            conditions.append(f'cts:word-query("{safe_q}")')
        if collection:
            safe_c = _xq_str(collection)
            conditions.append(f'cts:collection-query("{safe_c}")')
        cts_q = f'cts:and-query(({", ".join(conditions)}))' if conditions else "cts:true-query()"
        xq = (
            f"let $q := {cts_q}\n"
            f"let $total := xdmp:estimate(cts:search(fn:doc(), $q))\n"
            f'let $uris := fn:subsequence(cts:uris("", ("document"), $q), {offset}, {limit})\n'
            f"return (fn:string($total), $uris)"
        )
        results = self.eval_xquery(xq, database)
        if not results:
            return {"n": 0, "uris": []}
        return {"n": int(results[0]), "uris": results[1:]}

    def search_documents(
        self,
        query: str = "",
        collection: str = "",
        limit: int = 10,
        offset: int = 1,
        snippets: bool = False,
        database: str = "",
    ) -> dict:
        if database:
            # App-Services port 8000 ignores the database query param; use XQuery eval instead
            return self._search_via_xquery(query, collection, limit, offset, database)
        params: dict = {"pageLength": limit, "start": offset, "format": "json"}
        if query:
            params["q"] = query
        if collection:
            params["collection"] = collection
        r = self._request(
            "GET",
            "/v1/search",
            action="search_documents",
            timeout=60,
            params=params,
            headers={"Accept": "application/json"},
        )
        data = r.json()
        result: dict = {
            "n": data.get("total", 0),
            "uris": [res["uri"] for res in data.get("results", [])],
        }
        if snippets:
            result["snippets"] = [res.get("snippet", {}) for res in data.get("results", [])]
        return result

    def validate_documents(
        self,
        database: str,
        uris: list[str] | None = None,
        collection: str = "",
        limit: int = 100,
        mode: str = "strict",
    ) -> dict:
        """Validate stored documents against their in-scope schema.

        MarkLogic does not validate on insert, so a schema-invalid document is stored
        silently and is indistinguishable from a valid one by any count or existence
        check. This is the only way to detect that from the client.

        `xdmp:validate` returns a single `<xdmp:validation-errors>` wrapper and never
        raises; an empty wrapper means valid.
        """
        if mode not in ("strict", "lax"):
            raise ValueError(f"mode must be 'strict' or 'lax', got {mode!r}")
        limit = max(1, min(int(limit), 1000))

        truncated = False
        if uris:
            selected = list(uris)
            if len(selected) > limit:
                selected = selected[:limit]
                truncated = True
            uri_expr = "(" + ", ".join(f'"{_xq_str(u)}"' for u in selected) + ")"
        elif collection:
            uri_expr = (
                'fn:subsequence(cts:uris("", (), cts:collection-query('
                f'"{_xq_str(collection)}")), 1, {limit})'
            )
        else:
            raise ValueError(
                "validate_documents needs either uris=[...] or collection=... "
                "to decide what to validate"
            )

        # Namespace-wildcard (*:error) rather than a `declare namespace` prolog: this
        # body is wrapped in a function by _wrap_eval, and prolog declarations are not
        # legal inside a function body.
        xq = f"""let $uris := {uri_expr}
let $probe :=
  if (fn:exists($uris) and fn:doc-available($uris[1]))
  then fn:exists(xdmp:validate(fn:doc($uris[1]), "strict")
                   /*:error[*:code = "XDMP-VALIDATENODECL"])
  else fn:false()
return (
  fn:concat("SCHEMA_IN_SCOPE:", if ($probe) then "false" else "true"),
  fn:concat("CHECKED:", fn:count($uris)),
  for $u in $uris
  return
    if (fn:not(fn:doc-available($u)))
    then xdmp:to-json-string(map:new((
           map:entry("uri", $u),
           map:entry("code", "MISSING-DOCUMENT"),
           map:entry("message", "document does not exist in this database"))))
    else
      let $e := xdmp:validate(fn:doc($u), "{mode}")/*:error
      return
        if (fn:empty($e)) then ()
        else xdmp:to-json-string(map:new((
               map:entry("uri", $u),
               map:entry("code", fn:string($e[1]/*:code)),
               map:entry("message", fn:string($e[1]/*:message)),
               map:entry("detail", fn:string($e[1]/*:format-string)),
               map:entry("error_count", fn:count($e)))))
)"""
        results = self.eval_xquery_readonly(xq, database)

        checked = 0
        schema_in_scope = True
        findings: list[dict] = []
        for item in results:
            if item.startswith("SCHEMA_IN_SCOPE:"):
                schema_in_scope = item.split(":", 1)[1] == "true"
                continue
            if item.startswith("CHECKED:"):
                try:
                    checked = int(item.split(":", 1)[1])
                except ValueError:
                    pass
                continue
            try:
                findings.append(json.loads(item))
            except ValueError:
                findings.append({"uri": None, "code": "UNPARSEABLE", "message": item[:300]})

        invalid = [f for f in findings if f.get("code") != "MISSING-DOCUMENT"]
        missing = [f for f in findings if f.get("code") == "MISSING-DOCUMENT"]
        result = {
            "checked": checked,
            "invalid": len(invalid),
            "missing": len(missing),
            "mode": mode,
            "schema_in_scope": schema_in_scope,
            "truncated": truncated,
            "errors": invalid + missing,
        }
        if not schema_in_scope:
            # Without a schema loaded, "lax" validates nothing and reports zero errors —
            # a clean-looking result that verifies nothing at all. Refuse to look clean.
            result["warning"] = (
                "NO SCHEMA IN SCOPE — this result does not tell you whether the "
                f"documents are valid. MarkLogic found no element declaration for the "
                f"first document checked, which means the schema database attached to "
                f"'{database}' has no XSD loaded (check e.g. '{database}-schemas'). In "
                "'lax' mode that yields invalid=0, which must NOT be read as verified; "
                "in 'strict' mode it yields XDMP-VALIDATENODECL for every document, "
                "which reflects the missing schema rather than the data. Load the XSDs "
                "into the schema database, or validate out-of-band against the "
                "authoritative XSD service instead."
            )
        return result

    def eval_xquery_readonly(self, xquery: str, database: str = "") -> list[str]:
        """Execute XQuery with updates rejected.

        Enforced by the `<update>false</update>` eval option (see `_wrap_eval`), which
        is request-scoped and applies whether or not a database was named.

        Two earlier bugs are both guarded by tests, and neither should be reintroduced
        by "simplifying" this back to a transaction-mode declaration:
          - the declaration did not reach the body when a database was named, so every
            readonly call through the MCP tool silently permitted writes;
          - the declaration is session-scoped, so it broke all later writes on the
            same connection.
        """
        xq = xquery.strip()
        version_prefix = ""
        m = re.match(r'(xquery\s+version\s+[^;]+;)', xq, re.IGNORECASE)
        if m:
            version_prefix = m.group(1) + "\n"
            xq = xq[m.end():].lstrip()
        wrapped = self._wrap_eval(xq, database, readonly=True)
        # Pass database="" — the wrap above already applied it; wrapping twice would
        # nest the invoke and re-enter a second transaction for no reason.
        return self.eval_xquery(f"{version_prefix}{wrapped}")

    def eval_xquery(self, xquery: str, database: str = "") -> list[str]:
        actual_xquery = self._wrap_eval(xquery, database)
        r = self._request(
            "POST",
            "/v1/eval",
            action="eval_xquery",
            timeout=600,
            data={"xquery": actual_xquery},
            params={},
            headers={"Accept": "multipart/mixed"},
        )
        content_type = r.headers.get("Content-Type", "")
        if "multipart/mixed" not in content_type:
            return [r.text] if r.text else []
        boundary = re.search(r"boundary=([^\s;]+)", content_type)
        if not boundary:
            return [r.text]
        results = []
        for part in r.text.split(f"--{boundary.group(1)}"):
            if not part.strip() or part.strip() == "--":
                continue
            if "\r\n\r\n" in part:
                body = part.split("\r\n\r\n", 1)[1].strip()
            elif "\n\n" in part:
                body = part.split("\n\n", 1)[1].strip()
            else:
                body = part.strip()
            if body and body != "--":
                results.append(body)
        return results

    def ping(self) -> bool:
        try:
            r = self.session.get(
                f"{self.base_url}/v1/search",
                params={"pageLength": 0},
                timeout=5,
            )
            return r.status_code == 200
        except Exception:
            return False
