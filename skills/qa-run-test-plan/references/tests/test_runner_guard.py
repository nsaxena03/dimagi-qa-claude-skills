import pytest

from qa_runner import CaseRunner, ConnectSession


class FakePage:
    def screenshot(self, path, full_page):
        return None


class FakeGuard:
    """take_violations() returns the scripted lists in order, then []."""

    def __init__(self, script):
        self.script = list(script)

    def take_violations(self):
        return self.script.pop(0) if self.script else []


def test_prod_without_guard_refuses():
    with pytest.raises(ValueError):
        ConnectSession(env="prod")


def test_stage_without_guard_ok():
    assert ConnectSession(env="stage").env == "stage"


def test_violation_overrides_pass(tmp_path):
    runner = CaseRunner(FakePage(), tmp_path, guard=FakeGuard([[], ["real-org"]]))
    runner.run("TC-A-001", lambda: "ok")
    result = runner.results["TC-A-001"]
    assert result["status"] == "Blocking"
    assert result["detail"].startswith("PROD GUARD: write outside QA org real-org")
    assert result["kind"] == "guard"


def test_stale_violation_discarded(tmp_path):
    runner = CaseRunner(FakePage(), tmp_path, guard=FakeGuard([["old-org"], []]))
    runner.run("TC-A-001", lambda: "ok")
    assert runner.results["TC-A-001"]["status"] == "Pass"


def test_violation_overrides_fail(tmp_path):
    def fails():
        raise AssertionError("nope")

    runner = CaseRunner(FakePage(), tmp_path, guard=FakeGuard([[], ["x"]]))
    runner.run("TC-A-001", fails)
    assert runner.results["TC-A-001"]["status"] == "Blocking"
    assert runner.results["TC-A-001"]["kind"] == "guard"


def test_no_guard_unchanged(tmp_path):
    runner = CaseRunner(FakePage(), tmp_path)
    runner.run("TC-A-001", lambda: "ok")
    assert runner.results["TC-A-001"]["status"] == "Pass"


def test_summary_counts_guard(tmp_path):
    runner = CaseRunner(FakePage(), tmp_path, guard=FakeGuard([[], ["real-org"]]))
    runner.run("TC-A-001", lambda: "ok")
    assert runner.summary()["Blocking (prod guard)"] == 1


class SettlingPage(FakePage):
    def __init__(self):
        self.waits = []

    def wait_for_load_state(self, state, timeout):
        self.waits.append(state)


def test_waits_for_network_before_attributing(tmp_path):
    page = SettlingPage()
    runner = CaseRunner(page, tmp_path, guard=FakeGuard([[], []]))
    runner.run("TC-A-001", lambda: "ok")
    assert page.waits == ["networkidle"]


def test_between_case_violations_kept(tmp_path):
    runner = CaseRunner(FakePage(), tmp_path, guard=FakeGuard([["setup-org"], []]))
    runner.run("TC-A-001", lambda: "ok")
    assert runner.results["TC-A-001"]["status"] == "Pass"
    assert runner.stray_violations == [("before TC-A-001", "setup-org")]


def test_session_blocks_service_workers():
    from qa_runner import CONTEXT_OPTIONS
    assert CONTEXT_OPTIONS["service_workers"] == "block"
