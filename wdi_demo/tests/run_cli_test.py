"""
Smoke-tests build_db.py's actual CLI entry point (argparse, --dry-run,
--force, and the MAX_DB_BYTES size-budget gate), reusing the same fixture
cache as run_pipeline_test.py so no network access is needed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from tests.build_fixtures import build as build_fixtures, FIXTURE_ROOT, TEST_WDI_CODES, YEAR_START, YEAR_END

config.CACHE_DIR = FIXTURE_ROOT
config.WDI_INDICATOR_CODES = TEST_WDI_CODES
config.WPP_INDICATORS = {49: "TPopulation", 61: "E0", 9999: "Bogus"}
config.WDI_YEAR_START, config.WDI_YEAR_END = YEAR_START, YEAR_END
config.WPP_YEAR_START, config.WPP_YEAR_END = YEAR_START, YEAR_END

import build_db  # noqa: E402


def run_main(argv):
    old_argv = sys.argv
    sys.argv = ["build_db.py"] + argv
    try:
        build_db.main()
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old_argv


def main():
    build_fixtures()

    print("--- --dry-run with a generous budget: should exit 0, touch no DB ---")
    config.MAX_DB_BYTES = 10_000_000
    code = run_main(["--dry-run"])
    assert code == 0, f"expected exit 0, got {code}"
    print("OK\n")

    print("--- --dry-run with a tiny budget: should refuse (exit 1) ---")
    config.MAX_DB_BYTES = 10
    code = run_main(["--dry-run"])
    assert code == 1, f"expected exit 1, got {code}"
    print("OK\n")

    print("--- --dry-run --force with a tiny budget: should still just dry-run (exit 0) ---")
    code = run_main(["--dry-run", "--force"])
    assert code == 0, f"expected exit 0, got {code}"
    print("OK\n")

    print("All CLI checks passed.")


if __name__ == "__main__":
    main()
