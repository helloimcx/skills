#!/usr/bin/env python3
"""Fail the aggregate GitHub Actions check unless every suite succeeded."""

from __future__ import annotations

import argparse


RESULTS = ("success", "failure", "cancelled", "skipped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-result", choices=RESULTS, required=True)
    parser.add_argument("--service-result", choices=RESULTS, required=True)
    args = parser.parse_args()

    failed = [
        name
        for name, result in (
            ("repository-tests", args.repository_result),
            ("service-tests", args.service_result),
        )
        if result != "success"
    ]
    if failed:
        print("CI blocked: not all test jobs succeeded: " + ", ".join(failed))
        return 1

    print("CI passed: all test jobs succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
