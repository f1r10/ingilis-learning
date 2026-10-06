"""Turn one pytest JUnit XML report into a pass/fail gate for verify_phase12.sh.

Usage:
    python scripts/_junit_gate.py <report.xml> <label> [--allow-skips]

The verifier's contract is that a skipped *mandatory* check counts as a failure:
a test that skipped because Postgres, Redis or MinIO was unreachable proved
nothing, so reporting success would be a lie. Skips are only tolerated where the
suite is documented to be infrastructure-free (nothing in the mandatory path).

Exit codes: 0 gate satisfied, 1 gate violated, 2 the report itself is unusable.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def counts(path: str) -> tuple[int, int, int, int, int]:
    """Return (tests, failures, errors, skipped, passed) for a pytest JUnit XML."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError(f"no <testsuite> element found in {path}")

    def total(attr: str) -> int:
        return sum(int(s.get(attr, "0") or 0) for s in suites)

    tests = total("tests")
    failures = total("failures")
    errors = total("errors")
    skipped = sum(
        1 for s in suites for tc in s.iter("testcase") if any(c.tag == "skipped" for c in tc)
    ) or total("skipped")
    return tests, failures, errors, skipped, tests - failures - errors - skipped


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    path, label = argv[1], argv[2]
    allow_skips = "--allow-skips" in argv[3:]

    try:
        tests, failures, errors, skipped, passed = counts(path)
    except (OSError, ET.ParseError, ValueError) as exc:
        print(f"  !! {label}: unreadable test report ({exc})")
        return 2

    print(f"  {label}: {tests} tests | {passed} passed | {failures} failed | {errors} errors | {skipped} skipped")

    problems: list[str] = []
    if tests == 0:
        problems.append("no tests were collected")
    if failures:
        problems.append(f"{failures} test(s) failed")
    if errors:
        problems.append(f"{errors} test(s) errored")
    if skipped and not allow_skips:
        problems.append(f"{skipped} mandatory test(s) skipped - required infrastructure did not run")

    for problem in problems:
        print(f"  !! {label}: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
