#!/usr/bin/env python3
"""
Celaut Packer Service — HTTP front-end.

A network-facing Celaut microVM service that does *the same thing the nodo
packer does* (src/packers/zip_with_dockerfile.py): given a project archive
containing a Dockerfile, a packer config and a service.json, it builds the
image, exports its filesystem, serialises a content-addressed `celaut.Service`
protobuf, and returns the packed service as a single `.celaut.bee` file.

It does NOT re-implement the packer. It vendors the real nodo packer (cloned
into /opt/nodo at image-build time) and invokes its worker entrypoint, so the
service-id produced here is byte-identical to `nodo pack`. Docker/buildx runs
inside this microVM (docker-in-docker); that is why the service declares
`network: tag=(*)` — buildx must reach arbitrary registries to pull base images.

API (bound on 0.0.0.0:8080):
  GET  /health            -> 200 "ok"
  GET  /registry/<id>     -> 200 JSON {present, metadata, blocks} if the packed
                             dependency <id> is already in this packer's
                             filesystem registry, else 404 {present:false}. Lets
                             a client (nodo) skip re-uploading a dependency the
                             packer already holds.
  POST /registry/<id>     -> body = a gzip tar bundle of an already-packed
                             service (a "registry dependency"). Injected into the
                             packer host's REGISTRY / METADATA_REGISTRY / BLOCKDIR
                             dirs so a subsequent /pack whose pack_config declares
                             <id> as a registry dependency can resolve it (the
                             vendored ggconf reads {REGISTRY}/<id>). Idempotent.
                             Bundle layout (see _store_registry_bundle):
                               service/         -> the multiblock service dir
                                                   (becomes {REGISTRY}/<id>/)
                               metadata         -> optional Metadata protobuf
                                                   (becomes {METADATA_REGISTRY}/<id>)
                               blocks/<blockid> -> optional shared block files
                                                   (become {BLOCKDIR}/<blockid>)
                             200 -> JSON {service_id, stored, already_present,
                                    blocks_added}
                             400 -> malformed/missing id or bad bundle
  POST /pack              -> body = project .zip (application/zip | octet-stream)
                             200 -> application/octet-stream, the `.celaut.bee`
                                    headers: X-Service-Id: <hex>
                                             Content-Disposition: attachment;
                                                 filename="<service_id>.celaut.bee"
                             400 -> config or packing error (text body). The
                                    .service configuration is validated up front
                                    (valid JSON, architecture, entrypoint, api
                                    slots, pack_config) and any problems are
                                    returned as a numbered, plain-English list
                                    pointing at SERVICE_CONFIG_GUIDE.md, so a bad
                                    config never reaches a cryptic build failure.
                             415 -> bad/missing zip

The zip must contain (at its root, or in a single top-level folder):
  - Dockerfile           (root or .service/)
  - service.json         (root or .service/)
  - pack_config.json     (optional; honoured by the nodo packer's ignore globs)
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Where the nodo source tree is vendored in the image (see Dockerfile).
NODO_DIR = os.environ.get("NODO_DIR", "/opt/nodo")
PORT = int(os.environ.get("PORT", "8080"))
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", str(512 * 1024 * 1024)))  # 512MB
# The packer service itself runs on an amd64 host. With qemu/binfmt registered
# in the in-VM Docker (see start.sh), buildx can ALSO emulate arm64 — so this
# packer builds amd64 natively and arm64 via emulation.
PACKER_ARCH = os.environ.get("PACKER_ARCH", "linux/amd64")  # default suggested in errors
SUPPORTED_ARCHES = {
    a.strip() for a in os.environ.get(
        "PACKER_ARCHES", "linux/amd64,linux/arm64").split(",") if a.strip()
}
GUIDE = "SERVICE_CONFIG_GUIDE.md"

# The nodo ConfigManager reads these; start.sh exports them too. Kept here so the
# server is runnable/inspectable on its own.
os.environ.setdefault("CACHE", "/var/lib/celaut/cache/")
os.environ.setdefault("BLOCKDIR", "/var/lib/celaut/blocks/")

# A packed service's on-disk id: content hash (hex) or a tag. Keep it filesystem
# safe — no separators / traversal — since it names a directory under REGISTRY.
_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,255}$")

# Cache of the packer's registry dirs so we only parse config.yaml once.
_REGISTRY_DIRS = None


def _load_packer_config() -> dict:
    """Read the same config.yaml the vendored packer/ggconf reads (NODO_DIR/
    config.yaml, i.e. the COPYed packer-config.yaml). Returns {} if it can't be
    read (e.g. on a dev box without the image layout / without PyYAML), so the
    caller can fall back to defaults."""
    path = os.path.join(NODO_DIR, "config.yaml")
    try:
        import yaml  # PyYAML is present in the image (ConfigManager needs it).
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _registry_dirs():
    """Resolve (REGISTRY, METADATA_REGISTRY, BLOCKDIR) — the dirs the vendored
    ggconf resolves a registry dependency against. These MUST match what the
    packer worker uses (config.yaml -> main.*), otherwise an injected dependency
    lands where the packer can't see it.

    Order: explicit PACKER_*_DIR env overrides (used by tests to point at temp
    dirs) -> config.yaml main.* -> the image's built-in defaults.
    """
    global _REGISTRY_DIRS
    if _REGISTRY_DIRS is not None:
        return _REGISTRY_DIRS
    cfg = _load_packer_config()
    main = cfg.get("main", {}) if isinstance(cfg, dict) else {}
    registry = (os.environ.get("PACKER_REGISTRY_DIR")
                or main.get("REGISTRY") or "/opt/nodo/storage/__registry__/")
    metadata = (os.environ.get("PACKER_METADATA_DIR")
                or main.get("METADATA_REGISTRY")
                or "/opt/nodo/storage/__metadata__/")
    blocks = (os.environ.get("PACKER_BLOCKS_DIR")
              or main.get("BLOCKDIR") or "/opt/nodo/storage/__block__/")
    _REGISTRY_DIRS = (registry, metadata, blocks)
    return _REGISTRY_DIRS


def _registry_status(service_id: str):
    """Return a dict describing whether <service_id> is present in the packer's
    registry (and, if so, whether metadata + which blocks are there), or None if
    the service dir isn't present at all."""
    registry, metadata, _blocks = _registry_dirs()
    service_dir = os.path.join(registry, service_id)
    if not os.path.isdir(service_dir):
        return None
    blocks = []
    manifest = os.path.join(service_dir, "_.json")
    if os.path.exists(manifest):
        try:
            with open(manifest) as f:
                for entry in json.load(f):
                    if isinstance(entry, list) and entry:
                        blocks.append(entry[0])
        except Exception:
            pass
    return {
        "present": True,
        "metadata": os.path.exists(os.path.join(metadata, service_id)),
        "blocks": blocks,
    }


