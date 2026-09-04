#!/usr/bin/env python
"""Build and apply the work-level Chinese title review catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from imslp_library.title_review import (
    apply_reviewed_titles,
    apply_review_supplements,
    audit_reviewed_titles,
    build_canonical_review,
    load_reviewed_titles,
    write_json_atomic,
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--corpus", type=Path, required=True)
    build.add_argument("--review", type=Path, action="append", required=True)
    build.add_argument("--supplement", type=Path, action="append", default=[])
    build.add_argument("--reviewed-at", required=True)
    build.add_argument("--output", type=Path, required=True)

    apply = subparsers.add_parser("apply")
    apply.add_argument("--root", type=Path, required=True)
    apply.add_argument("--catalog", type=Path, required=True)

    audit = subparsers.add_parser("audit")
    audit.add_argument("--root", type=Path, required=True)
    audit.add_argument("--catalog", type=Path, required=True)
    audit.add_argument("--output", type=Path)

    args = parser.parse_args()
    if args.command == "build":
        payload = build_canonical_review(
            read_json(args.corpus),
            [read_json(path) for path in args.review],
            reviewed_at=args.reviewed_at,
        )
        if args.supplement:
            payload = apply_review_supplements(
                payload,
                [read_json(path) for path in args.supplement],
            )
        write_json_atomic(args.output, payload)
        print(json.dumps({"catalog": str(args.output), **payload["summary"]}, ensure_ascii=False))
        return 0

    reviewed = load_reviewed_titles(args.catalog.resolve())
    if args.command == "apply":
        summary = apply_reviewed_titles(args.root.resolve(), reviewed)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    summary = audit_reviewed_titles(args.root.resolve(), reviewed)
    if args.output:
        write_json_atomic(args.output.resolve(), summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
