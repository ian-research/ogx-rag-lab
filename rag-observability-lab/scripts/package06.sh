#!/usr/bin/env bash
# Build a whitelisted source archive: code only, no state, no evidence, no keys.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-/tmp/article06-r2-src.tar.gz}
list=$(mktemp)
trap 'rm -f "${list}"' EXIT
cd "${here}"
for path in action_api.py mcp_action_server.py model_loop.py docsearch.py draft.py store.py \
            security.py settings.py telemetry.py metrics.py devices.py upstream_devices.json \
            init_private.py requirements.lock requirements-otel.in requirements-otel.lock \
            requirements-test.in requirements-test.lock README.txt DEPLOYMENT.txt \
            README.md DEPLOYMENT.md TESTING.md PROVENANCE.json BUILD-MANIFEST.json \
            LICENSE LICENSE-UPSTREAM .gitignore \
            deploy docs web tests scripts/e06r2.py scripts/e07_collect.py scripts/install06.sh \
            scripts/rollback06.sh scripts/selftest_install06.sh scripts/package06.sh \
            scripts/manifest.py scripts/server_manifest.py scripts/check_state.py \
            scripts/check_isolation.py scripts/check_confirmation.py; do
  [[ -e ${path} ]] && echo "${path}" >> "${list}"
done
# Generated artefacts never belong in a source archive.
COPYFILE_DISABLE=1 tar czf "${out}" --no-xattrs --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='._*' --exclude='.pytest_cache' -T "${list}"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${out}" > "${out}.sha256"
else
  shasum -a 256 "${out}" > "${out}.sha256"
fi
echo "archive ${out} ($(wc -l < "${list}") paths)"
cat "${out}.sha256"
