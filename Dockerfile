# Celaut builds this image only to export its filesystem (docker buildx
# --output type=tar); the container is never run by Docker directly. CMD/
# ENTRYPOINT/EXPOSE are intentionally omitted — the entrypoint, ports and envs
# are declared in service.json. The packed service runs as a cloud-hypervisor
# microVM whose init runs /app/start.sh.
#
# This service does "the same as `nodo pack`" from INSIDE a sealed microVM: it
# boots its own native dockerd inside the guest and runs `docker buildx build`
# to compile the image being packed. That nested build is why service.json
# declares network: tag=(*) (buildx must pull base images).
#
# WHY A GLIBC (debian) BASE AND NOT docker:27-dind (alpine):
#   The packer vendors nodo, whose protobuf stack (bee_rpc's generated *_pb2)
#   requires protobuf's C backend (`google._upb`). protobuf 4.23.3 ships no
#   musllinux wheel with that extension, so on Alpine `import bee_rpc` dies with
#   "No module named 'google._upb'". On glibc the manylinux wheel provides it.
#   We still FORCE pure-python protobuf at runtime (see ENV below) for a stable,
#   deterministic xattrs-map serialization — but the package must be importable,
#   which needs glibc. The Docker engine itself is static, so we lift its
#   binaries out of the official docker:27-dind image (pinned by digest).

# ---- stage: official Docker image (source of static engine binaries) ---------
FROM docker:27-dind@sha256:aa3df78ecf320f5fafdce71c659f1629e96e9de0968305fe1de670e0ca9176ce AS dind

# ---- runtime: debian (glibc) -------------------------------------------------
FROM debian:bookworm-slim

