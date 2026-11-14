#!/usr/bin/env python3
"""Build a source-only archive from an explicit allowlist, never from live state."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    ".gitignore", "LICENSE", "README.md", "DEPLOYMENT.md", "TESTING.md",
    "requirements.in", "requirements.lock", "requirements-test.in", "requirements-test.lock",
    "insight/app.py", "insight/clients.py", "insight/core.py", "insight/explain.py",
    "web/index.html",
    "deploy/runtime07.env.in", "deploy/article07-api.service.in",
    "deploy/article07-prometheus.service.in", "deploy/prometheus.yml.in",
    "scripts/prepare.sh", "scripts/init_private.py", "scripts/install07.sh",
    "scripts/rollback.sh", "scripts/verify_evidence.py", "scripts/check_page.cjs",
    "scripts/package07.sh", "scripts/package07.py",
    "tests/test_checks.py", "tests/test_installer.py",
    "tests/fixtures/analysis-latency.json", "tests/fixtures/analysis-windows.json",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="/tmp/article07-source.tar.gz")
    args = parser.parse_args()
    output = Path(args.output).expanduser().absolute()
    checksum = Path(str(output) + ".sha256")
    if output.exists() or checksum.exists():
        parser.error("output or checksum already exists; choose another filename")
    payloads = {}
    for name in sorted(FILES):
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            parser.error(f"missing or symlinked source: {name}")
        payloads[name] = path.read_bytes()
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in payloads.items()
    ).encode()
    payloads["SHA256SUMS"] = manifest
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for name, data in sorted(payloads.items()):
                    info = tarfile.TarInfo(f"rag-insight-lab/{name}")
                    info.size = len(data)
                    info.mode = 0o755 if name.endswith(".sh") else 0o644
                    info.mtime = 0
                    archive.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(f"source files: {len(FILES)}; manifest entries: {len(FILES)}")
    print(f"archive: {output}")
    print(f"sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
