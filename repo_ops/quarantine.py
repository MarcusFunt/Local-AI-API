"""Record evidence from a manually operated, disposable skill quarantine."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Catalog URL or registry identifier.")
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--license", required=True)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--smoke-result", required=True, choices=("passed", "failed"))
    parser.add_argument("--snapshot", required=True, help="Pre-install volume snapshot identifier.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    artifact = args.artifact.read_bytes()
    evidence = {
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": args.source,
        "package": args.package,
        "version": args.version,
        "license": args.license,
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "smoke_result": args.smoke_result,
        "snapshot": args.snapshot,
        "promotion": "manual approval required",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
