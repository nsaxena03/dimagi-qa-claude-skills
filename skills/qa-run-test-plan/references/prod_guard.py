"""Write guard for running checks against production.

On prod, every request the browser sends passes through `ProdWriteGuard.handle`.
Reads go through untouched. A write (anything but GET/HEAD/OPTIONS) to a prod
host is let through only when it targets an org or project on the allowlist;
otherwise it is aborted inside the browser, so it never reaches the server, and
the slug is recorded so the case can be marked Blocking.

The decision itself is `classify`, a pure function, so the rules are tested
without a browser.

    guard = ProdWriteGuard(load_allowlist("prod_allowlist.yaml"))
    session = ConnectSession(env="prod", guard=guard)
"""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SLUG_RE = re.compile(r"^/a/([^/]+)/")


@dataclass(frozen=True)
class Allowlist:
    hosts: dict  # host -> frozenset of org / project slugs writes may target
    auth_path_prefixes: tuple = ()
    # Hosts under these domains are prod-family: an unlisted one is blocked,
    # not waved through as third-party (connectid, india/eu HQ, ...).
    guarded_suffixes: tuple = ()


@dataclass(frozen=True)
class Decision:
    allow: bool
    violation: str | None = None


def load_allowlist(path):
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Allowlist(
        hosts={host: frozenset(slugs) for host, slugs in data["hosts"].items()},
        auth_path_prefixes=tuple(data.get("auth_path_prefixes") or ()),
        guarded_suffixes=tuple(data.get("guarded_suffixes") or ()),
    )


def _slug(path):
    match = SLUG_RE.match(path)
    return match.group(1) if match else None


def _guarded(host, suffixes):
    return any(host == s or host.endswith("." + s) for s in suffixes)


def classify(method, url, frame_url, allowlist):
    if method.upper() in READ_METHODS:
        return Decision(True)

    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")  # "connect.dimagi.com." is the same host
    if host not in allowlist.hosts:
        if _guarded(host, allowlist.guarded_suffixes):
            return Decision(False, f"<unlisted host> {host}")
        return Decision(True)  # third-party traffic is not prod data
    path = parts.path or "/"
    if any(path.startswith(prefix) for prefix in allowlist.auth_path_prefixes):
        return Decision(True)

    slug = _slug(path)
    if slug is None and frame_url:
        frame = urlsplit(frame_url)
        if (frame.hostname or "").rstrip(".") == host:
            slug = _slug(frame.path or "/")
    if slug is None:
        return Decision(False, f"<no org> {path}")
    if slug in allowlist.hosts[host]:
        return Decision(True)
    return Decision(False, slug)


class ProdWriteGuard:
    """Routes a browser context's requests through `classify`."""

    def __init__(self, allowlist):
        self.allowlist = allowlist
        self._violations = []

    def install(self, context):
        context.route("**/*", self.handle)

    def handle(self, route, request):
        try:
            frame_url = request.frame.url
        except Exception:
            frame_url = None  # service-worker requests have no frame
        decision = classify(request.method, request.url, frame_url, self.allowlist)
        if decision.allow:
            route.continue_()
            return
        self._violations.append(decision.violation)
        route.abort()

    def take_violations(self):
        """Return the writes blocked since the last call, and forget them."""
        taken, self._violations = self._violations, []
        return taken
