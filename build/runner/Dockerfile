# Tools the native build and test jobs need, installed once instead of on every runner.
# Jobs run this image privileged: Bubblewrap user namespaces, mknod and KVM for the QEMU smoke test.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

LABEL org.opencontainers.image.source="https://github.com/andrebrait/unRAID-NerdTools" \
      org.opencontainers.image.description="NerdTools native package build and test environment"

RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install --no-install-recommends --yes \
      binutils bubblewrap ca-certificates cpio git python3 qemu-system-x86 \
      squashfs-tools sudo uidmap xz-utils zstd \
 && rm -rf /var/lib/apt/lists/* \
 && usermod --add-subuids 100000-165535 --add-subgids 100000-165535 root
