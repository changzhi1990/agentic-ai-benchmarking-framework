#!/usr/bin/env bash
set -euo pipefail

VM_COUNT="${VM_COUNT:-1}"
BASE_ID="${BASE_ID:-agent}"
BRIDGE_NAME="${BRIDGE_NAME:-aab-fcbr0}"
HOST_IP="${HOST_IP:-172.16.0.1}"
CIDR="${CIDR:-24}"
TAP_PREFIX="${TAP_PREFIX:-tap-${BASE_ID}-}"
TAP_OWNER="${TAP_OWNER:-${SUDO_USER:-$USER}}"

sudo ip link show "${BRIDGE_NAME}" >/dev/null 2>&1 || sudo ip link add name "${BRIDGE_NAME}" type bridge
sudo ip addr show dev "${BRIDGE_NAME}" | grep -q "${HOST_IP}/${CIDR}" || sudo ip addr add "${HOST_IP}/${CIDR}" dev "${BRIDGE_NAME}"
sudo ip link set "${BRIDGE_NAME}" up
sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null

for index in $(seq 0 $((VM_COUNT - 1))); do
  tap_name="${TAP_PREFIX}$(printf '%03d' "${index}")"
  if ! sudo ip link show "${tap_name}" >/dev/null 2>&1; then
    sudo ip tuntap add dev "${tap_name}" mode tap user "${TAP_OWNER}"
  fi
  sudo ip link set "${tap_name}" master "${BRIDGE_NAME}"
  sudo ip link set "${tap_name}" up
done

ip addr show dev "${BRIDGE_NAME}"
for index in $(seq 0 $((VM_COUNT - 1))); do
  sudo ip link show "${TAP_PREFIX}$(printf '%03d' "${index}")"
done