def _safe_extract_tar(tf: tarfile.TarFile, dest: str):
    """Extract a tar guarding against path traversal (absolute paths, .., and
    symlinks/links escaping dest). Mirrors the zip handling used for /pack."""
    dest_abs = os.path.abspath(dest)
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            raise RuntimeError(f"unsafe link member in bundle: {member.name!r}")
        target = os.path.abspath(os.path.join(dest, member.name))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            raise RuntimeError(f"unsafe path in bundle: {member.name!r}")
    tf.extractall(dest)


def _store_registry_bundle(service_id: str, bundle_path: str) -> dict:
    """Inject an already-packed dependency into the packer host's registry so a
    later /pack can resolve `service_id` as a registry dependency.

    `bundle_path` is a gzip tar with this layout (all paths relative to root):
        service/          -> the packed service's multiblock dir; its contents
                             become {REGISTRY}/{service_id}/ (must contain _.json)
        metadata          -> optional; the Metadata protobuf, copied verbatim to
                             {METADATA_REGISTRY}/{service_id}
        blocks/<blockid>  -> optional; content-addressed shared block files, each
                             copied to {BLOCKDIR}/<blockid> if not already present

    Content-addressed and therefore idempotent: if the service dir already
    exists we treat it as already-present and only backfill any missing blocks.
    Returns {service_id, stored, already_present, blocks_added}.
    """
    registry, metadata, blockdir = _registry_dirs()
    for d in (registry, metadata, blockdir):
        os.makedirs(d, exist_ok=True)

    extract = tempfile.mkdtemp(prefix="reg-", dir=os.environ["CACHE"])
    try:
        with tarfile.open(bundle_path, "r:*") as tf:
            _safe_extract_tar(tf, extract)

        src_service = os.path.join(extract, "service")
        if not os.path.isdir(src_service):
            raise RuntimeError(
                "bundle is missing a top-level 'service/' directory (the packed "
                "service's multiblock dir).")
        if not os.path.exists(os.path.join(src_service, "_.json")):
            raise RuntimeError(
                "bundle 'service/' has no _.json manifest — not a packed service "
                "directory.")

        service_dir = os.path.join(registry, service_id)
        already_present = os.path.isdir(service_dir)
        if not already_present:
            # Atomic-ish publish: move the fully-extracted dir into place.
            os.replace(src_service, service_dir)

        # Metadata (a single file) — write if provided and not already there.
        src_meta = os.path.join(extract, "metadata")
        if os.path.isfile(src_meta):
            dst_meta = os.path.join(metadata, service_id)
            if not os.path.exists(dst_meta):
                shutil.move(src_meta, dst_meta)

        # Blocks are shared + content-addressed: only add the ones we don't have.
        blocks_added = 0
        src_blocks = os.path.join(extract, "blocks")
        if os.path.isdir(src_blocks):
            for name in os.listdir(src_blocks):
                if not _SERVICE_ID_RE.match(name):
                    continue  # ignore anything with a suspicious name
                dst_block = os.path.join(blockdir, name)
                if not os.path.exists(dst_block):
                    shutil.move(os.path.join(src_blocks, name), dst_block)
                    blocks_added += 1

        return {
            "service_id": service_id,
            "stored": True,
            "already_present": already_present,
            "blocks_added": blocks_added,
        }
    finally:
        shutil.rmtree(extract, ignore_errors=True)


