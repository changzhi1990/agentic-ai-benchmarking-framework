#!/usr/bin/env bash
set -euo pipefail

BASE_ROOTFS_IMAGE="${BASE_ROOTFS_IMAGE:-/opt/firecracker/rootfs.ext4}"
OUTPUT_ROOTFS_IMAGE="${OUTPUT_ROOTFS_IMAGE:-/opt/firecracker/rootfs-mini-swe-agent.ext4}"
MOUNT_DIR="${MOUNT_DIR:-/mnt/aab-firecracker-mini-swe-rootfs}"
MINI_SWE_REPO_URL="${MINI_SWE_REPO_URL:-https://github.com/SWE-agent/mini-swe-agent}"
WHEELHOUSE="${WHEELHOUSE:-}"

if [[ ! -f "${BASE_ROOTFS_IMAGE}" ]]; then
  echo "Missing base rootfs image: ${BASE_ROOTFS_IMAGE}" >&2
  exit 1
fi

cp "${BASE_ROOTFS_IMAGE}" "${OUTPUT_ROOTFS_IMAGE}"
ROOTFS_IMAGE="${OUTPUT_ROOTFS_IMAGE}" MOUNT_DIR="${MOUNT_DIR}" ./bin/customize_firecracker_rootfs.sh

sudo mkdir -p "${MOUNT_DIR}"
if mountpoint -q "${MOUNT_DIR}"; then
  sudo umount "${MOUNT_DIR}"
fi
sudo mount -o loop "${OUTPUT_ROOTFS_IMAGE}" "${MOUNT_DIR}"
cleanup_mount() {
  if mountpoint -q "${MOUNT_DIR}"; then
    sudo umount "${MOUNT_DIR}"
  fi
}
trap cleanup_mount EXIT

sudo mkdir -p "${MOUNT_DIR}/opt/mini-swe-agent" "${MOUNT_DIR}/opt/agent-runtime" "${MOUNT_DIR}/output" "${MOUNT_DIR}/work" "${MOUNT_DIR}/task"

if [[ -n "${WHEELHOUSE}" && -d "${WHEELHOUSE}" ]]; then
  sudo mkdir -p "${MOUNT_DIR}/opt/wheelhouse"
  sudo cp -a "${WHEELHOUSE}/." "${MOUNT_DIR}/opt/wheelhouse/"
fi

if command -v git >/dev/null 2>&1 && [[ ! -e "${MOUNT_DIR}/opt/mini-swe-agent/.git" ]]; then
  tmp_repo="$(mktemp -d)"
  git clone --depth 1 "${MINI_SWE_REPO_URL}" "${tmp_repo}/mini-swe-agent"
  sudo cp -a "${tmp_repo}/mini-swe-agent/." "${MOUNT_DIR}/opt/mini-swe-agent/"
  rm -rf "${tmp_repo}"
fi

if sudo chroot "${MOUNT_DIR}" /usr/bin/python3 -m pip --version >/dev/null 2>&1; then
  pip_args=(/usr/bin/python3 -m pip install --no-cache-dir)
  if [[ -n "${WHEELHOUSE}" && -d "${WHEELHOUSE}" ]]; then
    pip_args+=(--no-index --find-links /opt/wheelhouse)
  fi
  sudo chroot "${MOUNT_DIR}" "${pip_args[@]}" \
    requests datasets PyYAML rich platformdirs pytest python-dotenv jinja2
  if [[ -d "${MOUNT_DIR}/opt/mini-swe-agent" ]]; then
    sudo chroot "${MOUNT_DIR}" /usr/bin/python3 -m pip install --no-cache-dir /opt/mini-swe-agent || true
  fi
else
  echo "python3/pip not available in rootfs; install Python >=3.10 and pip before running mini-swe-agent" >&2
fi

sudo tee "${MOUNT_DIR}/opt/agent-runtime/rootfs-version.txt" >/dev/null <<EOF
name=firecracker-mini-swe-agent
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
base_rootfs=${BASE_ROOTFS_IMAGE}
mini_swe_repo=${MINI_SWE_REPO_URL}
deps=requests,datasets,PyYAML,rich,platformdirs,pytest,python-dotenv,jinja2
EOF

sudo sync
echo "Built ${OUTPUT_ROOTFS_IMAGE}"
