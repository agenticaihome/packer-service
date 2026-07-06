#!/bin/bash
# Celaut init entrypoint for the packer service (declared in service.json ->
# init.entry_path). The microVM init runs this; Docker CMD/ENTRYPOINT are ignored
# by the packer that builds THIS image.
#
# Sequence:
#   1. Boot a native Docker daemon inside the cloud-hypervisor guest, on the
#      socket nodo expects (NODO_ROOT/docker/docker.sock).
#   2. Create a host-network buildx builder under nodo's DOCKER_CONFIG so the
#      vendored packer worker (which shells out to `docker buildx build
#      --builder nodo-hostnet`) finds it.
#   3. Export the env the nodo ConfigManager / packer expect.
#   4. Launch the HTTP packer front-end on 0.0.0.0:8080.
set -e

export NODO_DIR="${NODO_DIR:-/opt/nodo}"
export MAIN_DIR="${NODO_DIR}"
export PYTHONPATH="${NODO_DIR}:${PYTHONPATH}"
# Static Docker engine binaries live under NODO_DIR/bin (see Dockerfile).
# Pin the standard system dirs explicitly: this script is PID 1 in the CH guest,
# and switch_root gives init a minimal env, so ${PATH} can be empty here — leaving
# coreutils (mkdir/mount/…) unresolvable. Prepending nodo/bin + the usual dirs.
export PATH="${NODO_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH}"
# nodo's runtime.py points DOCKER_CONFIG / buildx state here; create the builder
# with the SAME config dir so the worker sees the nodo-hostnet builder.
export DOCKER_CONFIG="${NODO_DIR}/libexec/docker"
export DOCKER_HOST="unix://${NODO_DIR}/docker/docker.sock"

export CACHE="${CACHE:-/var/lib/celaut/cache/}"
export BLOCKDIR="${BLOCKDIR:-/var/lib/celaut/blocks/}"
# Packer tunables consumed by ConfigManager (safe defaults mirroring nodo).
export PACKER_MEMORY_SIZE_FACTOR="${PACKER_MEMORY_SIZE_FACTOR:-2}"
export MIN_BUFFER_BLOCK_SIZE="${MIN_BUFFER_BLOCK_SIZE:-1048576}"
export SAVE_ALL="${SAVE_ALL:-False}"
# Build with the host-network builder so buildx egress uses the VM's (*) channel.
export BUILDX_BUILDER="${BUILDX_BUILDER:-nodo-hostnet}"
# Force pure-python protobuf so the xattrs map serializes in the same byte order
# as the node packer (also pinned to pure-python) -> matching, stable service-id.
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION="python"

# --- cgroups -----------------------------------------------------------------
# The CH guest has NO init system: this script is PID 1 (nodo's initramfs does a
# switch_root straight into it). nodo's initramfs mounts /proc /sys /dev /dev/shm
# /run but NOT the cgroup hierarchy, and there is no systemd to mount it. Rootful
# dockerd (v27) requires cgroups; with none mounted it probes /sys/fs/cgroup, sees
# no cgroup2 magic, defaults to legacy cgroup-v1 mode, finds no controller mounts,
# and aborts at startup with "Devices cgroup isn't mounted". dockerd exits, this
# PID-1 script exits, and the kernel panics ("Attempted to kill init").
#
# Fix: mount the unified cgroup-v2 hierarchy before dockerd. On v2 device control
# is eBPF-based (no separate /sys/fs/cgroup/devices mount), so the v1 devices check
# is bypassed and dockerd starts in cgroup-v2 mode (guest kernel 5.15 supports it).
# Guard on cgroup.controllers so this is a no-op if a future initramfs already
# mounts it. Non-fatal on failure so the diagnostic reaches the serial log.
if [ ! -e /sys/fs/cgroup/cgroup.controllers ]; then
  echo "[start] mounting cgroup2 at /sys/fs/cgroup (no init system in guest) ..."
  mkdir -p /sys/fs/cgroup
  mount -t cgroup2 none /sys/fs/cgroup \
    || echo "[start] WARN: cgroup2 mount failed; dockerd will likely fail." >&2
else
  echo "[start] cgroup2 already mounted at /sys/fs/cgroup."
fi

mkdir -p "$CACHE" "$BLOCKDIR" \
    "${NODO_DIR}/docker/data" "${NODO_DIR}/docker/exec" "${DOCKER_CONFIG}"

# Remove any stale pidfile/socket from a previous boot (e.g. a microVM/container
# restart) so dockerd doesn't refuse to start with "process N is still running".
rm -f "${NODO_DIR}/docker/docker.pid" "${NODO_DIR}/docker/docker.sock"

# The CH guest's kernel ip= boot path points resolv.conf at the nodo gateway,
# but there is no DNS proxy listening there. Use public resolvers over the
# wildcard egress channel so dockerd itself can resolve registries.
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >/etc/resolv.conf || true

