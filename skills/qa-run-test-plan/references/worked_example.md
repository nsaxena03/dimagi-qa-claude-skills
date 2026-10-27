# Worked example — CCCT-2771, invoice bulk export

The run these helpers were extracted from. 41 cases, 24 executed, 24 passed,
15 waiting on data, 2 for a tester. Three iterations to get there; everything
below is what cost time, so the next run does not pay it again.

## Shape of the run

```python
import sys
sys.path.insert(0, r"C:\Users\DIMAGI\.claude\skills\qa-run-test-plan\references")
from qa_runner import ConnectSession, CaseRunner, TestPlan, scrape_table, go_to_page

PLAN = "CCCT-2771_Invoice_Bulk_Export_Test_Plan.xlsx"
OPP = "c4a52633-20fe-457d-8f1f-1b96650e07a8"
NM_SLUG = "network-manager"

plan = TestPlan(PLAN)
print(plan.summary())          # 41 cases: 39 automatable, 2 for a tester

session = ConnectSession(env="stage")
with session.start(org="Network Manager") as page:
    runner = CaseRunner(page, out_dir="run_output")
    todo = runner.adopt_plan(plan)      # every "No" row -> RUN BY HAND, automatically

    # Step 3: inventory first, across ALL pages
    inventory = []
    for pnum in (1, 2):
        page.goto(session.opportunity_url(NM_SLUG, OPP, "invoice") + "?month=2026-09&page=%d" % pnum)
        page.wait_for_load_state("load")
        inventory += scrape_table(page, {"number": 5, "status": 6, "type": 9})
        # each row also carries row["data"]["amountUsd"] from its checkbox

    dead = [r for r in inventory if r["status"] in DEAD_STATUSES]
    live = [r for r in inventory if r["status"] not in DEAD_STATUSES]
    months = {r["date"][:7] for r in inventory}
    if len(months) == 1:
        runner.skip_data("TC-MTH-003", "staging has one month only",
                         unblocked_by="one multi-month service-delivery invoice")
        ...

    # Step 4-5: one isolated check per remaining case
    runner.run("TC-MTH-001", lambda: check_chip_strip(page, expected_total=len(inventory)))
    ...

runner.print_summary()
runner.write_workbook(plan)     # dated copy
```

## Gotchas, each of which cost a full run

- **`page.goto` on a URL that responds with a file** raises `Download is
  starting`. Use `download_via_navigation`, which sets `window.location.href`
  from inside the page instead.
- **Alpine dropdowns do not close on Escape.** Pressing Escape leaves `open`
  true, so clicking the trigger again *closes* it and the submit button inside
  is invisible. Check whether the item you want is already visible before
  clicking the trigger.
- **django-tables2 pagers here are JS, not links.** There is no `a[href*=page=]`
  — it is `<button @click="goToPage('page', 2)">`. Use `go_to_page(page, 2)`.
  That function reads `window.location.href` and only sets the page param, so
  existing filters survive paging.
- **Tailwind `uppercase` changes `innerText`.** A label rendered as
  "All 23 exportable invoices" comes back as "ALL 23 EXPORTABLE INVOICES".
  Match case-insensitively.
- **Rows on the Prerequisites tab can start with "TC-".** Match case ids with a
  strict pattern, not `startswith`. (The harness now does.)

## Judgement calls worth copying

- **23 vs 25 looked like a bug and was not.** The header said "25 invoices in
  September 2026", the export dropdown said "All 23 exportable". Two invoices
  were genuinely Cancelled and Rejected, so 23 was right and the check was
  wrong. Scraping the inventory first would have made this obvious — it is why
  Step 3 exists.
- **The plan's prerequisites were unusable.** They named invoices `INV-AUG-01`
  and `INV/SEP 01`; the product generates invoice numbers itself into a
  read-only field. Correct the plan rather than working around it — and note
  the product question it raises (the export sanitises "user-supplied" invoice
  numbers that a user cannot actually supply).
- **The strongest case was a byte comparison.** TC-ZIP-005 asked whether a PDF
  rendered in a background worker matches one rendered in a request. Comparing
  extracted *text* passed, but the real proof was decompressing both content
  streams: identical 6136 bytes, same 66 curve operators. When a case asks "is
  this the same", compare the artifacts, not a description of them.
- **A passing check that could not have failed.** Every invoice was 5.27 USD, so
  the selection-total check confirmed `2 × 5.27 = 10.54` — true whichever two
  rows the code summed. Reported as a weak pass, not a clean one.
- **`page.images` is empty for an SVG logo.** WeasyPrint draws SVG as vector
  paths, so an assertion that the PDF "embeds an image" fails on a perfectly
  good PDF. Assert on what the renderer actually produces.
