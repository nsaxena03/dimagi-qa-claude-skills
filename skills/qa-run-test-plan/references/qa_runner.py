"""Harness for executing a QA test plan against a deployed environment.

Supplies the parts that are the same for every ticket, so a run only has to
write the per-case assertions:

  * one login, cached to disk, so repeated runs skip the CCHQ OAuth round trip
  * per-case isolation, so one broken case does not end the run
  * a screenshot of every failure
  * the shared export-progress download, which every export in Connect uses
  * writeback into a `qa-ticket-prep` workbook, always to a dated copy

Typical use:

    from qa_runner import ConnectSession, CaseRunner

    session = ConnectSession(env="stage")
    with session.start(org="Network Manager") as page:
        runner = CaseRunner(page, out_dir)
        runner.run("TC-MTH-001", lambda: check_chip_strip(page))
        runner.skip_manual("TC-REG-007", "needs a real screen reader")
        runner.skip_data("TC-MTH-003", "staging has one month only",
                         unblocked_by="one multi-month service-delivery invoice")
    runner.print_summary()
    runner.write_workbook("CCCT-2771_Test_Plan.xlsx")

On prod a write guard is mandatory (see prod_guard.py); a write outside the
allowlisted QA orgs is aborted in the browser and the case marked Blocking:

    guard = ProdWriteGuard(load_allowlist("prod_allowlist.yaml"))
    session = ConnectSession(env="prod", guard=guard)
    runner = CaseRunner(page, out_dir, guard=guard)

Statuses follow the workbook's dropdown: Pass, Fail, N/A, Blocking.

`skip_manual` and `skip_data` both land on Blocking but say different things,
and keeping them apart is the point: the first needs a tester, the second needs
a row in the database. The summary counts them separately.

The QA repo is located via the QA_CCC_REPO environment variable, falling back
to C:/Users/DIMAGI/PythonScripts/dimagi-qa-ccc.
"""

import os
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

QA_REPO = Path(os.getenv("QA_CCC_REPO", r"C:\Users\DIMAGI\PythonScripts\dimagi-qa-ccc"))
for _p in (QA_REPO, QA_REPO / "playwright_web"):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from playwright.sync_api import sync_playwright  # noqa: E402

from utils.helpers import ConfigLoader, SettingsLoader  # noqa: E402

STATUS_PASS = "Pass"
STATUS_FAIL = "Fail"
STATUS_NA = "N/A"
STATUS_BLOCKING = "Blocking"

MANUAL_PREFIX = "RUN BY HAND"
DATA_PREFIX = "NEEDS TEST DATA"
GUARD_PREFIX = "PROD GUARD"

# Service workers are blocked so every request goes through context.route,
# which is where the prod write guard sits.
CONTEXT_OPTIONS = {"accept_downloads": True, "service_workers": "block"}


def _header_columns(ws):
    """{lowercased header: 1-based column index} for a test case tab."""
    return {
        str(ws.cell(row=1, column=c).value).strip().lower(): c
        for c in range(1, ws.max_column + 1)
        if ws.cell(row=1, column=c).value
    }


# A case id and nothing else. Prose that merely starts with "TC-" (the
# Prerequisites tab has some) must not be mistaken for a case.
CASE_ID_RE = re.compile(r"^TC-[A-Z0-9]+-\d+$")


def _case_id(value):
    if isinstance(value, str) and CASE_ID_RE.match(value.strip()):
        return value.strip()
    return None


class TestPlan:
    """A qa-ticket-prep workbook, read for its case ids and Automatable column.

    Columns are found by header name, not by position, so a plan that gains a
    column still loads.
    """

    def __init__(self, path):
        from openpyxl import load_workbook

        self.path = Path(path)
        self.cases = {}
        wb = load_workbook(self.path, data_only=True)
        auto_col_seen = False
        for ws in wb.worksheets:
            cols = _header_columns(ws)
            if "status" not in cols:
                continue  # not a test case tab (Prerequisites and the like)
            auto_col = cols.get("automatable")
            auto_col_seen = auto_col_seen or auto_col is not None
            for row in range(2, ws.max_row + 1):
                case = _case_id(ws.cell(row=row, column=1).value)
                if case is None:
                    continue
                raw = ws.cell(row=row, column=auto_col).value if auto_col else None
                automatable = None
                if isinstance(raw, str):
                    automatable = raw.strip().lower() == "yes"
                self.cases[case] = {
                    "tab": ws.title,
                    "row": row,
                    "automatable": automatable,
                    "description": ws.cell(row=row, column=2).value,
                }
        if not auto_col_seen:
            print("  NOTE: this plan has no Automatable column - every case is treated as "
                  "automatable. Regenerate it with the current qa-ticket-prep skill.")

    @property
    def automatable(self):
        return [c for c, m in self.cases.items() if m["automatable"] is not False]

    @property
    def manual(self):
        return [c for c, m in self.cases.items() if m["automatable"] is False]

    def summary(self):
        return (f"{len(self.cases)} cases: {len(self.automatable)} automatable, "
                f"{len(self.manual)} for a tester")