def _has_config(project: str, name: str) -> bool:
    """A required config file (service.json / Dockerfile) may sit at the project
    root (Option 2) or under .service/ (Option 1, the recommended layout). The
    vendored nodo packer's prepare_directory honours both, so accept either."""
    return os.path.exists(os.path.join(project, name)) or os.path.exists(
        os.path.join(project, ".service", name)
    )


def _locate_config(project: str, name: str):
    """Return the path the packer will actually use for a config file. nodo's
    prepare_directory prefers .service/ when that dir exists, else the project
    root. Returns None if the file is in neither place."""
    svc = os.path.join(project, ".service", name)
    root = os.path.join(project, name)
    if os.path.isdir(os.path.join(project, ".service")) and os.path.exists(svc):
        return svc
    if os.path.exists(root):
        return root
    if os.path.exists(svc):
        return svc
    return None


def _load_json(path: str):
    """Return (obj, None) on success, or (None, "human-readable error") if the
    file isn't valid JSON. Reports the exact line/column of a syntax error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"{e.msg} (line {e.lineno}, column {e.colno})"
    except OSError as e:
        return None, str(e)


def _validate_service_config(project: str):
    """Pre-flight check of the .service configuration. Returns a list of
    plain-English problems ("what's wrong + how to fix"). An empty list means the
    config is structurally sound (the Docker build may still fail for other
    reasons, which are surfaced separately with the build log)."""
    problems = []

    sj_path = _locate_config(project, "service.json")
    if not sj_path:  # caller already guards this, but stay defensive
        return ["service.json is missing (expected at the project root or in a "
                ".service/ folder)."]

    sj, err = _load_json(sj_path)
    if err:
        return [f"service.json is not valid JSON — {err}. Common causes: a "
                "trailing comma, a missing comma, or unquoted keys/strings. Fix "
                "the syntax and re-pack."]
    if not isinstance(sj, dict):
        return [f"service.json must be a JSON object {{ ... }}, not a "
                f"{type(sj).__name__}."]

    # architecture — required. Builds amd64 natively and arm64 via qemu/binfmt.
    arch = sj.get("architecture")
    supported = ", ".join(sorted(SUPPORTED_ARCHES))
    if not arch:
        problems.append(
            f'"architecture" is required in service.json. Add e.g. '
            f'"architecture": "{PACKER_ARCH}" (one of: {supported}). '
            f'({GUIDE} → architecture)')
    elif not isinstance(arch, str):
        problems.append(f'"architecture" must be a string like "{PACKER_ARCH}", '
                        f'not {type(arch).__name__}.')
    elif arch not in SUPPORTED_ARCHES:
        problems.append(
            f'architecture "{arch}" can\'t be built by this packer. '
            f'Supported: {supported}.')

    # entrypoint — init.entry_path (preferred) or the legacy "entrypoint" field.
    init = sj.get("init")
    entry = init.get("entry_path") if isinstance(init, dict) else None
    if not entry and not sj.get("entrypoint"):
        problems.append(
            'no entrypoint declared — the service has nothing to run. Add '
            '"init": { "entry_path": ["app", "start.sh"] } pointing at the '
            f'executable/script inside your image. ({GUIDE} → init)')
    elif entry is not None and not isinstance(entry, (str, list)):
        problems.append('init.entry_path must be a string ("app/start.sh") or an '
                        'array of segments (["app", "start.sh"]).')

    # api slots — optional, but validate shape when present.
    api = sj.get("api")
    if api is not None:
        if not isinstance(api, list):
            problems.append('"api" must be an array of slot objects, e.g. '
                            '[ { "port": 8080, "protocol": ["http"] } ].')
        else:
            for i, slot in enumerate(api):
                if not isinstance(slot, dict):
                    problems.append(f'api[{i}] must be an object with at least a '
                                    '"port".')
                    continue
                port = slot.get("port")
                if not isinstance(port, int) or isinstance(port, bool):
                    problems.append(f'api[{i}].port must be an integer (got '
                                    f'{port!r}).')
                elif not 1 <= port <= 65535:
                    problems.append(f'api[{i}].port {port} is out of range '
                                    '(1–65535).')
                if "protocol" not in slot:
                    problems.append(f'api[{i}] has no "protocol" — add e.g. '
                                    '"protocol": ["http"] or ["grpc"].')

    # network — optional.
    if sj.get("network") is not None and not isinstance(sj["network"], list):
        problems.append('"network" must be an array of { "tags": [...], '
                        '"prose": "..." } entries.')

    # pack_config.json — optional; validate JSON + the dependencies_env/array trap.
    pc_path = _locate_config(project, "pack_config.json")
    if pc_path:
        pc, perr = _load_json(pc_path)
        if perr:
            problems.append(f"pack_config.json is not valid JSON — {perr}.")
        elif isinstance(pc, dict) and pc.get("dependencies_env") and isinstance(
            pc.get("dependencies"), list
        ):
            problems.append(
                '"dependencies_env": true requires "dependencies" to be an object '
                '(NAME → path), not an array, so each dependency gets an env-var '
                f'name. ({GUIDE} → dependencies_env)')

    return problems


def _flatten_single_root(extract_dir: str) -> str:
    """If the zip wrapped everything in one top-level folder, descend into it so
    the packer finds service.json/Dockerfile at `path`. Mirrors how `nodo pack`
    treats a project directory."""
    entries = [e for e in os.listdir(extract_dir) if not e.startswith("__MACOSX")]
    if len(entries) == 1:
        only = os.path.join(extract_dir, entries[0])
        if os.path.isdir(only) and _has_config(only, "service.json"):
            return only + "/"
    return extract_dir.rstrip("/") + "/"


def _run_packer(zip_path: str):
    """Invoke the real nodo packer worker out-of-process (exactly as nodo's
    pack_zip does) and return (service_id, metadata_bytes, service_dir) or raise.

    The worker writes a JSON result file: {service_id, metadata_b64, service_dir}
    or {error}.
    """
    result_path = os.path.join(
        os.environ["CACHE"], f"pack_result_{uuid.uuid4().hex}.json"
    )
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    cmd = [
        sys.executable, "-m", "src.packers.zip_with_dockerfile",
        "--worker", zip_path, result_path,
    ]
    proc = subprocess.run(cmd, cwd=NODO_DIR)
    if proc.returncode != 0:
        raise RuntimeError(f"packer worker exited {proc.returncode}")
    if not os.path.exists(result_path):
        raise RuntimeError("packer worker produced no result file")
    with open(result_path) as f:
        result = json.load(f)
    os.remove(result_path)
    if result.get("error"):
        raise RuntimeError(result["error"])
    metadata_b64 = result.get("metadata_b64")
    return (
        result["service_id"],
        base64.b64decode(metadata_b64) if metadata_b64 else b"",
        result["service_dir"],
    )


def _serialize_bee(service_dir: str) -> bytes:
    """Serialise the packer's multiblock directory into a single `.celaut.bee`
    byte stream in the framing `nodo import` expects: a sequence of
    buffer_pb2.Buffer messages, each preceded by its length as a 4-byte
    big-endian integer (see bee_rpc.client write_to_file / reader.read_bee_file).

    read_from_registry walks the multiblock dir and yields the Buffer objects
    (chunk= for inline bytes, block= for block pointers); we just length-prefix
    each. Concatenating raw multiblock chunks (the previous approach) is NOT a
    valid .bee and fails import with "Incomplete message data"."""
    # Imported lazily so the module is importable without the nodo deps present
    # (e.g. for unit-testing the HTTP layer on a dev box).
    sys.path.insert(0, NODO_DIR)
    from bee_rpc import client as grpcbb  # type: ignore
    buf = bytearray()
    for b in grpcbb.read_from_registry(filename=service_dir):
        data = b.SerializeToString()
        buf.extend(len(data).to_bytes(4, "big"))
        buf.extend(data)
    return bytes(buf)


class Handler(BaseHTTPRequestHandler):
    server_version = "celaut-packer/0.1"

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter, single-line logs to stderr
        sys.stderr.write("[packer] " + (fmt % args) + "\n")

    def _registry_id_from_path(self):
        """Parse a validated <id> out of /registry/<id>, or None (and send the
        appropriate error) if the path/id is malformed."""
        rest = self.path.split("?", 1)[0].rstrip("/")
        prefix = "/registry/"
        if not rest.startswith(prefix):
            self._send(404, "not found")
            return None
        service_id = rest[len(prefix):]
        if not service_id or "/" in service_id or not _SERVICE_ID_RE.match(service_id):
            self._send(400, "invalid service id in /registry/<id>")
            return None
        return service_id

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz", ""):
            return self._send(200, "ok")
        if self.path.split("?", 1)[0].rstrip("/").startswith("/registry/"):
            service_id = self._registry_id_from_path()
            if service_id is None:
                return
            status = _registry_status(service_id)
            if status is None:
                return self._send(
                    404,
                    json.dumps({"service_id": service_id, "present": False}),
                    ctype="application/json")
            return self._send(
                200,
                json.dumps({"service_id": service_id, **status}),
                ctype="application/json")
        return self._send(404, "not found")

    def do_POST(self):
        if self.path.split("?", 1)[0].rstrip("/").startswith("/registry/"):
            return self._handle_registry_upload()
        if self.path.rstrip("/") != "/pack":
            return self._send(404, "not found")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(415, "empty body; POST a project .zip")
        if length > MAX_ZIP_BYTES:
            return self._send(413, f"zip too large (> {MAX_ZIP_BYTES} bytes)")

        work = tempfile.mkdtemp(prefix="pack-", dir=os.environ["CACHE"])
        zip_path = os.path.join(work, "input.zip")
        try:
            remaining = length
            with open(zip_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            if not zipfile.is_zipfile(zip_path):
                return self._send(415, "body is not a valid zip archive")

            extract_dir = os.path.join(work, "src")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
            project = _flatten_single_root(extract_dir)
            if not _has_config(project, "service.json"):
                return self._send(400,
                    "No service.json found. Every service needs a service.json "
                    "(at the project root or in a .service/ folder) declaring its "
                    f"architecture, entrypoint, ports and network. See {GUIDE}.")
            if not _has_config(project, "Dockerfile"):
                return self._send(400,
                    "No Dockerfile found. The packer builds your service's "
                    "filesystem from a Dockerfile (at the project root or in a "
                    f".service/ folder). See {GUIDE} → Dockerfile.")

            # Pre-flight: explain any .service config mistakes before building, so
            # users get an actionable message instead of a cryptic build failure.
            problems = _validate_service_config(project)
            if problems:
                body = (
                    "Your .service configuration has "
                    f"{len(problems)} problem{'s' if len(problems) != 1 else ''}:\n\n"
                    + "\n".join(f"  {i}. {p}" for i, p in enumerate(problems, 1))
                    + f"\n\nFix and re-pack. Full field reference: {GUIDE}."
                )
                return self._send(400, body)

            # Re-zip the (possibly flattened) project for the worker, which unzips
            # to its own cache. Mirrors nodo's zipfile_ok contract.
            norm_zip = os.path.join(work, "project.zip")
            with zipfile.ZipFile(norm_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(project):
                    for name in files:
                        ap = os.path.join(root, name)
                        zf.write(ap, os.path.relpath(ap, project))

            service_id, _meta, service_dir = _run_packer(norm_zip)
            bee = _serialize_bee(service_dir)

            fname = f"{service_id}.celaut.bee"
            return self._send(
                200, bee,
                ctype="application/octet-stream",
                headers={
                    "X-Service-Id": service_id,
                    "Content-Disposition": f'attachment; filename="{fname}"',
                },
            )
        except RuntimeError as e:
            return self._send(400,
                "Packing failed while building your service:\n\n"
                f"{e}\n\n"
                "This is usually a Docker build error, not a config-shape problem. "
                "Common causes: a COPY referencing a file that isn't in the build "
                "context, a base image that can't be pulled, or a RUN step that "
                f"exits non-zero. See {GUIDE} → Common Issues.")
        except Exception as e:  # noqa: BLE001 - surface unexpected failures
            return self._send(500, f"internal error: {e}")
        finally:
            subprocess.run(["rm", "-rf", work], check=False)

    def _handle_registry_upload(self):
        """POST /registry/<id> — inject a packed dependency into the registry."""
        service_id = self._registry_id_from_path()
        if service_id is None:
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(415, "empty body; POST a gzip tar dependency bundle")
        if length > MAX_ZIP_BYTES:
            return self._send(413, f"bundle too large (> {MAX_ZIP_BYTES} bytes)")

        os.makedirs(os.environ["CACHE"], exist_ok=True)
        work = tempfile.mkdtemp(prefix="reg-up-", dir=os.environ["CACHE"])
        bundle_path = os.path.join(work, "bundle.tar.gz")
        try:
            remaining = length
            with open(bundle_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            if not tarfile.is_tarfile(bundle_path):
                return self._send(415, "body is not a valid tar archive")

            result = _store_registry_bundle(service_id, bundle_path)
            return self._send(200, json.dumps(result), ctype="application/json")
        except RuntimeError as e:
            return self._send(400, f"invalid dependency bundle: {e}")
        except Exception as e:  # noqa: BLE001
            return self._send(500, f"internal error: {e}")
        finally:
            shutil.rmtree(work, ignore_errors=True)


def main():
    os.makedirs(os.environ["CACHE"], exist_ok=True)
    os.makedirs(os.environ["BLOCKDIR"], exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[packer] listening on 0.0.0.0:{PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
