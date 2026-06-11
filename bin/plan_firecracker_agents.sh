#!/usr/bin/env bash
set -euo pipefail

VM_COUNT="${VM_COUNT:-4}"
HOST_VLLM_URL="${HOST_VLLM_URL:-http://172.16.0.1:8000/v1}"
KERNEL_IMAGE="${KERNEL_IMAGE:-/opt/firecracker/vmlinux}"
ROOTFS_IMAGE="${ROOTFS_IMAGE:-/opt/firecracker/rootfs.ext4}"
OUT_DIR="${OUT_DIR:-runs/firecracker-plan}"

python3 -m aab_framework.cli firecracker-preflight \
  --kernel-image "${KERNEL_IMAGE}" \
  --rootfs-image "${ROOTFS_IMAGE}"

python3 -m aab_framework.cli plan-firecracker-agents \
  --vm-count "${VM_COUNT}" \
  --host-vllm-url "${HOST_VLLM_URL}" \
  --kernel-image "${KERNEL_IMAGE}" \
  --rootfs-image "${ROOTFS_IMAGE}" \
  --out-dir "${OUT_DIR}"
