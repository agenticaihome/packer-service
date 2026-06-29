#!/usr/bin/env python3
"""
Celaut Packer Service — HTTP front-end.

A network-facing Celaut microVM service that does *the same thing the nodo
packer does* (src/packers/zip_with_dockerfile.py): given a project archive
containing a Dockerfile, a packer config and a service.json, it builds the
image, exports its filesystem, serialises a content-addressed `celaut.Service`
protobuf, and returns the packed service as a single `.celaut.bee` file.

It does NOT re-implement the packer. It vendors the real nodo packer in-repo
(vendor/nodo/, COPYed to /opt/nodo at image-build time — no clone of nodo) and
invokes its worker entrypoint, so the service-id produced here is byte-identical
to `nodo pack`. Docker/buildx runs
inside this microVM (docker-in-docker); that is why the service declares
`network: tag=(*)` — buildx must reach arbitrary registries to pull base images.

API (bound on 0.0.0.0:8080):
  GET  /health            -> 200 "ok"
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
import subprocess
import sys
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

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz", ""):
            return self._send(200, "ok")
        return self._send(404, "not found")

    def do_POST(self):
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


def main():
    os.makedirs(os.environ["CACHE"], exist_ok=True)
    os.makedirs(os.environ["BLOCKDIR"], exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[packer] listening on 0.0.0.0:{PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
