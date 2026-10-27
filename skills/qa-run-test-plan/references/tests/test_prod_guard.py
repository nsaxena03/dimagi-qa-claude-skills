from types import SimpleNamespace

import pytest

from prod_guard import Allowlist, Decision, ProdWriteGuard, classify

HQ = frozenset({"connectqa-automation-prod", "qateam", "casesearch"})
AL = Allowlist(
    hosts={
        "connect.dimagi.com": frozenset(
            {"pm_automation_01", "network-manager", "program-manager-prod", "network-manager-prod"}
        ),
        "www.commcarehq.org": HQ,
        "commcarehq.org": HQ,
    },
    auth_path_prefixes=("/accounts/", "/oauth/", "/o/"),
)
CONNECT = "https://connect.dimagi.com"


def test_get_always_allowed():
    assert classify("GET", f"{CONNECT}/a/real-org/x/", None, AL) == Decision(True, None)


def test_post_in_qa_org_allowed():
    url = f"{CONNECT}/a/network-manager-prod/opportunity/"
    assert classify("POST", url, None, AL) == Decision(True, None)


def test_post_outside_qa_org_blocked():
    url = f"{CONNECT}/a/real-org/opportunity/"
    assert classify("POST", url, None, AL) == Decision(False, "real-org")


@pytest.mark.parametrize("method", ["post", "PUT", "PATCH", "DELETE"])
def test_write_verbs_any_case(method):
    assert classify(method, f"{CONNECT}/a/real-org/x/", None, AL).allow is False


@pytest.mark.parametrize("slug", ["network-manager-prod2", "NETWORK-MANAGER", "pm_automation_01x"])
def test_lookalike_slugs_blocked(slug):
    decision = classify("POST", f"{CONNECT}/a/{slug}/x/", None, AL)
    assert decision.allow is False
    assert decision.violation == slug


def test_no_org_path_blocked():
    assert classify("POST", f"{CONNECT}/api/thing/", None, AL) == Decision(False, "<no org> /api/thing/")


def test_frame_fallback():
    frame = f"{CONNECT}/a/pm_automation_01/opportunity/"
    assert classify("POST", f"{CONNECT}/htmx/x/", frame, AL).allow is True


def test_request_slug_beats_frame():
    frame = f"{CONNECT}/a/pm_automation_01/"
    assert classify("POST", f"{CONNECT}/a/real-org/x/", frame, AL) == Decision(False, "real-org")


def test_hq_without_www():
    assert classify("POST", "https://commcarehq.org/a/qateam/x/", None, AL).allow is True
    assert classify("POST", "https://www.commcarehq.org/a/other/x/", None, AL) == Decision(False, "other")


def test_auth_paths_allowed():
    assert classify("POST", f"{CONNECT}/accounts/login/", None, AL).allow is True


def test_third_party_allowed():
    assert classify("POST", "https://sentry.io/api/1/", None, AL).allow is True


class FakeRoute:
    def __init__(self):
        self.calls = []

    def continue_(self):
        self.calls.append("continue")

    def abort(self):
        self.calls.append("abort")


def _request(method, url, frame_url=None):
    return SimpleNamespace(method=method, url=url, frame=SimpleNamespace(url=frame_url))


def test_handle_and_take_violations():
    guard = ProdWriteGuard(AL)

    blocked = FakeRoute()
    guard.handle(blocked, _request("POST", f"{CONNECT}/a/real-org/x/"))
    assert blocked.calls == ["abort"]
    assert guard.take_violations() == ["real-org"]
    assert guard.take_violations() == []

    allowed = FakeRoute()
    guard.handle(allowed, _request("GET", f"{CONNECT}/a/real-org/x/"))
    assert allowed.calls == ["continue"]
    assert guard.take_violations() == []


GUARDED = Allowlist(
    hosts=AL.hosts,
    auth_path_prefixes=("/accounts/login/", "/oauth/authorize/"),
    guarded_suffixes=("dimagi.com", "commcarehq.org"),
)


@pytest.mark.parametrize("host", ["connectid.dimagi.com", "india.commcarehq.org", "dimagi.com"])
def test_unlisted_prod_family_host_blocked(host):
    assert classify("POST", f"https://{host}/a/qateam/x/", None, GUARDED) == Decision(
        False, f"<unlisted host> {host}")


def test_trailing_dot_host_treated_as_listed():
    assert classify("POST", "https://connect.dimagi.com./a/real-org/x/", None, GUARDED) == Decision(
        False, "real-org")
    assert classify("POST", "https://connect.dimagi.com./a/pm_automation_01/x/", None, GUARDED).allow


def test_auth_prefixes_are_exact_paths():
    assert classify("POST", f"{CONNECT}/accounts/login/", None, GUARDED).allow
    assert classify("POST", f"{CONNECT}/accounts/email/", None, GUARDED) == Decision(
        False, "<no org> /accounts/email/")


def test_lookalike_suffix_not_guarded():
    assert classify("POST", "https://notdimagi.com/x/", None, GUARDED).allow
