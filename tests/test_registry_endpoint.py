#!/usr/bin/env python3
"""Unit + light-integration tests for the /registry dependency-injection
endpoint added to server.py.

These DO NOT pack anything (no Docker/KVM needed). They exercise the pure
store/lookup logic and the HTTP surface against temp registry dirs, so they run
anywhere Python does.

Run:  python -m unittest tests.test_registry_endpoint   (from the repo root)
"""
import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# Point the packer's registry dirs at throwaway temp dirs BEFORE importing the
# server, so the module-level env setdefaults / registry resolution use them.
_TMP = tempfile.mkdtemp(prefix="packer-test-")
os.environ["CACHE"] = os.path.join(_TMP, "cache")
os.environ["PACKER_REGISTRY_DIR"] = os.path.join(_TMP, "registry")
os.environ["PACKER_METADATA_DIR"] = os.path.join(_TMP, "metadata")
os.environ["PACKER_BLOCKS_DIR"] = os.path.join(_TMP, "blocks")
os.makedirs(os.environ["CACHE"], exist_ok=True)

import server  # noqa: E402


def _build_bundle(block_ids=("blockAAA",), with_metadata=True) -> bytes:
    """Build an in-memory gzip-tar dependency bundle in the layout
    _store_registry_bundle expects."""
    manifest = [[b] for b in block_ids] + ["inline_chunk_0"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, data: bytes):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        add("service/_.json", json.dumps(manifest).encode())
        add("service/inline_chunk_0", b"chunk-bytes")
        if with_metadata:
            add("metadata", b"fake-metadata-protobuf")
        for b in block_ids:
            add(f"blocks/{b}", f"block-bytes-{b}".encode())
    return buf.getvalue()


class StoreLookupUnitTests(unittest.TestCase):
    """Directly exercise the pure store/lookup helpers."""

    def setUp(self):
        # Fresh registry per test.
        for key in ("PACKER_REGISTRY_DIR", "PACKER_METADATA_DIR", "PACKER_BLOCKS_DIR"):
            d = tempfile.mkdtemp(prefix="reg-", dir=_TMP)
            os.environ[key] = d
        server._REGISTRY_DIRS = None  # bust the cache so the new dirs take effect

    def _write_bundle(self, data: bytes) -> str:
        path = os.path.join(os.environ["CACHE"], "b.tar.gz")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_missing_returns_none(self):
        self.assertIsNone(server._registry_status("deadbeef"))

    def test_store_then_status(self):
        path = self._write_bundle(_build_bundle(block_ids=("blk1", "blk2")))
        res = server._store_registry_bundle("svc123", path)
        self.assertTrue(res["stored"])
        self.assertFalse(res["already_present"])
        self.assertEqual(res["blocks_added"], 2)

        registry, metadata, blockdir = server._registry_dirs()
        # ggconf reads exactly these paths:
        self.assertTrue(os.path.exists(os.path.join(registry, "svc123", "_.json")))
        self.assertTrue(os.path.exists(os.path.join(metadata, "svc123")))
        self.assertTrue(os.path.exists(os.path.join(blockdir, "blk1")))
        self.assertTrue(os.path.exists(os.path.join(blockdir, "blk2")))

        status = server._registry_status("svc123")
        self.assertTrue(status["present"])
        self.assertTrue(status["metadata"])
        self.assertCountEqual(status["blocks"], ["blk1", "blk2"])

    def test_idempotent_second_store(self):
        path = self._write_bundle(_build_bundle(block_ids=("blkX",)))
        server._store_registry_bundle("svcABC", path)
        res2 = server._store_registry_bundle("svcABC", path)
        self.assertTrue(res2["already_present"])
        self.assertEqual(res2["blocks_added"], 0)

    def test_rejects_bundle_without_service_dir(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("metadata")
            data = b"x"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        path = self._write_bundle(buf.getvalue())
        with self.assertRaises(RuntimeError):
            server._store_registry_bundle("svcNoDir", path)

    def test_rejects_path_traversal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("../escape.txt")
            data = b"x"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        path = self._write_bundle(buf.getvalue())
        with self.assertRaises(RuntimeError):
            server._store_registry_bundle("svcEscape", path)


class HttpEndpointTests(unittest.TestCase):
    """Drive the real HTTP handler over a loopback socket."""

    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp(prefix="http-", dir=_TMP)
        os.environ["PACKER_REGISTRY_DIR"] = os.path.join(d, "registry")
        os.environ["PACKER_METADATA_DIR"] = os.path.join(d, "metadata")
        os.environ["PACKER_BLOCKS_DIR"] = os.path.join(d, "blocks")
        server._REGISTRY_DIRS = None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path)) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post(self, path, body, ctype="application/octet-stream"):
        req = urllib.request.Request(
            self._url(path), data=body, method="POST",
            headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_full_flow(self):
        sid = "httpsvc0001"
        # Not present yet.
        code, body = self._get(f"/registry/{sid}")
        self.assertEqual(code, 404)
        self.assertFalse(json.loads(body)["present"])

        # Upload.
        code, body = self._post(f"/registry/{sid}", _build_bundle(("hblk",)))
        self.assertEqual(code, 200)
        res = json.loads(body)
        self.assertTrue(res["stored"])
        self.assertFalse(res["already_present"])
        self.assertEqual(res["blocks_added"], 1)

        # Now present.
        code, body = self._get(f"/registry/{sid}")
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertTrue(status["present"])
        self.assertTrue(status["metadata"])
        self.assertIn("hblk", status["blocks"])

        # Idempotent re-upload.
        code, body = self._post(f"/registry/{sid}", _build_bundle(("hblk",)))
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["already_present"])

    def test_invalid_id(self):
        code, _ = self._get("/registry/bad%2Fid")
        self.assertIn(code, (400, 404))
        code, _ = self._post("/registry/..", _build_bundle())
        self.assertEqual(code, 400)

    def test_non_tar_body(self):
        code, _ = self._post("/registry/notar01", b"this is not a tar")
        self.assertEqual(code, 415)

    def test_health_still_ok(self):
        code, body = self._get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"ok")


if __name__ == "__main__":
    unittest.main()