start_dockerd() {
  local storage_driver="$1"
  echo "[start] booting native dockerd (${storage_driver}) on ${NODO_DIR}/docker/docker.sock ..."
  "${NODO_DIR}/bin/dockerd" \
      --host="unix://${NODO_DIR}/docker/docker.sock" \
      --data-root="${NODO_DIR}/docker/data" \
      --exec-root="${NODO_DIR}/docker/exec" \
      --pidfile="${NODO_DIR}/docker/docker.pid" \
      --bridge=none \
      --iptables=false \
      --ip6tables=false \
      --dns=1.1.1.1 \
      --dns=8.8.8.8 \
      --storage-driver="${storage_driver}" \
      >/var/log/dockerd.log 2>&1 &
}

wait_for_dockerd() {
  local tries=0
  until docker info >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 90 ]; then
      return 1
    fi
    sleep 1
  done
}

start_dockerd overlay2

if ! wait_for_dockerd; then
  echo "[start] dockerd overlay2 startup failed; retrying with vfs fallback." >&2
  tail -n 40 /var/log/dockerd.log >&2 || true
  if [ -f "${NODO_DIR}/docker/docker.pid" ]; then
    kill "$(cat "${NODO_DIR}/docker/docker.pid")" 2>/dev/null || true
  fi
  sleep 2
  rm -f "${NODO_DIR}/docker/docker.pid" "${NODO_DIR}/docker/docker.sock"
  start_dockerd vfs
  if ! wait_for_dockerd; then
    echo "[start] dockerd did not become ready in time; last log:" >&2
    tail -n 80 /var/log/dockerd.log >&2 || true
    exit 1
  fi
fi
echo "[start] dockerd ready."

# Host-network buildx builder (matches nodo's packer.docker.BUILDX_BUILDER).
if ! docker buildx inspect "$BUILDX_BUILDER" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDX_BUILDER" \
    --driver docker-container --driver-opt network=host >/dev/null 2>&1 || true
fi
docker buildx inspect "$BUILDX_BUILDER" --bootstrap >/dev/null 2>&1 || true
echo "[start] buildx builder '$BUILDX_BUILDER' ready."

# --- qemu/binfmt for cross-arch (arm64) emulation ----------------------------
# The packer host is amd64; to ALSO pack linux/arm64 services it must emulate
# arm. Register qemu binfmt handlers in the (DinD) kernel so buildx/buildkit
# can build linux/arm64. tonistiigi/binfmt installs static qemu interpreters +
# binfmt_misc entries; pulls over the service's network: tag=(*) egress.
# Idempotent; gated by PACKER_ENABLE_QEMU (default on).
if [ "${PACKER_ENABLE_QEMU:-1}" = "1" ]; then
  if docker run --privileged --rm tonistiigi/binfmt --install arm64 \
       >/var/log/binfmt.log 2>&1; then
    echo "[start] qemu binfmt arm64 registered:"
    ls /proc/sys/fs/binfmt_misc/ 2>/dev/null | grep -i 'qemu-aarch64' \
      | sed 's/^/[start]   /' || echo "[start]   (handler not visible in binfmt_misc)"
  else
    echo "[start] WARN: qemu binfmt install failed — arm64 packing unavailable." >&2
    tail -n 20 /var/log/binfmt.log >&2 || true
  fi
fi

# --- Browser IDE (code-server) ------------------------------------------------
# Serve a full VS Code on :8443 so users can pick a language template, edit, and
# pack inside this same microVM. Access is mediated by the nodo network/DNAT
# layer, so code-server runs with auth disabled.
export WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
export TEMPLATES_DIR="${TEMPLATES_DIR:-/opt/ide/templates}"
export SERVICE_GUIDE="${SERVICE_GUIDE:-/opt/ide/SERVICE_CONFIG_GUIDE.md}"
export PACKER_URL="${PACKER_URL:-http://127.0.0.1:8080}"
IDE_PORT="${IDE_PORT:-8443}"

# Seed the workspace once (no-clobber, so user edits survive a microVM restart).
mkdir -p "$WORKSPACE_DIR"
cp -rn /opt/ide/workspace/. "$WORKSPACE_DIR"/ 2>/dev/null || true
mkdir -p "$WORKSPACE_DIR/templates"
cp -rn "$TEMPLATES_DIR"/. "$WORKSPACE_DIR/templates"/ 2>/dev/null || true
# The configuration guide is ALWAYS present: refreshed at the workspace root and
# alongside every reference template.
if [ -f "$SERVICE_GUIDE" ]; then
  cp -f "$SERVICE_GUIDE" "$WORKSPACE_DIR/SERVICE_CONFIG_GUIDE.md" || true
  for d in "$WORKSPACE_DIR"/templates/*/; do
    [ -d "$d" ] && cp -f "$SERVICE_GUIDE" "${d}SERVICE_CONFIG_GUIDE.md" || true
  done
fi

if command -v code-server >/dev/null 2>&1; then
  echo "[start] launching code-server (VS Code Web) on :${IDE_PORT}"
  code-server \
    --bind-addr "0.0.0.0:${IDE_PORT}" \
    --auth none \
    --disable-telemetry \
    --disable-update-check \
    "$WORKSPACE_DIR" \
    >/var/log/code-server.log 2>&1 &
else
  echo "[start] WARNING: code-server not installed; IDE on :${IDE_PORT} disabled." >&2
fi

echo "[start] launching packer HTTP server on :8080"
exec python3 /app/server.py
