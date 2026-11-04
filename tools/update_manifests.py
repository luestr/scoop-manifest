#!/usr/bin/env python3
"""Maintain every Scoop manifest in a bucket.

A pure-stdlib (Python 3.8+) replacement for the GitHub Actions "Excavator":
it walks ``bucket/*.json``, resolves the newest upstream version through each
manifest's ``checkver`` block, then rewrites ``version`` / ``url`` / ``hash``
(and ``extract_dir`` or anything else carrying a version variable) using the
manifest's ``autoupdate`` block.

Supported checkver strategies
-----------------------------
* ``"checkver": "github"``                -- repository taken from ``homepage``
* ``{"github": "<repo or API url>"}``     -- explicit repository
* ``{"url": "<url>", "regex"|"jsonpath"|"xpath": ...}`` -- generic scraping
* ``checkver.regex`` / ``checkver.replace`` are honored in every mode.

Supported hash sources (in the order this script tries them)
------------------------------------------------------------
1. ``autoupdate.hash.url`` + ``find``/``regex`` (text) or ``jsonpath`` (json)
2. The digest GitHub publishes for every release asset, read from the public
   ``releases/expanded_assets/<tag>`` page (no API quota), with the REST API as
   a fallback
3. Download the asset and compute sha256 locally (last resort, ``--no-download``
   turns it off)

GitHub rate limits
------------------
HTML scraping (what Scoop's own checkver does for ``"checkver": "github"``) is
not rate limited. The 60 req/h anonymous REST limit only applies to manifests
that hardcode ``api.github.com``; those fall back to equivalent HTML pages
automatically. Set ``GITHUB_TOKEN`` or pass ``--token`` to use the API with a
5000 req/h quota instead.

Usage
-----
    python tools/update_manifests.py                  # update everything
    python tools/update_manifests.py -n               # dry run, report only
    python tools/update_manifests.py kitter-desktop   # selected apps
    python tools/update_manifests.py -f               # recompute even if same
    python tools/update_manifests.py --commit         # git commit the result
    python tools/update_manifests.py --commit --push  # ... and push
    python tools/update_manifests.py --no-download    # never fetch asset bytes

Exit codes: 0 = ok (with or without changes), 1 = at least one manifest failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

USER_AGENT = "Scoop/1.0 (+https://scoop.sh)"
DEFAULT_TIMEOUT = 60
RETRIES = 3

# Built-in default patterns, mirroring bin/checkver.ps1
TAG_SHAPED_REGEX = r"^(?:v|V)?(?P<version>[\d][\w.+-]*)$"
DEFAULT_HTML_REGEX = r"/releases/tag/(?:v|V)?([\d.-]+)"
GITHUB_RELEASE_DOWNLOAD = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/[^/]+/"
)

# .NET named-group / backreference syntax, which Scoop accepts because it compiles
# manifests with System.Text.RegularExpressions. Python's ``re`` needs the ``P``.
DOTNET_GROUP_RE = re.compile(r"\(\?(?:<(?![=!])|')(?P<name>[\w]+)(?:>|')")
DOTNET_BACKREF_RE = re.compile(r"\\k<(?P<name>[\w]+)>|\\k'(?P<name2>[\w]+)'")


def translate_scoop_regex(pattern: str) -> str:
    """Rewrite .NET-only regex syntax into its Python ``re`` equivalent.

    Scoop compiles every manifest pattern with System.Text.RegularExpressions, so a
    bucket may legitimately contain ``(?<version>...)`` or ``\\k<version>``. Python
    rejects both, so translate them before compiling and leave everything else --
    including lookarounds such as ``(?=`` / ``(?!`` / ``(?<=`` / ``(?<!`` -- untouched.
    """
    pattern = DOTNET_GROUP_RE.sub(lambda m: f"(?P<{m.group('name')}>", pattern)
    pattern = DOTNET_BACKREF_RE.sub(
        lambda m: f"(?P={m.group('name') or m.group('name2')})", pattern
    )
    return pattern


def compile_scoop_regex(pattern: str, flags: int = 0) -> "re.Pattern[str]":
    """Compile a manifest-supplied regex, accepting .NET syntax."""
    return re.compile(translate_scoop_regex(pattern), flags)



# --------------------------------------------------------------------------- #
# console helpers
# --------------------------------------------------------------------------- #
def _init_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(msg)


def ok(msg: str) -> None:
    print(_c(msg, "32"))


def warn(msg: str) -> None:
    print(_c(f"warning: {msg}", "33"), file=sys.stderr)


def error(msg: str) -> None:
    print(_c(f"error: {msg}", "31"), file=sys.stderr)


class UpdateError(Exception):
    """Raised when a manifest cannot be maintained."""


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class Http:
    """Tiny urllib wrapper with retries, a Scoop user agent and optional token."""

    def __init__(self, token: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.token = token
        self.timeout = timeout
        self._json_cache: Dict[str, Any] = {}

    def _open(self, url: str, accept: str = "*/*"):
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if self.token and "api.github.com" in url:
            headers["Authorization"] = f"token {self.token}"
        request = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(request, timeout=self.timeout)

    def _with_retries(self, func, url: str):
        last: Optional[Exception] = None
        for attempt in range(RETRIES):
            try:
                return func()
            except urllib.error.HTTPError as exc:
                if exc.code == 403 and "api.github.com" in url:
                    raise UpdateError(
                        "GitHub API rate limit hit while reading "
                        f"{url} -- set GITHUB_TOKEN or pass --token to raise the 60 req/h limit"
                    ) from exc
                if exc.code in (400, 401, 403, 404, 410, 422):
                    raise UpdateError(f"HTTP {exc.code} for {url}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            if attempt + 1 < RETRIES:
                time.sleep(1.5 * (attempt + 1))
        raise UpdateError(f"cannot fetch {url}: {last}")

    def text(self, url: str) -> str:
        return self.request(url)[1]

    def request(self, url: str) -> Tuple[str, str]:
        """GET ``url`` and return ``(final_url, body)`` after redirects."""

        def run() -> Tuple[str, str]:
            with self._open(url) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return response.geturl(), raw.decode(charset, "replace")

        return self._with_retries(run, url)

    def json(self, url: str) -> Any:
        if url in self._json_cache:
            return self._json_cache[url]

        def run() -> str:
            with self._open(url, accept="application/vnd.github+json, application/json") as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "replace")

        body = self._with_retries(run, url)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise UpdateError(f"invalid JSON from {url}: {exc}") from exc
        self._json_cache[url] = parsed
        return parsed

    def sha256(self, url: str) -> str:
        def run() -> str:
            digest = hashlib.sha256()
            with self._open(url) as response:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()

        return self._with_retries(run, url)

# --------------------------------------------------------------------------- #
# JSONPath subset -- enough for Scoop manifests
# --------------------------------------------------------------------------- #
def jsonpath_find(data: Any, expression: str) -> List[Any]:
    """Evaluate a small but practical subset of JSONPath.

    Supports ``$``, ``.name``, ``['name']``, ``[*]``, ``[n]``, ``[a:b]``,
    ``..name`` (recursive descent) and one trailing ``[?(<expr>)]`` filter
    whose operands may be ``@.field`` or literals.
    """
    tokens = _tokenize_path(expression)
    current: List[Any] = [data]
    for kind, value in tokens:
        nxt: List[Any] = []
        if kind == "child":
            for item in current:
                if isinstance(item, dict) and value in item:
                    nxt.append(item[value])
        elif kind == "index":
            for item in current:
                if isinstance(item, list) and -len(item) <= value < len(item):
                    nxt.append(item[value])
                elif isinstance(item, dict) and str(value) in item:
                    nxt.append(item[str(value)])
        elif kind == "wildcard":
            for item in current:
                if isinstance(item, list):
                    nxt.extend(item)
                elif isinstance(item, dict):
                    nxt.extend(item.values())
        elif kind == "descend":
            def walk(node: Any) -> Iterable[Any]:
                if isinstance(node, dict):
                    for key, val in node.items():
                        if key == value:
                            yield val
                        yield from walk(val)
                elif isinstance(node, list):
                    for element in node:
                        yield from walk(element)

            for item in current:
                nxt.extend(walk(item))
        elif kind == "slice":
            start, stop = value
            for item in current:
                if isinstance(item, list):
                    nxt.extend(item[start:stop])
        elif kind == "filter":
            for item in current:
                pool = item if isinstance(item, list) else [item]
                for element in pool:
                    if _eval_filter(element, value):
                        nxt.append(element)
        current = nxt
        if not current:
            return []
    return current


def _tokenize_path(expression: str) -> List[Tuple[str, Any]]:
    text = expression.strip()
    if text.startswith("$"):
        text = text[1:]
    tokens: List[Tuple[str, Any]] = []
    i = 0
    while i < len(text):
        char = text[i]
        if text.startswith("..", i):
            i += 2
            match = re.match(r"[\w\-*]+", text[i:])
            if not match:
                raise UpdateError(f"bad JSONPath (recursive descent): {expression}")
            name = match.group(0)
            i += len(name)
            if name == "*":
                raise UpdateError(f"bad JSONPath (recursive wildcard): {expression}")
            tokens.append(("descend", name))
        elif char == ".":
            i += 1
            if i < len(text) and text[i] == "*":
                i += 1
                tokens.append(("wildcard", None))
                continue
            match = re.match(r"[\w\-]+", text[i:])
            if not match:
                raise UpdateError(f"bad JSONPath (child): {expression}")
            tokens.append(("child", match.group(0)))
            i += len(match.group(0))
        elif char == "[":
            depth = 1
            j = i + 1
            while j < len(text) and depth:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                elif text[j] in "'\"":
                    quote = text[j]
                    j += 1
                    while j < len(text) and text[j] != quote:
                        if text[j] == "\\":
                            j += 1
                        j += 1
                j += 1
            inner = text[i + 1:j - 1].strip()
            i = j
            if inner == "*":
                tokens.append(("wildcard", None))
            elif inner.startswith("?(") and inner.endswith(")"):
                tokens.append(("filter", inner[2:-1].strip()))
            elif inner.startswith(("'", '"')):
                tokens.append(("child", inner[1:-1]))
            elif re.fullmatch(r"-?\d+", inner):
                tokens.append(("index", int(inner)))
            elif re.fullmatch(r"-?\d*:-?\d*", inner):
                parts = inner.split(":")
                start = int(parts[0]) if parts[0] else None
                stop = int(parts[1]) if parts[1] else None
                tokens.append(("slice", (start, stop)))
            else:
                raise UpdateError(f"unsupported JSONPath segment [{inner}] in {expression}")
        else:
            raise UpdateError(f"bad JSONPath near {text[i:]!r} in {expression}")
    return tokens


FILTER_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<and>&&|\band\b)
      | (?P<or>\|\||\bor\b)
      | (?P<not>!|\bnot\b)
      | (?P<path>@(?:\.\w+|\[[^\]]*\])*)
      | (?P<op>==|!=|=~|<=|>=|<|>)
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
      | (?P<literal>true|false|null)
    )
    """,
    re.VERBOSE,
)

FILTER_COMPARE_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "=~": lambda a, b: bool(re.search(str(b), str(a))),
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


class _FilterParser:
    """Recursive-descent parser for the boolean subset of JSONPath filters.

    Grammar::

        expr   := term (('||' | 'or') term)*
        term   := factor (('&&' | 'and') factor)*
        factor := '!' factor | '(' expr ')' | comparison | operand
        comparison := operand op operand
    """

    def __init__(self, expression: str) -> None:
        self.tokens: List[Tuple[str, str]] = []
        position = 0
        text = expression.strip()
        while position < len(text):
            match = FILTER_TOKEN_RE.match(text, position)
            if not match:
                raise UpdateError(
                    f"unsupported JSONPath filter syntax at {text[position:]!r} in {expression!r}"
                )
            kind = match.lastgroup
            self.tokens.append((kind or "", match.group(kind or "")))
            position = match.end()
            while position < len(text) and text[position].isspace():
                position += 1

    def parse(self) -> str:
        node = self._expr()
        if self.tokens:
            raise UpdateError(f"unexpected trailing tokens in filter: {self.tokens!r}")
        return node

    def _peek(self) -> Tuple[str, str]:
        return self.tokens[0] if self.tokens else ("", "")

    def _take(self) -> Tuple[str, str]:
        if not self.tokens:
            raise UpdateError("filter expression ended unexpectedly")
        return self.tokens.pop(0)

    def _expr(self) -> str:
        node = self._term()
        while self._peek()[0] == "or":
            self._take()
            node = ("or", node, self._term())
        return node

    def _term(self) -> str:
        node = self._factor()
        while self._peek()[0] == "and":
            self._take()
            node = ("and", node, self._factor())
        return node

    def _factor(self) -> str:
        kind, value = self._peek()
        if kind == "not":
            self._take()
            return ("not", self._factor())
        if kind == "lparen":
            self._take()
            node = self._expr()
            if self._peek()[0] != "rparen":
                raise UpdateError("unbalanced parentheses in filter")
            self._take()
            return node
        left = self._operand()
        if self._peek()[0] == "op":
            _, operator = self._take()
            right = self._operand()
            return ("cmp", operator, left, right)
        return ("truthy", left)

    def _operand(self) -> Tuple[str, Any]:
        kind, value = self._take()
        if kind == "path":
            return ("path", value[1:])  # drop the "@"
        if kind == "number":
            return ("const", float(value) if "." in value else int(value))
        if kind == "string":
            return ("const", value[1:-1].replace("\\" + value[0], value[0]))
        if kind == "literal":
            return ("const", {"true": True, "false": False, "null": None}[value])
        raise UpdateError(f"expected an operand in filter, got {value!r}")


def _resolve_path(element: Any, path: str) -> Any:
    """Resolve a ``.field.sub`` / ``['key']`` chain inside one JSON object."""
    current: Any = element
    for segment in re.findall(r"\.(\w+)|\[\s*'([^']*)'\s*\]|\[\s*\"([^\"]*)\"\s*\]", path):
        key = next(part for part in segment if part)
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _eval_filter_node(element: Any, node: Any) -> bool:
    kind = node[0]
    if kind == "and":
        return _eval_filter_node(element, node[1]) and _eval_filter_node(element, node[2])
    if kind == "or":
        return _eval_filter_node(element, node[1]) or _eval_filter_node(element, node[2])
    if kind == "not":
        return not _eval_filter_node(element, node[1])
    if kind == "truthy":
        operand = node[1]
        return bool(_operand_value(element, operand))
    if kind == "cmp":
        _, operator, left, right = node
        actual = _operand_value(element, left)
        expected = _operand_value(element, right)
        comparison = FILTER_COMPARE_OPS[operator]
        try:
            return bool(comparison(actual, expected))
        except TypeError:
            return False
    raise UpdateError(f"internal: unknown filter node {kind!r}")


def _operand_value(element: Any, operand: Tuple[str, Any]) -> Any:
    kind, value = operand
    if kind == "path":
        return _resolve_path(element, value)
    return value


def _eval_filter(element: Any, expression: str) -> bool:
    """Evaluate a JSONPath filter expression against one candidate element."""
    if not isinstance(element, dict):
        return False
    node = _FilterParser(expression).parse()
    return _eval_filter_node(element, node)


# --------------------------------------------------------------------------- #
# XPath subset -- ``/a/b/text()``, ``//a``, ``@attr``, ``[n]``
# --------------------------------------------------------------------------- #
def xpath_find(text: str, expression: str) -> List[str]:
    # Reject DTDs outright: manifests only ever point at plain version feeds, and a
    # DOCTYPE is the vector for entity-expansion (billion laughs) attacks.
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
        raise UpdateError("refusing to parse XML that declares a DOCTYPE/ENTITY")
    try:
        parser = ET.XMLParser()
        parser.entity.clear()  # no predefined-entity resolution beyond the XML five
        root = ET.fromstring(text.encode("utf-8"), parser=parser)
    except ET.ParseError as exc:
        raise UpdateError(f"invalid XML: {exc}") from exc
    expression = expression.strip()
    want_text = expression.endswith("/text()")
    if want_text:
        expression = expression[: -len("/text()")]
    results: List[Any] = []
    for segment in [s for s in expression.split("/") if s]:
        if segment in ("text()", ""):
            continue
        matches: List[Any] = list(results) if results else [root]
        if segment.startswith("@"):
            attr = segment[1:]
            found = [node.get(attr) for node in matches if hasattr(node, "get")]
            results = [v for v in found if v is not None]
            continue
        index = None
        name = segment
        index_match = re.match(r"^(.+?)\[(\d+)\]$", segment)
        if index_match:
            name = index_match.group(1)
            index = int(index_match.group(2))
        pool: List[Any] = []
        for node in matches:
            if not hasattr(node, "iter"):
                continue
            pool.extend(node.iter(name))
        if index is not None:
            # XPath positions are 1-based; ``[1]`` is the first match.
            pool = [pool[index - 1]] if 0 < index <= len(pool) else []
        results = pool
    if want_text:
        return [(node.text or "") for node in results if hasattr(node, "text")]
    return [str(node) for node in results]

# --------------------------------------------------------------------------- #
# version variables (mirror of Get-VersionSubstitution in autoupdate.ps1)
# --------------------------------------------------------------------------- #
def strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def remote_filename(url: str) -> str:
    path = strip_fragment(url).split("?", 1)[0]
    return urllib.parse.unquote(path.rsplit("/", 1)[-1])


def version_variables(version: str, matches: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    parts = version.split("-")
    first, last = parts[0], parts[-1]
    dot = re.sub(r"[._-]", ".", version)
    variables: Dict[str, str] = {
        "$version": version,
        "$dotVersion": dot,
        "$underscoreVersion": re.sub(r"[._-]", "_", version),
        "$dashVersion": re.sub(r"[._-]", "-", version),
        "$cleanVersion": re.sub(r"[._-]", "", version),
    }
    segments = first.split(".")
    for key, idx in (("$majorVersion", 0), ("$minorVersion", 1), ("$patchVersion", 2), ("$buildVersion", 3)):
        variables[key] = segments[idx] if len(segments) > idx else ""
    variables["$preReleaseVersion"] = last
    head = re.match(r"(?P<head>\d+\.\d+(?:\.\d+)?)(?P<tail>.*)", version)
    if head:
        variables["$matchHead"] = head.group("head")
        variables["$matchTail"] = head.group("tail")
    for name, value in (matches or {}).items():
        if name == "0" or not name:
            continue
        variables["$match" + name[:1].upper() + name[1:].lower()] = value
    return variables


def substitute(value: Any, params: Dict[str, str], regex_escape: bool = False) -> Any:
    """Recursive ``str.replace`` with longest key first, like Scoop's substitute()."""
    if isinstance(value, str):
        result = value
        for key in sorted(params, key=len, reverse=True):
            replacement = params[key]
            if replacement is None:
                continue
            result = result.replace(key, re.escape(replacement) if regex_escape else replacement)
        return result
    if isinstance(value, list):
        return [substitute(item, params, regex_escape) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, params, regex_escape) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------- #
# checkver
# --------------------------------------------------------------------------- #
class VersionInfo:
    def __init__(self, version: str, matches: Optional[Dict[str, str]] = None) -> None:
        self.version = version
        self.matches = matches or {}


def github_token_available(http: "Http") -> bool:
    return bool(http.token)


def github_repo_from_url(url: str) -> Optional[str]:
    match = re.match(r"^https://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/?$", url.rstrip("/") + "/")
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    match = re.match(r"^https://api\.github\.com/repos/([\w.-]+)/([\w.-]+)", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def _html_fallback_for_github_api(
    http: "Http", api_url: str, jsonpath: Optional[str], regex: Optional[str]
) -> Optional[Tuple[str, str]]:
    """Translate a GitHub REST checkver into an equivalent HTML scrape.

    Only the two shapes that actually occur in bucket manifests are handled:

    * ``api.github.com/repos/<o>/<r>/releases/latest`` -- the ``/releases/latest``
      page lists a single tag, so the default ``/releases/tag/...`` regex works.
    * ``api.github.com/repos/<o>/<r>/releases`` with a prerelease filter -- the
      releases page lists pre-releases first and marks them with a ``Pre-release``
      label, which ``normalise_github_html`` turns into a ``data-prerelease``
      attribute so ``$[?(@.prerelease == true)].tag_name`` can be satisfied.
    """
    match = re.match(r"^https://api\.github\.com/repos/([\w.-]+)/([\w.-]+)(/releases.*)?$", api_url)
    if not match:
        return None
    owner, repo, tail = match.group(1), match.group(2), match.group(3) or ""
    base = f"https://github.com/{owner}/{repo}"

    if tail.startswith("/releases/latest"):
        return base + "/releases/latest", EXTRACT_TAG_FROM_REDIRECT

    if tail.startswith("/releases") and jsonpath and "prerelease" in jsonpath:
        return base + "/releases", EXTRACT_PRERELEASE_TAG

    return None


# Sentinels meaning "follow the /releases/latest redirect and read the tag from it".
EXTRACT_TAG_FROM_REDIRECT = "\x00extract-tag-from-redirect"
# "read the first Pre-release tag off the /releases listing"
EXTRACT_PRERELEASE_TAG = "\x00extract-prerelease-tag"


def _first_prerelease_tag(http: "Http", page_url: str) -> str:
    """Return the first pre-release tag on a GitHub /releases listing page."""
    page = normalise_github_html(http.text(page_url))
    found = re.findall(
        r'/releases/tag/(?P<tag>[^"?#]+)"[^>]*data-prerelease="true"', page
    )
    if not found:
        raise UpdateError(f"no pre-release found on {page_url}")
    return urllib.parse.unquote(found[0])


# GitHub marks a pre-release with this label class inside the release block, and the
# stable equivalent carries no Label--warning at all. Anchoring on the class beats
# scanning raw prose for the words "Pre-release".
PRERELEASE_LABEL_RE = re.compile(
    r'class="[^"]*\bLabel--warning\b[^"]*"[^>]*>\s*Pre-release\s*<', re.IGNORECASE
)


def normalise_github_html(html: str) -> str:
    """Inject ``data-prerelease`` markers next to release tag links.

    Each ``href="/<o>/<r>/releases/tag/<tag>"`` anchor gains a
    ``data-prerelease="true|false"`` attribute so a regex can select pre-releases
    without re-parsing the whole page. The marker is derived from the label markup
    that follows the anchor (GitHub renders ``Pre-release`` in a ``Label--warning``
    badge), falling back to a plain-text probe only when that markup is absent.
    """
    anchors = list(re.finditer(r'href="[^"]*/releases/tag/(?P<tag>[^"?#]+)"', html))
    if not anchors:
        return html
    pieces: List[str] = []
    cursor = 0
    for index, anchor in enumerate(anchors):
        pieces.append(html[cursor:anchor.end()])
        window_end = anchors[index + 1].start() if index + 1 < len(anchors) else len(html)
        window = html[anchor.end():min(window_end, anchor.end() + 8000)]
        is_pre = bool(PRERELEASE_LABEL_RE.search(window))
        if not is_pre and "Label--" not in window:
            # Older/leaner markup: fall back to the visible badge text.
            is_pre = "Pre-release" in window[:2000]
        pieces.append(f' data-prerelease="{"true" if is_pre else "false"}"')
        cursor = anchor.end()
    pieces.append(html[cursor:])
    return "".join(pieces)


def _apply_replace(template: str, version: str, matches: Dict[str, str], groups: Sequence[Optional[str]]) -> str:
    """Expand ``checkver.replace`` for one version string.

    ``${1}``..``${n}`` are positional regex groups, ``${name}`` are named groups --
    the same convention bin/checkver.ps1 uses.
    """
    substitution = version_variables(version, matches)
    for index, value in enumerate(groups, start=1):
        substitution[f"${{{index}}}"] = value if value is not None else ""
    for key, value in matches.items():
        substitution[f"${{{key}}}"] = value
    return substitute(template, substitution)


def _version_from_tag(tag: str, regex: str, replace: Optional[str]) -> VersionInfo:
    """Turn a release tag into a version via ``checkver.regex`` / ``checkver.replace``."""
    matches: Dict[str, str] = {}
    candidate = tag
    found = None
    for pattern in (regex, TAG_SHAPED_REGEX):
        # The configured regex is usually page-shaped (``/releases/tag/...``) and cannot
        # match a bare tag; fall back to a tag-shaped pattern so capture groups such as
        # ``${1}`` still resolve.
        try:
            found = compile_scoop_regex(pattern).search(tag)
        except re.error:
            found = None
        if found:
            break
    if found:
        candidate = found.group(1) or tag
        matches = {k: v for k, v in found.groupdict().items() if v is not None}
    candidate = re.sub(r"^(?:v|V)(?=\d)", "", candidate)
    groups: Tuple[Optional[str], ...] = found.groups() if found else ()
    if replace:
        candidate = _apply_replace(replace, candidate, matches, groups)
    return VersionInfo(candidate.strip(), matches)


def resolve_version(http: Http, manifest: Dict[str, Any], name: str) -> VersionInfo:
    checkver = manifest.get("checkver")
    homepage = manifest.get("homepage", "")

    if checkver is None:
        raise UpdateError("no checkver defined")

    if isinstance(checkver, str):
        if checkver.lower() != "github":
            raise UpdateError(f"unsupported checkver string: {checkver!r}")
        source_url: Optional[str] = homepage
        explicit_github = True
    elif isinstance(checkver, dict):
        source_url = checkver.get("github") or checkver.get("url")
        explicit_github = bool(checkver.get("github"))
    else:
        raise UpdateError(f"unsupported checkver type: {type(checkver).__name__}")

    if not source_url:
        raise UpdateError("checkver has neither 'github' nor 'url'")

    regex = (checkver.get("regex") or checkver.get("re")) if isinstance(checkver, dict) else None
    jsonpath = (checkver.get("jsonpath") or checkver.get("jp")) if isinstance(checkver, dict) else None
    xpath = checkver.get("xpath") if isinstance(checkver, dict) else None
    replace = checkver.get("replace") if isinstance(checkver, dict) else None
    reverse = bool(checkver.get("reverse")) if isinstance(checkver, dict) else False

    repo = github_repo_from_url(source_url) if explicit_github or "github.com" in source_url else None
    api_mode = source_url.startswith("https://api.github.com")

    # Only switch to the API when a token is available or the manifest itself pins an
    # API endpoint. A manifest that only asks for a *list* (a jsonpath picking
    # prereleases, for instance) must not be redirected to /releases/latest -- that
    # endpoint returns a single release and would silently pick the wrong one.
    wants_list = bool(jsonpath) and (
        "?" in (jsonpath or "") or bool(re.search(r"\[\s*\d*\s*[:*]", jsonpath or ""))
    )
    if repo and not api_mode and (github_token_available(http) or wants_list):
        api_mode = True
        suffix = "releases" if wants_list else "releases/latest"
        source_url = f"https://api.github.com/repos/{repo}/{suffix}"
    elif repo and not api_mode:
        # mirror bin/checkver.ps1: HTML mode always reads the /releases/latest page
        source_url = source_url.rstrip("/") + "/releases/latest"

    if api_mode and not jsonpath and "releases/latest" in source_url and not regex:
        jsonpath = "$.tag_name"
    if not regex:
        regex = TAG_SHAPED_REGEX if api_mode else DEFAULT_HTML_REGEX

    # ---- fetch -----------------------------------------------------------
    # GitHub HTML mode: ``/releases/latest`` 302s to ``/releases/tag/<tag>``. Reading
    # that tag from the final URL and running checkver.regex against *it* is far more
    # reliable than regexing the whole page, which is full of asset names, bundle
    # hashes and JS that happily produce bogus "versions". Scoop itself relies on the
    # /releases/tag/ path being present in the page; the redirect gives it to us
    # directly.
    if explicit_github and not api_mode and not jsonpath and not xpath:
        final_url, _ = http.request(source_url)
        tag_match = re.search(r"/releases/tag/(?P<tag>[^/?#]+)", final_url)
        if tag_match:
            return _version_from_tag(urllib.parse.unquote(tag_match.group("tag")), regex, replace)

    html_fallback = _html_fallback_for_github_api(http, source_url, jsonpath, regex)

    def fetch(url: str, jp: Optional[str], xp: Optional[str]) -> List[str]:
        if xp:
            values = xpath_find(http.text(url), xp)
            if not values:
                raise UpdateError(f"xpath {xp!r} matched nothing in {url}")
            return values
        if jp:
            # jsonpath is not tied to api.github.com: it applies to any JSON document.
            payload = http.json(url)
            candidates = jsonpath_find(payload, jp)
            if not candidates:
                raise UpdateError(f"jsonpath {jp!r} matched nothing in {url}")
            return [str(item) for item in candidates if item is not None]
        if url.startswith("https://api.github.com"):
            return [json.dumps(http.json(url))]
        return [normalise_github_html(http.text(url))]

    try:
        texts = fetch(source_url, jsonpath, xpath)
    except UpdateError:
        if html_fallback is None:
            raise
        page_url, html_regex = html_fallback
        info("  (GitHub API unavailable -- retrying via the HTML releases page)")
        source_url, regex, jsonpath = page_url, html_regex, None
        if html_regex == EXTRACT_TAG_FROM_REDIRECT:
            final_url, _ = http.request(page_url)
            tag_match = re.search(r"/releases/tag/(?P<tag>[^/?#]+)", final_url)
            if not tag_match:
                raise UpdateError(f"no /releases/tag/ redirect from {page_url}")
            return _version_from_tag(urllib.parse.unquote(tag_match.group("tag")), regex, replace)
        if html_regex == EXTRACT_PRERELEASE_TAG:
            return _version_from_tag(
                _first_prerelease_tag(http, page_url), regex, replace
            )
        texts = [normalise_github_html(http.text(page_url))]

    if reverse:
        texts = list(reversed(texts))

    pattern = compile_scoop_regex(regex)
    version: Optional[str] = None
    matches: Dict[str, str] = {}
    groups: Tuple[Optional[str], ...] = ()
    for text in texts:
        found = pattern.search(text)
        if not found:
            continue
        version = found.group(1)
        matches = {k: v for k, v in found.groupdict().items() if v is not None}
        groups = found.groups()
        break
    if not version:
        raise UpdateError(f"regex {regex!r} did not match in {source_url}")

    if replace:
        version = _apply_replace(replace, version, matches, groups)

    return VersionInfo(version.strip(), matches)

# --------------------------------------------------------------------------- #
# hash resolution
# --------------------------------------------------------------------------- #
DEFAULT_TEXT_HASH_REGEX = r"^\s*([a-fA-F0-9]+)\s*$"
TEXT_HASH_TEMPLATES = {
    "$md5": r"([a-fA-F0-9]{32})",
    "$sha1": r"([a-fA-F0-9]{40})",
    "$sha256": r"([a-fA-F0-9]{64})",
    "$sha512": r"([a-fA-F0-9]{128})",
    "$checksum": r"([a-fA-F0-9]{32,128})",
}
HEX_ONLY = re.compile(r"^[a-fA-F0-9]+$")


def format_hash(value: Optional[str]) -> Optional[str]:
    """Normalise a digest the way Scoop's ``format_hash`` does.

    A bare hex string is prefixed according to its length (md5/sha1/sha512);
    sha256 stays unprefixed because that is Scoop's default. ``sha256:`` is
    stripped, which is the standard shape GitHub's Release API returns.
    """
    if not value:
        return None
    cleaned = value.strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[len("sha256:"):]
    if not HEX_ONLY.match(cleaned):
        return None
    return {
        32: f"md5:{cleaned}",
        40: f"sha1:{cleaned}",
        64: cleaned,
        128: f"sha512:{cleaned}",
    }.get(len(cleaned))


def _hash_from_text(body: str, basename: str, regex: Optional[str]) -> Optional[str]:
    pattern = regex or DEFAULT_TEXT_HASH_REGEX
    for key, value in TEXT_HASH_TEMPLATES.items():
        pattern = pattern.replace(key, value)
    pattern = pattern.replace("$basename", re.escape(basename))
    try:
        compiled = compile_scoop_regex(pattern, re.MULTILINE)
    except re.error as exc:
        raise UpdateError(f"invalid hash find regex {pattern!r}: {exc}") from exc
    match = compiled.search(body)
    if match:
        for group in match.groups():
            if group:
                return group
    filename_pattern = re.compile(
        r"([a-fA-F0-9]{32,128})[\x20\t]+.*" + re.escape(basename) + r"(?:\s|$)"
        r"|" + re.escape(basename) + r"[\x20\t]+.*?([a-fA-F0-9]{32,128})"
    )
    match = filename_pattern.search(body)
    if match:
        return next((g for g in match.groups() if g), None)
    metalink = re.search(r"<hash[^>]+>([a-fA-F0-9]{64})", body)
    if metalink:
        return metalink.group(1)
    return None


def _hash_from_json(payload: Any, jsonpath: str) -> Optional[str]:
    """Pick the first value under ``jsonpath`` that looks like a usable digest."""
    candidates = jsonpath_find(payload, jsonpath)
    for item in candidates:
        if isinstance(item, str) and format_hash(item):
            return item
    return None


def _hash_from_github_digest(http: Http, url: str) -> Optional[str]:
    """Read the sha256 of a GitHub release asset.

    Prefers the public ``releases/expanded_assets/<tag>`` HTML page, which lists the
    digest of every asset and is *not* rate limited like the REST API. Falls back to
    the REST API (which also reports ``digest``) when the HTML page is unavailable.
    """
    match = GITHUB_RELEASE_DOWNLOAD.match(strip_fragment(url))
    if not match:
        return None
    owner, repo = match.group("owner"), match.group("repo")
    tag = strip_fragment(url).split("/releases/download/", 1)[1].split("/", 1)[0]
    basename = remote_filename(url)

    # --- HTML: releases/expanded_assets/<tag> ------------------------------
    page_url = f"https://github.com/{owner}/{repo}/releases/expanded_assets/{tag}"
    try:
        page = http.text(page_url)
    except UpdateError:
        page = ""
    if page:
        # each asset is an <a href=".../download/<tag>/<file>">...</a> followed by a
        # <span ...>sha256:<hex></span>; anchor on the href, then read the next digest.
        for found in re.finditer(
            r'href="[^"]*/releases/download/[^"]*/(?P<file>[^/"]+)"(?P<tail>.{0,2000}?)(?P<digest>sha256:[0-9a-f]{64})',
            page,
            re.DOTALL,
        ):
            if urllib.parse.unquote(found.group("file")) == basename:
                return found.group("digest").split(":", 1)[-1]

    # --- REST API fallback -------------------------------------------------
    api = f"https://api.github.com/repos/{owner}/{repo}/releases"
    try:
        releases = http.json(api)
    except UpdateError:
        return None
    target = strip_fragment(url)
    for release in releases if isinstance(releases, list) else []:
        for asset in release.get("assets", []):
            if asset.get("browser_download_url") == target:
                digest = asset.get("digest")
                if digest:
                    return digest.split(":", 1)[-1]
    return None


def resolve_hash(
    http: Http,
    hash_spec: Any,
    url: str,
    substitutions: Dict[str, str],
    allow_download: bool = True,
) -> str:
    """Return a digest for ``url``, formatted the way Scoop expects."""
    basename = remote_filename(url)
    spec = hash_spec if isinstance(hash_spec, dict) else {}

    hash_url = spec.get("url")
    if hash_url:
        hash_url = substitute(hash_url, substitutions)
        try:
            jsonpath = spec.get("jsonpath") or spec.get("jp")
            if jsonpath:
                result = _hash_from_json(http.json(hash_url), substitute(jsonpath, substitutions))
            else:
                find = spec.get("find") or spec.get("regex")
                body = http.text(hash_url)
                result = _hash_from_text(body, basename, substitute(find, substitutions) if find else None)
        except UpdateError as exc:
            raise UpdateError(f"hash file {hash_url} unusable: {exc}") from exc
        formatted = format_hash(result)
        if formatted:
            return formatted
        raise UpdateError(f"could not find a usable hash for {basename} in {hash_url}")

    digest = _hash_from_github_digest(http, url)
    formatted = format_hash(digest)
    if formatted:
        return formatted

    if not allow_download:
        raise UpdateError(f"no published digest for {basename}; download disabled")
    computed = http.sha256(url)
    formatted = format_hash(computed)
    if not formatted:
        raise UpdateError(f"computed digest for {basename} is not a recognised hash: {computed!r}")
    return formatted


# --------------------------------------------------------------------------- #
# manifest rewriting
# --------------------------------------------------------------------------- #
def _arch_specific(block: Dict[str, Any], arch: Optional[str], key: str) -> Any:
    architecture = block.get("architecture")
    if arch is not None and isinstance(architecture, dict):
        spec = architecture.get(arch)
        if isinstance(spec, dict) and key in spec:
            return spec[key]
    return block.get(key)


UPDATABLE_KEYS = (
    "extract_dir",
    "extract_to",
    "bin",
    "shortcuts",
    "installer",
    "pre_install",
    "post_install",
    "persist",
    "env_add_path",
    "env_set",
    "license",
    "notes",
    "innosetup",
    "depends",
)


def apply_autoupdate(
    http: Http,
    manifest: Dict[str, Any],
    info: VersionInfo,
    allow_download: bool = True,
) -> Dict[str, Any]:
    """Return a new manifest with version-dependent fields rewritten."""
    autoupdate = manifest.get("autoupdate")
    if not isinstance(autoupdate, dict):
        raise UpdateError("no autoupdate block, cannot rewrite url/hash")

    substitutions = version_variables(info.version, info.matches)
    updated = json.loads(json.dumps(manifest))
    updated["version"] = info.version

    architecture = updated.get("architecture")
    archs: List[Optional[str]] = (
        [a for a, spec in architecture.items() if isinstance(spec, dict)]
        if isinstance(architecture, dict) and architecture
        else [None]
    )

    for arch in archs:
        url_template = _arch_specific(autoupdate, arch, "url")
        entry = architecture[arch] if arch is not None else updated

        if url_template:
            # A manifest may list several downloads; rewrite every one of them and keep
            # the list shape, otherwise the extra files would be silently dropped.
            templates = url_template if isinstance(url_template, list) else [url_template]
            target_urls = [substitute(t, substitutions) for t in templates]
            if isinstance(url_template, list):
                entry["url"] = target_urls
            else:
                entry["url"] = target_urls[0]

            hash_spec = _arch_specific(autoupdate, arch, "hash")
            if hash_spec is None and isinstance(autoupdate.get("hash"), dict):
                hash_spec = autoupdate["hash"]
            specs = hash_spec if isinstance(hash_spec, list) else [hash_spec] * len(target_urls)
            hashes = [
                resolve_hash(http, specs[i] if i < len(specs) else None, url, substitutions, allow_download)
                for i, url in enumerate(target_urls)
            ]
            current_hash = entry.get("hash")
            if isinstance(current_hash, list) or isinstance(hash_spec, list):
                entry["hash"] = hashes
            else:
                entry["hash"] = hashes[0]

        for key in UPDATABLE_KEYS:
            if _arch_specific(updated, arch, key) is None:
                continue
            template = _arch_specific(autoupdate, arch, key)
            if template is None:
                template = autoupdate.get(key)
            if template is None:
                continue
            # Scoop looks the property up in the architecture block first, then at the
            # manifest root; write it back wherever it already lives.
            in_arch = (
                arch is not None
                and isinstance(architecture, dict)
                and isinstance(architecture.get(arch), dict)
                and key in architecture[arch]
            )
            target = architecture[arch] if in_arch else updated
            target[key] = substitute(template, substitutions)

    # global properties carrying a version variable, e.g. notes
    for key, template in autoupdate.items():
        if key in ("architecture", "hash", "url"):
            continue
        if not isinstance(template, str):
            continue
        if key not in updated:
            continue
        updated[key] = substitute(template, substitutions)

    return updated


def reserialize(path: Path, manifest: Dict[str, Any]) -> bool:
    """Write the manifest with Scoop's JSON style. Returns True if content changed."""
    text = json.dumps(manifest, indent=4, ensure_ascii=False) + "\n"
    raw = path.read_bytes() if path.exists() else b""
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    original = raw.decode("utf-8-sig")
    if original.replace("\r\n", "\n") == text:
        return False
    newline = "\r\n" if "\r\n" in original else "\n"
    # Preserve the BOM if the file had one, so a rewrite does not silently change it.
    encoding = "utf-8-sig" if has_bom else "utf-8"
    path.write_text(text.replace("\n", newline), encoding=encoding, newline="")
    return True

# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def load_manifest(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise UpdateError("manifest root is not an object")
    return manifest


def update_one(
    http: Http,
    path: Path,
    force: bool = False,
    dry_run: bool = False,
    allow_download: bool = True,
) -> Tuple[str, Optional[str]]:
    """Maintain a single manifest. Returns (status, message)."""
    name = path.stem
    manifest = load_manifest(path)
    current = str(manifest.get("version", ""))

    info = resolve_version(http, manifest, name)
    if info.version == current and not force:
        return "current", "already at " + info.version

    updated = apply_autoupdate(http, manifest, info, allow_download=allow_download)

    if info.version == current:
        new_ = json.dumps(updated, indent=4, sort_keys=False)
        old = json.dumps(manifest, indent=4, sort_keys=False)
        if new_ == old:
            return "current", "no change at " + info.version

    if dry_run:
        return "outdated", f"{current} -> {info.version} (dry run)"

    changed = reserialize(path, updated)
    if not changed:
        return "current", "no change at " + info.version
    return "updated", f"{current} -> {info.version}"


def discover(bucket_dir: Path, selection: Sequence[str]) -> List[Path]:
    if not bucket_dir.is_dir():
        raise SystemExit(f"bucket directory not found: {bucket_dir}")
    paths = sorted(p for p in bucket_dir.glob("*.json") if p.is_file())
    paths = [p for p in paths if not p.name.endswith(".template")]
    if selection:
        wanted = {s.lower()[:-5] if s.lower().endswith(".json") else s.lower() for s in selection}
        paths = [p for p in paths if p.stem.lower() in wanted]
        unknown = sorted(wanted - {p.stem.lower() for p in paths})
        if unknown:
            raise UpdateError(
                "no manifest named " + ", ".join(repr(n) for n in unknown) + f" in {bucket_dir}"
            )
    return paths


def run_git(args: Sequence[str], cwd: Path) -> Tuple[int, str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def commit_changes(repo: Path, updated: Sequence[str], push: bool) -> bool:
    """Commit (and optionally push) the updated manifests. Returns False on failure."""
    if not updated:
        info("nothing to commit")
        return True
    code, out = run_git(["add", "--", *updated], repo)
    if code != 0:
        error(f"git add failed: {out}")
        return False
    message = "Update manifests for " + ", ".join(sorted(updated))
    code, out = run_git(["commit", "-m", message], repo)
    if code != 0:
        error(f"git commit failed: {out}")
        return False
    ok(f"committed: {message}")
    if push:
        code, out = run_git(["push"], repo)
        if code != 0:
            error(f"git push failed: {out}")
            return False
        ok("pushed")
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    _init_stdout()
    parser = argparse.ArgumentParser(
        description="Keep every Scoop manifest in a bucket up to date.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("apps", nargs="*", help="manifest names to update (default: all)")
    parser.add_argument("-b", "--bucket", default="bucket", help="bucket directory (default: bucket)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="report only, never write")
    parser.add_argument("-f", "--force", action="store_true", help="recompute even when already up to date")
    parser.add_argument("--no-download", action="store_true", help="never download assets to hash locally")
    parser.add_argument("--commit", action="store_true", help="git commit the updated manifests")
    parser.add_argument("--push", action="store_true", help="also git push (implies --commit)")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token (env GITHUB_TOKEN)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="per-request timeout in seconds")
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    bucket_dir = (repo / args.bucket) if not Path(args.bucket).is_absolute() else Path(args.bucket)
    try:
        paths = discover(bucket_dir, args.apps)
    except UpdateError as exc:
        error(str(exc))
        return 1
    if not paths:
        info("no manifests found")
        return 0

    http = Http(token=args.token, timeout=args.timeout)
    counts = {"updated": 0, "current": 0, "failed": 0}
    updated_files: List[str] = []

    def git_path(p: Path) -> str:
        try:
            return p.relative_to(repo).as_posix()
        except ValueError:
            # --bucket pointed outside the repository; absolute paths still work.
            return str(p)

    for path in paths:
        name = path.stem
        try:
            status, message = update_one(
                http, path, force=args.force, dry_run=args.dry_run, allow_download=not args.no_download
            )
        except UpdateError as exc:
            counts["failed"] += 1
            error(f"{name}: {exc}")
            continue
        except Exception as exc:  # unexpected -- keep going with the other manifests
            counts["failed"] += 1
            error(f"{name}: {type(exc).__name__}: {exc}")
            continue

        if status == "updated":
            counts["updated"] += 1
            ok(f"{name}: {message}")
            updated_files.append(git_path(path))
        elif status == "outdated":
            counts["updated"] += 1
            warn(f"{name}: {message}")
        else:
            counts["current"] += 1
            info(_c(f"{name}: {message}", "90"))

    summary = (
        f"\n{counts['updated']} updated, {counts['current']} up to date, {counts['failed']} failed"
    )
    print(summary)
    if counts["failed"]:
        print(_c("some manifests need manual attention", "31"), file=sys.stderr)

    commit_failed = False
    if args.commit or args.push:
        if args.dry_run:
            info("dry run -- skipping --commit/--push")
        else:
            commit_failed = not commit_changes(repo, updated_files, push=args.push)

    return 1 if (counts["failed"] or commit_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
