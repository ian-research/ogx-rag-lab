#!/usr/bin/env bash
# Based on https://github.com/ian-research/vllm_test/blob/main/rhel10_vllm_env_install.sh
# RHEL 10 / x86_64, cuda-drivers + CUDA 13.3 route. No automatic reboot.
set -euo pipefail
[[ "${1:-}" == --install ]] || { echo '用法: sudo bash scripts/rhel10-vllm-prerequisites.sh --install'; exit 2; }
[[ $EUID == 0 ]] || { echo '需要 root'; exit 1; }
source /etc/os-release
[[ "$ID" == rhel && "$VERSION_ID" == 10* && $(uname -m) == x86_64 ]] || { echo '仅支持 RHEL 10 x86_64'; exit 1; }
# CRB_REPO_ID can be a RHUI or subscription repository; never mix both routes.
if [[ -n "${CRB_REPO_ID:-}" ]]; then
  dnf install -y dnf-plugins-core
  dnf config-manager --set-enabled "$CRB_REPO_ID"
fi
dnf repolist --enabled | grep -Ei 'codeready|code.?ready|crb' >/dev/null || {
  echo '请先启用本机对应的 CodeReady Builder 源，或传入 CRB_REPO_ID'; exit 1;
}
dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm
dnf install -y python3 python3-pip python3-devel git podman curl wget \
  ca-certificates pciutils gcc gcc-c++ make ninja-build dnf-plugins-core \
  kernel-devel-matched kernel-headers
python3 -c 'import sys; assert sys.version_info[:2] == (3,12), "此基线要求 Python 3.12"'
if [[ ! -d /usr/src/kernels/$(uname -r) ]]; then
  echo '当前内核与开发包不一致；请重启进入匹配内核后再次运行。尚未安装驱动。'; exit 1
fi
if ! rpm -q cuda-repo-rhel10-13-3-local >/dev/null 2>&1; then
  task_tmp=$(mktemp -d)
  trap 'rm -rf -- "$task_tmp"' EXIT
  curl -fL --retry 3 -o "$task_tmp/cuda-repo.rpm" \
    https://developer.download.nvidia.com/compute/cuda/13.3.1/local_installers/cuda-repo-rhel10-13-3-local-13.3.1_610.43.02-1.x86_64.rpm
  rpm -Uvh "$task_tmp/cuda-repo.rpm"
fi
dnf clean expire-cache
dnf install -y cuda-toolkit-13-3
# Keep working drivers. A different installed version needs explicit review.
if nvidia-smi --query-gpu=driver_version --format=csv,noheader >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version --format=csv
  echo '驱动已工作，保留当前驱动；请与实验基线核对。'
else
  # No -y: inspect the transaction before agreeing; do not substitute open packages.
  dnf install cuda-drivers-610.43.02-1.el10.x86_64
  dracut -f
fi
echo '基础包已处理。请按需重启，再检查 nvidia-smi、/usr/local/cuda-13.3/bin/nvcc --version、ninja --version。'
echo '本流程不安装 NVIDIA Container Toolkit（仅数据库使用容器），不写 GSP 禁用配置。'