ARG NODO_REF=stable
ENV NODO_DIR=/opt/nodo \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    # Force pure-python protobuf. protobuf MAP fields (the filesystem xattrs)
    # serialize in a DIFFERENT (and, for upb, time-unstable) byte order under the
    # C backend vs pure-python, which changes the service-id. The node packer is
    # pinned to pure-python too (pack_zip injects this into its worker), so both
    # sides serialize identically and reproducibly.
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# DinD runtime support tools (glibc side): iptables/iproute2 for networking,
# kmod for modprobe, e2fsprogs/xfsprogs for storage, pigz/xz for layer (de)compress.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev build-essential git ca-certificates \
        unzip zip curl \
        iptables iproute2 kmod e2fsprogs xfsprogs pigz xz-utils openssl uidmap \
    && rm -rf /var/lib/apt/lists/*

# Vendor the nodo tree (packer + its src.utils / protos / bee_rpc deps).
RUN git clone --depth 1 --branch "${NODO_REF}" \
        https://github.com/celaut-project/nodo.git "${NODO_DIR}"

# nodo resolves its docker tooling under NODO_ROOT (=/opt/nodo): bin/docker,
# bin/dockerd, libexec/docker/cli-plugins/docker-buildx, and the socket at
# docker/docker.sock — and RAISES if they're missing. Place the static engine
# binaries exactly there (after the clone, so the target dir merges cleanly).
COPY --from=dind /usr/local/bin/ /opt/nodo/bin/
COPY --from=dind /usr/local/libexec/ /opt/nodo/libexec/

# Determinism guards (REQUIRED so this service's ids match `nodo pack` on a
# stable node, and so packing the same project twice yields the same id).
# Upstream nodo's packer WAS nondeterministic two ways, and `stable` has since
# fixed BOTH — so we no longer patch, we VERIFY. This service must serialize with
# upstream's exact logic; re-forcing our old values (e.g. mtime_ns=0 for regular
# files) would now DIVERGE from a stable node's `nodo pack` and change the id.
#   1. recursive_parsing now iterates sorted(os.listdir()) -> stable branch order
#      (was UNSORTED -> serialized bytes varied between extractions of one tar).
#   2. get_filesystem_metadata now zeroes ONLY symlink mtimes -- tar extraction
#      re-stamps symlink mtimes to wall-clock each pass, while regular-file mtimes
#      are restored from the tar and stay deterministic (was: hashed raw mtime_ns
#      for everything -> id changed every pack).
# The greps below fail the build LOUDLY if a future upstream drift removes either
# property, rather than silently packing a service whose id won't match the node.
RUN grep -q 'sorted(os.listdir(host_dir + directory))' \
        "${NODO_DIR}/src/packers/zip_with_dockerfile.py" \
    && grep -q 'mtime_ns=(0 if stat.S_ISLNK(mode) else int(stat_result.st_mtime_ns))' \
        "${NODO_DIR}/src/utils/filesystem_xattrs.py" \
    && echo "determinism guards verified (upstream stable already deterministic)"

# Install exactly the deps the packer worker (src/packers/zip_with_dockerfile)
# imports — NOT nodo's full requirements (those drag in the Ergo payment stack:
# ergpy/jpype1/coincurve/bip32/mnemonic, which need a JVM + libsecp256k1 and are
# never imported by the packer).
#   * protobuf==4.23.3 — matches the node's runtime + the protoc that generated
#     nodo's vendored protos/*_pb2.py.
#   * bee_rpc @ the exact git commit the node uses — owns the multiblock/.bee
#     block format the id is hashed over. Installed --no-deps so we control
#     protobuf/grpcio (bee_rpc hard-pins grpcio==1.56.0, which has no wheel here;
#     grpcio is only the streaming transport and contributes no hashed bytes).
RUN pip3 install --no-cache-dir \
        "protobuf==4.23.3" "grpcio" \
        "docker==6.1.3" requests requests-unixsocket "PyYAML==6.0.1" \
        "python-dotenv==1.0.0" psutil netifaces2 tabulate packaging \
        typing_extensions six mnemonic \
    && pip3 install --no-cache-dir --no-deps \
        "git+https://github.com/bee-rpc-protocol/bee-rpc-over-grpc-py@7a2a344bc29546328bcf2f753dc2407f2c226376" \
    && python3 -c "from bee_rpc import client; import google.protobuf; from google.protobuf.internal import api_implementation as a; assert a.Type()=='python', 'expected pure-python protobuf, got '+a.Type(); print('deps ok, protobuf', google.protobuf.__version__, a.Type())"

# Packer config. The vendored ConfigManager loads "config.yaml" relative to the
# worker's cwd (=NODO_DIR), and RAISES if absent; it also drives the SERVICE-ID
# (hash spec = sha3_256, MIN_BUFFER_BLOCK_SIZE = 10MB, packer arch support). This
# is the node's own config, sanitized (no ledgers/secrets/token) and re-pathed to
# /opt/nodo, so the service packs byte-identically to `nodo pack`.
COPY ./packer-config.yaml /opt/nodo/config.yaml

# --- Browser IDE (code-server) ------------------------------------------------
# A full VS Code in the browser, served on :8443 alongside the packer API. Users
# pick a language template, edit, and pack — all inside this sealed microVM.
# code-server is a glibc binary; the standalone install works on debian.
ARG CODE_SERVER_VERSION=4.91.1
RUN curl -fsSL https://code-server.dev/install.sh | sh -s -- --version "${CODE_SERVER_VERSION}" \
    && code-server --version

# IDE assets: the always-bundled config guide, language templates, the seed
# workspace (START_HERE + VS Code template-picker task), and the helper CLIs
# (new-service / pack-service) placed on PATH for the IDE terminal.
COPY ./ide/SERVICE_CONFIG_GUIDE.md /opt/ide/SERVICE_CONFIG_GUIDE.md
COPY ./ide/templates /opt/ide/templates
COPY ./ide/workspace /opt/ide/workspace
# NOTE: split into single-source COPYs — nodo's build-context path rewriter
# only prefixes the FIRST source arg of a multi-source COPY, so a combined
# `COPY a b /dst/` leaves `b` resolving outside the service/ prefix and fails.
COPY ./ide/bin/new-service  /usr/local/bin/
COPY ./ide/bin/pack-service /usr/local/bin/
RUN chmod +x /usr/local/bin/new-service /usr/local/bin/pack-service

# --- Application --------------------------------------------------------------
WORKDIR /app
COPY ./server.py /app/server.py
COPY ./start.sh  /app/start.sh
RUN chmod +x /app/start.sh
