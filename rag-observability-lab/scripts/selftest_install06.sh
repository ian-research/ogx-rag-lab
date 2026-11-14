#!/usr/bin/env bash
# Isolated checks for the install contract: stubbed systemctl/podman with real
# state tracking, throwaway directories, never touching the running lab.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
root=$(mktemp -d)
fail() { echo "FAIL: $*" >&2; exit 1; }
hashfile() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

make_env() {  # name
  ROOTDIR="${root}/$1"
  mkdir -p "${ROOTDIR}/units" "${ROOTDIR}/etc" "${ROOTDIR}/state/private" "${ROOTDIR}/state/data"
  mkdir -p "${ROOTDIR}/svc"
  echo '{"lab-a":"x"}' > "${ROOTDIR}/state/private/sessions.json"
  cat > "${ROOTDIR}/systemctl" <<'STUB'
#!/usr/bin/env bash
args=("$@")
cmd=${args[0]:-}
unit=""
now=0
for a in "${args[@]:1}"; do
  case "${a}" in
    --now) now=1 ;;
    daemon-reload|*) [[ ${a} == *.service ]] && unit=${a%.service} ;;
  esac
done
dir="${STUB_STATE:?}/svc/${unit}"
[[ -n ${unit} ]] && mkdir -p "${dir}"
hit=0
if [[ -n ${FAIL_ON:-} && "${cmd} ${unit}" == "${FAIL_ON}" ]]; then hit=1; fi
case "${cmd}" in
  is-enabled) [[ -f "${dir}/enabled" ]] && echo enabled || echo disabled ;;
  is-active)  [[ -f "${dir}/active" ]] && echo active || echo inactive ;;
  enable)     touch "${dir}/enabled"; (( now )) && touch "${dir}/active" ;;
  disable)    rm -f "${dir}/enabled"; (( now )) && rm -f "${dir}/active" ;;
  start)      touch "${dir}/active" ;;
  stop)       rm -f "${dir}/active" ;;
esac
echo "systemctl ${cmd} ${unit}" >> "${SYSTEMCTL_LOG:?}"
if (( hit )); then
  # A failed "enable --now" may already have enabled the unit: record that.
  [[ ${cmd} == enable ]] && touch "${dir}/enabled"
  exit 1
fi
exit 0
STUB
  chmod +x "${ROOTDIR}/systemctl"
  cat > "${ROOTDIR}/podman" <<'POD'
#!/usr/bin/env bash
if [[ "$1" == "image" && "$2" == "exists" ]]; then
  [[ ${FAIL_IMAGE:-0} == 1 ]] && exit 1
  exit 0
fi
exit 0
POD
  chmod +x "${ROOTDIR}/podman"
  mkdir -p "${ROOTDIR}/app/.venv/bin" "${ROOTDIR}/app/deploy"
  cp "${here}"/deploy/*.in "${ROOTDIR}/app/deploy/"
  printf '#!/usr/bin/env bash\nexit 0\n' > "${ROOTDIR}/app/.venv/bin/python"
  chmod +x "${ROOTDIR}/app/.venv/bin/python"
  export SYSTEMCTL="${ROOTDIR}/systemctl" SYSTEMCTL_LOG="${ROOTDIR}/calls.log" STUB_STATE="${ROOTDIR}"
  export UNIT_DIR="${ROOTDIR}/units" CONFIG_PATH="${ROOTDIR}/etc/article06-runtime.env"
  export STATE_DIR="${ROOTDIR}/state" PODMAN="${ROOTDIR}/podman"
  export ALLOW_NONROOT=1 SKIP_PREFLIGHT=1 FAIL_IMAGE=0 FAIL_ON=""
}

run() { APP_DIR="${ROOTDIR}/app" "${here}/scripts/install06.sh" "$@"; }
st() { "${SYSTEMCTL}" "$1" "$2.service"; }

# 1. Image missing -> refuse before writing or stopping anything.
make_env preflight
FAIL_IMAGE=1 SKIP_PREFLIGHT=0
run >/dev/null 2>&1 && fail "missing image accepted"
[[ ! -e ${CONFIG_PATH} ]] || fail "config written despite failed preflight"
[[ ! -e ${UNIT_DIR}/article06-api.service ]] || fail "unit written despite failed preflight"
grep -q "stop" "${SYSTEMCTL_LOG}" 2>/dev/null && fail "systemctl stop called before preflight passed"

# 2. Clean install creates config and three units.
make_env fresh
run >/dev/null 2>&1 || fail "clean install failed"
for u in article06-jaeger article06-mcp article06-api; do
  [[ -e ${UNIT_DIR}/${u}.service ]] || fail "missing unit ${u}"
done
grep -q "@JAEGER_IMAGE@" "${UNIT_DIR}/article06-jaeger.service" && fail "unrendered image placeholder"

# 3. Existing units are refused and left byte-identical.
sum_before=$(hashfile "${UNIT_DIR}/article06-api.service")
run >/dev/null 2>&1 && fail "existing units overwritten without replace mode"
sum_after=$(hashfile "${UNIT_DIR}/article06-api.service")
[[ ${sum_before} == "${sum_after}" ]] || fail "existing unit changed"

# 4. Replace mode success keeps a backup.
ARTICLE06_REPLACE=1 run >/dev/null 2>&1 || fail "replace mode failed"
ls "${STATE_DIR}"/install-backup-* >/dev/null 2>&1 || fail "no backup directory"

# 5. Replace mode failure restores files, enablement and activity.
make_env rollback
run >/dev/null 2>&1 || fail "seed install failed"
for u in article06-jaeger article06-mcp article06-api; do
  "${SYSTEMCTL}" enable "${u}.service" >/dev/null
  "${SYSTEMCTL}" stop "${u}.service" >/dev/null
done
echo "# local marker" >> "${UNIT_DIR}/article06-api.service"
marked=$(hashfile "${UNIT_DIR}/article06-api.service")
old_cfg=$(hashfile "${CONFIG_PATH}")
ARTICLE06_REPLACE=1 FAIL_ON="enable article06-mcp" run >/dev/null 2>&1 && fail "failed install returned success"
[[ $(hashfile "${UNIT_DIR}/article06-api.service") == "${marked}" ]] || fail "previous unit not restored"
[[ $(hashfile "${CONFIG_PATH}") == "${old_cfg}" ]] || fail "config changed by failed install"
for u in article06-jaeger article06-mcp article06-api; do
  [[ $(st is-enabled "${u}") == "enabled" ]] || fail "${u} lost its enabled state after rollback"
  [[ $(st is-active "${u}") == "inactive" ]] || fail "${u} became active although it was stopped before"
done

# 6. A config pointing at another state directory is refused, not edited.
make_env foreign
run >/dev/null 2>&1 || fail "seed install failed"
sed -i.bak "s|^STATE_DIR=.*|STATE_DIR=/somewhere/else|" "${CONFIG_PATH}" && rm -f "${CONFIG_PATH}.bak"
foreign_sum=$(hashfile "${CONFIG_PATH}")
run >/dev/null 2>&1 && fail "foreign state config accepted"
[[ $(hashfile "${CONFIG_PATH}") == "${foreign_sum}" ]] || fail "foreign config edited"

rm -r "${root}"
echo "selftest: ok"
