import sys
from pathlib import Path

REFERENCES = Path(__file__).resolve().parent.parent
HARNESS = REFERENCES.parent.parent / "qa-run-test-plan" / "references"
for p in (REFERENCES, HARNESS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