class ConnectSession:
    """A logged-in Connect browser context, with the session cached to disk.

    The first run does the full CCHQ login and Connect OAuth through the repo's
    own flow and saves the storage state; later runs reuse it and start on the
    page you ask for. Pass `fresh=True` (or delete the state file) when the
    session has expired.
    """

    def __init__(self, env="stage", state_dir=None, headless=True, viewport=(1600, 1100),
                 guard=None):
        if env == "prod" and guard is None:
            raise ValueError("env='prod' needs a ProdWriteGuard")
        self.env = env
        self.guard = guard
        self.config = ConfigLoader(env)
        self.settings = SettingsLoader()
        self.base_url = self.config.get("connect_url")
        self.headless = headless
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        state_dir = Path(state_dir) if state_dir else Path.home() / ".qa_runner"
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / f"storage_state_{env}.json"

    @contextmanager
    def start(self, org, fresh=False):
        """Yield a page already signed in to Connect under `org`."""
        from flows.tasking_static import login_to_connect

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            reuse = self.state_path.exists() and not fresh
            ctx = browser.new_context(
                storage_state=str(self.state_path) if reuse else None,
                viewport=self.viewport,
                **CONTEXT_OPTIONS,
            )
            if self.guard is not None:
                self.guard.install(ctx)  # before login, so nothing slips past it
            page = ctx.new_page()
            if not reuse:
                page = login_to_connect(page, self.config, self.settings, organization=org)
                page.context.storage_state(path=str(self.state_path))
            try:
                yield page
            finally:
                browser.close()

    def opportunity_url(self, org_slug, opp_id, suffix=""):
        base = f"{self.base_url}/a/{org_slug}/opportunity/{opp_id}"
        return f"{base}/{suffix}".rstrip("/") + "/"


