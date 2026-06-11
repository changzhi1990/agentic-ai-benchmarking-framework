#!/usr/bin/env bash
set -euo pipefail

ROOTFS_IMAGE="${ROOTFS_IMAGE:-/opt/firecracker/rootfs.ext4}"
MOUNT_DIR="${MOUNT_DIR:-/mnt/aab-firecracker-rootfs}"

if [[ ! -f "${ROOTFS_IMAGE}" ]]; then
  echo "Missing rootfs image: ${ROOTFS_IMAGE}" >&2
  exit 1
fi
if ! command -v gcc >/dev/null 2>&1; then
  echo "gcc is required to build aab-memory-burner" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

python3 - <<'PY' > "${tmpdir}/aab-guest-agent"
from aab_framework.rootfs import build_guest_agent_script
print(build_guest_agent_script(), end="")
PY

python3 - <<'PY' > "${tmpdir}/aab-guest-agent.service"
from aab_framework.rootfs import build_guest_systemd_unit
print(build_guest_systemd_unit(), end="")
PY

python3 - <<'PY' > "${tmpdir}/aab-memory-burner.c"
from aab_framework.rootfs import build_memory_burner_source
print(build_memory_burner_source(), end="")
PY

gcc -O3 -pthread "${tmpdir}/aab-memory-burner.c" -o "${tmpdir}/aab-memory-burner"

sudo mkdir -p "${MOUNT_DIR}"
if mountpoint -q "${MOUNT_DIR}"; then
  sudo umount "${MOUNT_DIR}"
fi
sudo mount -o loop "${ROOTFS_IMAGE}" "${MOUNT_DIR}"
cleanup_mount() {
  if mountpoint -q "${MOUNT_DIR}"; then
    sudo umount "${MOUNT_DIR}"
  fi
}
trap 'cleanup_mount; rm -rf "${tmpdir}"' EXIT

sudo install -m 0755 "${tmpdir}/aab-guest-agent" "${MOUNT_DIR}/usr/local/bin/aab-guest-agent"
sudo install -m 0755 "${tmpdir}/aab-memory-burner" "${MOUNT_DIR}/usr/local/bin/aab-memory-burner"
sudo install -m 0644 "${tmpdir}/aab-guest-agent.service" "${MOUNT_DIR}/etc/systemd/system/aab-guest-agent.service"
sudo mkdir -p "${MOUNT_DIR}/etc/systemd/system/multi-user.target.wants"
sudo ln -sf /etc/systemd/system/aab-guest-agent.service \
  "${MOUNT_DIR}/etc/systemd/system/multi-user.target.wants/aab-guest-agent.service"
sudo mkdir -p "${MOUNT_DIR}/var/lib/aab"
sudo rm -f "${MOUNT_DIR}/var/lib/aab/result.json" "${MOUNT_DIR}/var/lib/aab/trace.jsonl"
sudo sync

echo "Customized ${ROOTFS_IMAGE} with aab-guest-agent.service"
