#!/usr/bin/env python3
"""Verify run -> frozen bundle hashes without changing the evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="runtime STATE_DIR or archived result root")
    parser.add_argument("--output", type=Path, help="optional JSON report")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if (root / "runs").is_dir():
        run_dir, frozen_dir = root / "runs", root / "evidence"
    else:
        run_dir, frozen_dir = root / "evidence/runs", root / "evidence/frozen"
    runs = sorted(run_dir.glob("*.json"))
    rows = []
    for path in runs:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            eid = record.get("evidence_id", "")
            if not isinstance(eid, str) or not re.fullmatch(r"E-[A-Za-z0-9_-]+", eid):
                rows.append({"file": path.name, "status": "invalid_evidence_id"})
                continue
            bundle = frozen_dir / f"{eid}.json"
            if not bundle.is_file():
                rows.append({"file": path.name, "evidence_id": eid, "status": "bundle_missing"})
                continue
            data = json.loads(bundle.read_text(encoding="utf-8"))
            digest = hashlib.sha256(json.dumps(
                data, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")).hexdigest()
            rows.append({"file": path.name, "evidence_id": eid,
                         "status": "match" if digest == record.get("evidence_sha256") else "mismatch"})
        except (ValueError, OSError, AttributeError, TypeError):
            rows.append({"file": path.name, "status": "invalid_record"})
    matched = sum(row["status"] == "match" for row in rows)
    report = {
        "runs": len(runs), "match": matched, "failed": len(runs) - matched,
        "ok": bool(runs) and matched == len(runs),
        "method": "sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True))",
        "rows": rows,
    }
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