class CaseRunner:
    """Runs test cases one at a time, recording a status and evidence for each."""

    def __init__(self, page, out_dir, run_note=None, guard=None):
        self.page = page
        self.guard = guard
        # Writes the guard blocked outside any case (login, setup between
        # cases): (when, slug). Reported, never silently dropped.
        self.stray_violations = []
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.run_note = run_note or f"Verified by automation on {date.today().isoformat()}."
        self.results = {}

    # -- recording ---------------------------------------------------------
    def _record(self, case, status, detail, evidence=None, kind=None):
        self.results[case] = {
            "status": status, "detail": detail, "evidence": evidence, "kind": kind,
        }
        print(f"[{status:<8}] {case}: {detail}")

    def run(self, case, fn):
        """Execute one case. Its return value becomes the evidence line.

        An AssertionError means a check you wrote did not hold - a candidate
        product failure. Any other exception is far more often a broken check
        than a broken product, so it is labelled to say so. Triage either
        before reporting it.

        With a guard, a write blocked during the case overrides Pass or Fail:
        the case did not run as written, so it is Blocking.
        """
        if self.guard is not None:
            # Not this case's doing, but a blocked setup write still matters.
            self.stray_violations += [(f"before {case}", v) for v in self.guard.take_violations()]
        try:
            self._record(case, STATUS_PASS, fn(), kind="ran")
        except AssertionError as exc:
            self._record(case, STATUS_FAIL, str(exc), self._shot(case), kind="assert")
        except Exception as exc:
            self._record(
                case,
                STATUS_FAIL,
                f"CHECK ERRORED ({type(exc).__name__}: {exc}) - triage before reporting this as a "
                "product failure; an error here is usually the check, not the app",
                self._shot(case),
                kind="error",
            )
        blocked = []
        if self.guard is not None:
            self._settle()  # let this case's late requests reach the guard now, not in the next case
            blocked = self.guard.take_violations()
        if blocked:
            self._record(
                case,
                STATUS_BLOCKING,
                f"{GUARD_PREFIX}: write outside QA org {', '.join(sorted(set(blocked)))}",
                self._shot(case),
                kind="guard",
            )

    def skip_manual(self, case, reason):
        """A case automation cannot judge. It goes to a human tester."""
        self._record(case, STATUS_BLOCKING, f"{MANUAL_PREFIX} - {reason}", kind="manual")

    def skip_data(self, case, reason, unblocked_by=None):
        """A case the environment's data cannot support."""
        tail = f" Unblocked by: {unblocked_by}" if unblocked_by else ""
        self._record(case, STATUS_BLOCKING, f"{DATA_PREFIX} - {reason}{tail}", kind="data")

    def not_applicable(self, case, reason):
        self._record(case, STATUS_NA, reason, kind="na")

    def _settle(self, timeout_ms=5_000):
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass  # a page that never goes idle still gets its violations taken

    def _shot(self, case):
        path = self.out_dir / f"fail_{case}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None

    # -- reporting ---------------------------------------------------------
    def summary(self):
        counts = {}
        for r in self.results.values():
            key = {
                "manual": "Blocking (run by hand)",
                "data": "Blocking (needs data)",
                "guard": "Blocking (prod guard)",
            }.get(r["kind"], r["status"])
            counts[key] = counts.get(key, 0) + 1
        return counts

    def failures(self):
        return {c: r for c, r in self.results.items() if r["status"] == STATUS_FAIL}

    def needs_data(self):
        return {c: r for c, r in self.results.items() if r["kind"] == "data"}

    def needs_hand_run(self):
        return {c: r for c, r in self.results.items() if r["kind"] == "manual"}

    def guard_blocked(self):
        return {c: r for c, r in self.results.items() if r["kind"] == "guard"}

    def print_summary(self):
        print("\n" + "=" * 72)
        for key, n in sorted(self.summary().items()):
            print(f"  {key}: {n}")
        for case, r in self.failures().items():
            print(f"\n  FAIL {case}\n    {r['detail'][:600]}")
            if r["evidence"]:
                print(f"    screenshot: {r['evidence']}")
        if self.needs_hand_run():
            print(f"\n  For a tester to run by hand: {sorted(self.needs_hand_run())}")
        if self.needs_data():
            print(f"\n  Waiting on test data: {sorted(self.needs_data())}")
        if self.stray_violations:
            print(f"\n  Writes blocked outside any case: {self.stray_violations}")
        if self.guard_blocked():
            print(f"\n  Stopped by the prod write guard: {sorted(self.guard_blocked())}")

    def adopt_plan(self, plan, mark_manual=True):
        """Take the plan's own Automatable column as the starting split.

        Every case the plan marks No is recorded as one for a tester, so a run
        only has to write checks for the Yes rows. Returns the list of case ids
        still to be covered.
        """
        for case, meta in plan.cases.items():
            if meta["automatable"] is False and mark_manual:
                # The plan's own Comments cell already says why; repeating a
                # generic reason here would just be noise in the workbook.
                self.skip_manual(case, "Automatable = No in the plan")
        return [c for c, m in plan.cases.items() if m["automatable"] is not False]

    def write_workbook(self, plan, out_path=None):
        """Write statuses into a dated COPY of the plan, never the original.

        A tester may have been filling the original in by hand; overwriting it
        would silently discard their work.

        `plan` may be a TestPlan or a path.
        """
        from openpyxl import load_workbook

        plan_path = Path(plan.path if isinstance(plan, TestPlan) else plan)
        if out_path is None:
            out_path = plan_path.with_name(
                f"{plan_path.stem}_run_{date.today().isoformat()}{plan_path.suffix}"
            )
        out_path = Path(out_path)
        shutil.copyfile(plan_path, out_path)

        wb = load_workbook(out_path)
        written = 0
        seen = set()
        for ws in wb.worksheets:
            cols = _header_columns(ws)
            status_col = cols.get("status")
            comment_col = cols.get("comments / notes") or cols.get("comments")
            if not status_col:
                continue
            for row in range(2, ws.max_row + 1):
                case = _case_id(ws.cell(row=row, column=1).value)
                if case is None:
                    continue
                seen.add(case)
                result = self.results.get(case)
                if not result:
                    continue
                ws.cell(row=row, column=status_col, value=result["status"])
                if comment_col:
                    prefix = self.run_note if result["status"] == STATUS_PASS else ""
                    note = f"{prefix} {result['detail']}".strip()
                    if result["evidence"]:
                        note += f"\nScreenshot: {result['evidence']}"
                    existing = ws.cell(row=row, column=comment_col).value or ""
                    ws.cell(row=row, column=comment_col, value=f"{note}\n\n{existing}".strip())
                written += 1
        wb.save(out_path)

        unknown = sorted(set(self.results) - seen)
        untouched = sorted(seen - set(self.results))
        print(f"\nWrote {written} result(s) into {out_path}")
        if unknown:
            print(f"  WARNING: results with no matching row in the plan: {unknown}")
        if untouched:
            print(f"  {len(untouched)} plan case(s) not addressed by this run: {untouched}")
        return out_path


