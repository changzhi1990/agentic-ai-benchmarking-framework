#!/usr/bin/env bash
set -euo pipefail

FIRECRACKER_VERSION="${FIRECRACKER_VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/opt/firecracker}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
INSECURE_DOWNLOADS="${INSECURE_DOWNLOADS:-0}"
ARCH="$(uname -m)"
CURL_TLS_ARGS=()
if [[ "${INSECURE_DOWNLOADS}" == "1" ]]; then
  CURL_TLS_ARGS+=("-k")
fi

if [[ "${ARCH}" != "x86_64" && "${ARCH}" != "aarch64" ]]; then
  echo "Unsupported architecture: ${ARCH}" >&2
  exit 1
fi

if [[ "${FIRECRACKER_VERSION}" == "latest" ]]; then
  FIRECRACKER_VERSION="$(basename "$(curl "${CURL_TLS_ARGS[@]}" -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/firecracker-microvm/firecracker/releases/latest)")"
fi

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

echo "Installing Firecracker ${FIRECRACKER_VERSION} for ${ARCH}"
curl "${CURL_TLS_ARGS[@]}" -fsSL \
  "https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/firecracker-${FIRECRACKER_VERSION}-${ARCH}.tgz" \
  -o "${workdir}/firecracker.tgz"
tar -xzf "${workdir}/firecracker.tgz" -C "${workdir}"

release_dir="${workdir}/release-${FIRECRACKER_VERSION}-${ARCH}"
sudo install -m 0755 "${release_dir}/firecracker-${FIRECRACKER_VERSION}-${ARCH}" "${BIN_DIR}/firecracker"
if [[ -f "${release_dir}/jailer-${FIRECRACKER_VERSION}-${ARCH}" ]]; then
  sudo install -m 0755 "${release_dir}/jailer-${FIRECRACKER_VERSION}-${ARCH}" "${BIN_DIR}/jailer"
fi

sudo mkdir -p "${INSTALL_DIR}"
image_base_url="https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/${ARCH}"
sudo curl "${CURL_TLS_ARGS[@]}" -fsSL "${image_base_url}/kernels/vmlinux.bin" -o "${INSTALL_DIR}/vmlinux"
sudo curl "${CURL_TLS_ARGS[@]}" -fsSL "${image_base_url}/rootfs/bionic.rootfs.ext4" -o "${INSTALL_DIR}/rootfs.ext4"
sudo chmod 0644 "${INSTALL_DIR}/vmlinux" "${INSTALL_DIR}/rootfs.ext4"

echo "Installed:"
command -v firecracker
firecracker --version
ls -lh "${INSTALL_DIR}/vmlinux" "${INSTALL_DIR}/rootfs.ext4"