# ---------------------------------------------------------------- page helpers

def download_via_progress_toast(page, timeout_ms=180_000, save_dir=None):
    """Wait for Connect's shared export-progress component to offer a download, then take it.

    Every export in the app redirects back with a task id and polls this same
    component, so this covers invoice, visit, payment and status exports alike.
    """
    assert "task_id=" in page.url, f"redirect carried no task id: {page.url}"
    link = page.locator("a[href*='download_export']").first
    left = timeout_ms
    while left > 0 and not (link.count() and link.is_visible()):
        page.wait_for_timeout(2000)
        left -= 2000
    assert link.count() and link.is_visible(), (
        f"no download link after {timeout_ms // 1000}s - is a background worker running?"
    )
    with page.expect_download(timeout=120_000) as dl:
        return _save(dl, save_dir, click=link)


def download_via_navigation(page, url, save_dir=None):
    """Fetch a URL that responds with a file.

    `page.goto` raises "Download is starting" on such a URL, so the navigation
    has to be driven from inside the page instead.
    """
    with page.expect_download(timeout=90_000) as dl:
        page.evaluate("u => { window.location.href = u }", url)
    saved = _save(dl, save_dir)
    page.wait_for_timeout(500)
    return saved


def _save(dl_info, save_dir, click=None):
    if click is not None:
        click.click()
    download = dl_info.value
    save_dir = Path(save_dir or ".")
    save_dir.mkdir(parents=True, exist_ok=True)
    download.save_as(str(_unclobbered(save_dir / download.suggested_filename)))
    return _LAST_SAVED[0]


_LAST_SAVED = [None]


def _unclobbered(dest):
    """A path that does not already exist, suffixed _2, _3 ... if it does.

    Connect names every export download `<org>_<opportunity>_export.<ext>`
    whatever its scope, so a run that takes several exports would otherwise
    overwrite its own evidence and compare the wrong file.
    """
    candidate, n = dest, 2
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
    _LAST_SAVED[0] = candidate
    return candidate


def scrape_table(page, columns, require_checkbox=True):
    """Rows of the visible django-tables2 table as dicts.

    `columns` maps a name to a zero-based cell index, e.g.
    {"number": 5, "status": 6}. Each row also carries `pk` from its selection
    checkbox, and `data` with every `data-*` attribute on that checkbox — pages
    hang per-row values there (the invoice list puts the USD amount in
    `data-amount-usd`, which is what its selection bar totals).

    Pass `require_checkbox=False` for a table with no selection column; `pk` is
    then None and nothing is filtered out.
    """
    return page.evaluate(
        """({columns, requireCheckbox}) =>
            [...document.querySelectorAll('table tbody tr')].map(tr => {
                const box = tr.querySelector("input[name='row_select']");
                const tds = [...tr.querySelectorAll('td')].map(t => t.innerText.trim());
                const row = {pk: box ? box.value : null,
                             data: box ? {...box.dataset} : {}};
                for (const [name, idx] of Object.entries(columns)) row[name] = tds[idx];
                return row;
            }).filter(r => !requireCheckbox || r.pk !== null)""",
        {"columns": columns, "requireCheckbox": require_checkbox},
    )


def go_to_page(page, n):
    """Click through to a table page. django-tables2 pagers here are JS, not links."""
    page.evaluate("n => goToPage('page', n)", n)
    page.wait_for_load_state("load")
    page.wait_for_timeout(1500)
