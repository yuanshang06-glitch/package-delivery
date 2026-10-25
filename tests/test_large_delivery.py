"""Focused fixture tests for the package-delivery 1.2.0 large/ZIP64 extension.

Run::

    python tests/test_large_delivery.py \
        --tool src/delivery.py \
        --checker src/check_handoff.py \
        --base-tests inputs/test_delivery.py \
        --release-tests inputs/test_release.py

The module imports the two scripts under test in-process, builds every fixture
inside ``tempfile`` directories (at most a few MiB), and runs the original 52/31
test suites unchanged in-process as an explicit compatibility check.  It never
touches a real package, the network, dependencies, or another project.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.dont_write_bytecode = True

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.abspath(os.path.join(_HERE, ".."))

_DEFAULT_TOOL = os.path.join(_WORKSPACE, "src", "delivery.py")
_DEFAULT_CHECKER = os.path.join(_WORKSPACE, "src", "check_handoff.py")
_DEFAULT_BASE_TESTS = os.path.join(_WORKSPACE, "inputs", "test_delivery.py")
_DEFAULT_RELEASE_TESTS = os.path.join(_WORKSPACE, "inputs", "test_release.py")

_TOOL = None
_CHECKER = None
_BASE_TESTS = _DEFAULT_BASE_TESTS
_RELEASE_TESTS = _DEFAULT_RELEASE_TESTS
_INSTALLER = None

_EOCD_SIG = b"PK\x05\x06"
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"


class _ReadSpy:
    """File proxy that records every read size the delivery reader requests."""

    def __init__(self, inner, sizes):
        self._inner = inner
        self._sizes = sizes

    def read(self, size=-1):
        self._sizes.append(size)
        return self._inner.read(size)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)


def load_module(path, name):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise RuntimeError("module not found: %s" % path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import: %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(root):
    result = {}
    for base, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                data = handle.read()
            result[os.path.relpath(path, root).replace(os.sep, "/")] = (
                len(data),
                sha256_bytes(data),
            )
    return result


def ensure_modules():
    """Load the default scripts when a test runner did not go through main()."""
    global _TOOL, _CHECKER
    if _TOOL is None:
        _TOOL = load_module(_DEFAULT_TOOL, "large_delivery_tool")
    if _CHECKER is None:
        _CHECKER = load_module(_DEFAULT_CHECKER, "large_delivery_checker")


class LargeDeliveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ensure_modules()
        self.d = _TOOL
        self.checker = _CHECKER
        self.root = os.path.join(self.tmp.name, "staging")
        os.makedirs(self.root)
        self.out = os.path.join(self.tmp.name, "delivery.zip")

    # -- fixtures -----------------------------------------------------------

    def path(self, relative, root=None):
        return os.path.join(root or self.root, *relative.split("/"))

    def write(self, relative, data, root=None):
        path = self.path(relative, root)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def default_meta(self, payload_digest=None, **overrides):
        meta = {
            "schema_version": "collaborator-delivery/1",
            "task_id": "large-delivery-011",
            "attempt_id": "large-delivery-011-a2",
            "title": "large fixture",
            "summary": "synthetic delivery for the large-file extension",
            "status": "completed",
            "independent_acceptance": "UNKNOWN",
            "files": ["START-HERE.md", "payload.bin"],
            "checks": [],
            "unfinished": [],
            "requirements": ["Python 3.11+"],
            "next_steps": ["read START-HERE.md"],
        }
        if payload_digest is not None:
            meta["traceability"] = {
                "schema_version": "collaborator-traceability/1",
                "criteria": [
                    {
                        "id": "AC1",
                        "requirement": "payload is traceable",
                        "status": "met",
                        "artifacts": ["payload.bin"],
                        "verification_ids": ["V1"],
                        "reason": None,
                    }
                ],
                "verifications": [
                    {
                        "id": "V1",
                        "kind": "unit",
                        "execution": "current",
                        "status": "passed",
                        "command": ["python", "-m", "unittest"],
                        "cwd": ".",
                        "environment": {"os": "fixture"},
                        "exit_code": 0,
                        "evidence": "START-HERE.md",
                        "subjects": [{"path": "payload.bin", "sha256": payload_digest}],
                        "reason": None,
                        "source": None,
                    }
                ],
                "revision": None,
            }
        meta.update(overrides)
        return meta

    def make_staging(self, payload_size=4096, payload_name="payload.bin", traceable=False):
        payload = bytes((index * 37 + 11) & 0xFF for index in range(payload_size))
        self.write("START-HERE.md", "# start\n\nread me\n")
        self.write(payload_name, payload)
        meta = self.default_meta(
            sha256_bytes(payload) if traceable else None,
            files=["START-HERE.md", payload_name],
        )
        self.write("DELIVERY.json", json.dumps(meta, ensure_ascii=False))
        return payload

    def expect_error(self, code, func, *args, **kwargs):
        with self.assertRaises(self.d.DeliveryError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    # -- ZIP manipulation ---------------------------------------------------

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def write_bytes(self, path, data):
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def find_eocd(self, data):
        position = data.rfind(_EOCD_SIG)
        self.assertGreaterEqual(position, 0)
        return position

    def assert_preflight_rejection(self, archive, expected_codes, limits=None, profile="standard"):
        """The central-directory preflight must reject before ZipFile exists."""

        def fail(*args, **kwargs):
            raise AssertionError("zipfile.ZipFile was constructed before preflight")

        with mock.patch.object(zipfile, "ZipFile", fail):
            with self.assertRaises(self.d.DeliveryError) as caught:
                self.d.verify_delivery(archive, limits=limits, profile=profile)
        self.assertIn(caught.exception.code, expected_codes)
        return caught.exception


class ProfileAndLimitTests(LargeDeliveryBase):
    def test_default_and_large_limits_are_exact_and_immutable(self):
        self.assertEqual(
            dict(self.d.DEFAULT_LIMITS),
            {
                "max_entries": 512,
                "max_file_bytes": 64 * 1024 * 1024,
                "max_total_bytes": 128 * 1024 * 1024,
                "max_metadata_bytes": 64 * 1024,
                "max_manifest_bytes": 512 * 1024,
            },
        )
        self.assertEqual(
            dict(self.d.LARGE_LIMITS),
            {
                "max_entries": 512,
                "max_file_bytes": 8 * 1024 * 1024 * 1024,
                "max_total_bytes": 16 * 1024 * 1024 * 1024,
                "max_metadata_bytes": 64 * 1024,
                "max_manifest_bytes": 512 * 1024,
            },
        )
        for mapping in (self.d.DEFAULT_LIMITS, self.d.LARGE_LIMITS):
            with self.assertRaises(TypeError):
                mapping["max_file_bytes"] = 1

    def test_get_limits_rejects_bad_profiles_and_widening(self):
        self.assertEqual(self.d.get_limits(), dict(self.d.DEFAULT_LIMITS))
        self.assertEqual(self.d.get_limits("large"), dict(self.d.LARGE_LIMITS))
        self.assertEqual(
            self.d.get_limits("large", {"max_file_bytes": 4096})["max_file_bytes"], 4096
        )

        for bad_profile in (None, "", "Standard", "large ", "huge", 1, True, object()):
            with self.subTest(profile=bad_profile):
                self.expect_error("INVALID_PROFILE", self.d.get_limits, bad_profile)

        bad_limits = [
            True,
            -1,
            "16",
            1.5,
            {"max_entries": True},
            {"max_file_bytes": -1},
            {"max_file_bytes": "16"},
            {"max_total_bytes": 1.5},
            {"unknown_limit": 1},
            {"max_file_bytes": self.d.DEFAULT_LIMITS["max_file_bytes"] + 1},
            {"max_file_bytes": 8 * 1024 * 1024 * 1024},
        ]
        for bad in bad_limits:
            with self.subTest(limits=bad):
                self.expect_error("INVALID_LIMITS", self.d.get_limits, "standard", bad)

        self.expect_error(
            "INVALID_LIMITS",
            self.d.get_limits,
            "large",
            {"max_file_bytes": self.d.LARGE_LIMITS["max_file_bytes"] + 1},
        )
        self.expect_error(
            "INVALID_LIMITS",
            self.d.get_limits,
            "large",
            {"max_metadata_bytes": self.d.LARGE_LIMITS["max_metadata_bytes"] + 1},
        )

    def test_api_rejects_bad_profile_and_compression_with_fixed_codes(self):
        self.make_staging()
        self.expect_error(
            "INVALID_PROFILE",
            self.d.pack_delivery,
            self.root,
            self.out,
            profile="huge",
        )
        self.expect_error(
            "INVALID_COMPRESSION",
            self.d.pack_delivery,
            self.root,
            self.out,
            compression="bzip2",
        )
        self.assertFalse(os.path.exists(self.out))

        self.d.pack_delivery(self.root, self.out)
        self.expect_error("INVALID_PROFILE", self.d.verify_delivery, self.out, profile=None)
        self.expect_error(
            "INVALID_PROFILE", self.d.verify_delivery, self.out, profile="Large"
        )

    def test_cli_bad_profile_and_compression_are_code_only_json(self):
        self.make_staging()
        for arguments, expected in (
            (["pack", "--root", self.root, "--out", self.out, "--profile", "huge"], "INVALID_PROFILE"),
            (
                ["pack", "--root", self.root, "--out", self.out, "--compression", "bzip2"],
                "INVALID_COMPRESSION",
            ),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = self.d.main(arguments)
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(json.loads(stderr.getvalue()), {"error": expected})
            self.assertNotIn(self.root, stderr.getvalue())
            self.assertNotIn("payload", stderr.getvalue())
            self.assertFalse(os.path.exists(self.out))

        self.d.pack_delivery(self.root, self.out)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(["verify", self.out, "--profile", "bogus"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "INVALID_PROFILE"})

    def test_receiver_does_not_infer_large_profile(self):
        self.make_staging(payload_size=4096)
        self.d.pack_delivery(self.root, self.out, profile="large", compression="stored")

        # Simulate a receiver whose standard cap is far below the package, as
        # a real standard receiver would be below an 8 GiB payload.  The
        # default must stay standard; only an explicit large profile passes.
        with mock.patch.dict(
            self.d._FIXED_LIMITS,
            {"max_file_bytes": 1024, "max_total_bytes": 1024 * 1024},
        ):
            self.expect_error("FILE_LIMIT", self.d.verify_delivery, self.out)
            stats = self.d.verify_delivery(self.out, profile="large")
        self.assertEqual(stats["files"], 4)

    def test_raw_archive_cap_accounts_for_encoding_overhead(self):
        large = self.d.get_limits("large")
        cap = self.d._archive_raw_cap(large)
        total = large["max_total_bytes"]
        self.assertEqual(cap, total + (total + 999) // 1000 + 8 * 1024 * 1024)
        self.assertGreater(cap, total + 8 * 1024 * 1024)


class RoundTripTests(LargeDeliveryBase):
    def test_stored_large_roundtrip_preserves_source_and_digests(self):
        self.make_staging(payload_size=4096)
        before = snapshot(self.root)

        stats = self.d.pack_delivery(
            self.root, self.out, profile="large", compression="stored"
        )

        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(set(stats), {"archive_sha256", "files", "total_bytes"})
        self.assertEqual(stats["archive_sha256"], file_digest(self.out))
        self.assertEqual(self.d.verify_delivery(self.out, profile="large"), stats)

        with zipfile.ZipFile(self.out) as archive:
            self.assertTrue(
                all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
            )
            manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
            for entry in manifest["files"]:
                payload = archive.read(entry["path"])
                self.assertEqual(entry["bytes"], len(payload))
                self.assertEqual(entry["sha256"], sha256_bytes(payload))
            self.assertEqual(
                stats["total_bytes"], sum(info.file_size for info in archive.infolist())
            )

        with self.d.open_bounded_archive(self.out, profile="large") as archive:
            self.assertEqual(len(archive.infolist()), stats["files"])

    def test_deflated_large_roundtrip_and_checker_cli(self):
        self.make_staging(traceable=True)
        stats = self.d.pack_delivery(self.root, self.out, profile="large")

        with zipfile.ZipFile(self.out) as archive:
            payload_info = archive.getinfo("payload.bin")
            self.assertEqual(payload_info.compress_type, zipfile.ZIP_DEFLATED)

        result = self.checker.check_handoff(self.out, profile="large")
        self.assertEqual(
            set(result),
            {
                "archive_sha256",
                "criteria_count",
                "verification_count",
                "independent_acceptance",
            },
        )
        self.assertEqual(result["archive_sha256"], stats["archive_sha256"])
        self.assertEqual(result["independent_acceptance"], "UNKNOWN")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.checker.main([self.out, "--profile", "large"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), result)

    def test_checker_forwards_the_explicit_profile(self):
        self.make_staging(traceable=True)
        self.d.pack_delivery(self.root, self.out, profile="large")
        self.checker._DELIVERY_HELPER = self.d

        with mock.patch.object(
            self.d, "verify_delivery", wraps=self.d.verify_delivery
        ) as spy:
            result = self.checker.check_handoff(self.out, profile="large")
        self.assertEqual(spy.call_args.kwargs.get("profile"), "large")
        self.assertEqual(result["verification_count"], 1)

        self.checker._DELIVERY_HELPER = self.d
        with mock.patch.object(
            self.d, "verify_delivery", wraps=self.d.verify_delivery
        ) as spy:
            self.checker.check_handoff(self.out)
        self.assertEqual(spy.call_args.kwargs.get("profile"), "standard")

        self.checker._DELIVERY_HELPER = self.d
        with self.assertRaises(self.checker.HandoffError) as caught:
            self.checker.check_handoff(self.out, profile="large-everything")
        self.assertEqual(caught.exception.code, "INVALID_PROFILE")

    def test_limits_only_tighten_and_standard_receiver_rejects_big_member(self):
        self.make_staging(payload_size=4096)
        self.d.pack_delivery(
            self.root,
            self.out,
            profile="large",
            limits={"max_file_bytes": 8192},
            compression="stored",
        )
        self.d.verify_delivery(
            self.out, profile="large", limits={"max_file_bytes": 8192}
        )
        self.expect_error(
            "FILE_LIMIT",
            self.d.verify_delivery,
            self.out,
            limits={"max_file_bytes": 1024},
        )

    def test_cli_large_pack_verify_roundtrip(self):
        self.make_staging(payload_size=4096)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(
                [
                    "pack",
                    "--root",
                    self.root,
                    "--out",
                    self.out,
                    "--profile",
                    "large",
                    "--compression",
                    "stored",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        pack_stats = json.loads(stdout.getvalue())
        self.assertEqual(set(pack_stats), {"archive_sha256", "files", "total_bytes"})

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(["verify", self.out, "--profile", "large"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), pack_stats)


class Zip64Tests(LargeDeliveryBase):
    def test_zip64_member_and_eocd_roundtrip_with_lowered_threshold(self):
        self.make_staging(payload_size=4096)
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 1024):
            stats = self.d.pack_delivery(
                self.root, self.out, profile="large", compression="stored"
            )

        raw = self.read_bytes(self.out)
        self.assertIn(_ZIP64_EOCD_SIG, raw)
        self.assertIn(_ZIP64_LOCATOR_SIG, raw)

        with self.d.open_bounded_archive(self.out, profile="large") as archive:
            info = archive.getinfo("payload.bin")
            self.assertGreaterEqual(info.extract_version, 45)
            offset = info.header_offset
            local = raw[offset : offset + 30]
            self.assertEqual(local[:4], b"PK\x03\x04")
            name_length = struct.unpack_from("<H", local, 26)[0]
            extra_length = struct.unpack_from("<H", local, 28)[0]
            extra = raw[offset + 30 + name_length : offset + 30 + name_length + extra_length]
            self.assertGreaterEqual(extra_length, 20)
            self.assertEqual(struct.unpack_from("<HH", extra)[0], 1)

        self.assertEqual(self.d.verify_delivery(self.out, profile="large"), stats)
        # A small ZIP64 archive is still readable under the standard caps.
        self.assertEqual(self.d.verify_delivery(self.out), stats)

    def test_standard_pack_never_writes_zip64_and_rejects_oversize(self):
        self.make_staging(payload_size=4096)
        self.d.pack_delivery(self.root, self.out, compression="stored")
        raw = self.read_bytes(self.out)
        self.assertNotIn(_ZIP64_EOCD_SIG, raw)
        self.assertNotIn(_ZIP64_LOCATOR_SIG, raw)

        oversized = os.path.join(self.tmp.name, "oversized.zip")
        self.expect_error(
            "FILE_LIMIT",
            self.d.pack_delivery,
            self.root,
            oversized,
            profile="standard",
            limits={"max_file_bytes": 1024},
        )
        self.assertFalse(os.path.exists(oversized))

        # Standard also refuses to emit ZIP64 when a lowered threshold would
        # otherwise force it; the failure is still a fixed DeliveryError.
        zip64_needed = os.path.join(self.tmp.name, "zip64-needed.zip")
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 1024):
            self.expect_error(
                "FILE_LIMIT",
                self.d.pack_delivery,
                self.root,
                zip64_needed,
                profile="standard",
                compression="stored",
            )
        self.assertFalse(os.path.exists(zip64_needed))

    def test_forged_classic_eocd_is_rejected_before_zipfile(self):
        self.make_staging()
        self.d.pack_delivery(self.root, self.out)
        original = bytearray(self.read_bytes(self.out))
        eocd = self.find_eocd(original)

        count_forged = bytearray(original)
        struct.pack_into("<H", count_forged, eocd + 8, 9999)
        struct.pack_into("<H", count_forged, eocd + 10, 9999)
        count_path = self.write_bytes(
            os.path.join(self.tmp.name, "forged-count.zip"), count_forged
        )
        self.assert_preflight_rejection(count_path, {"ENTRY_LIMIT"})

        size_forged = bytearray(original)
        struct.pack_into("<I", size_forged, eocd + 12, 5 * 1024 * 1024)
        size_path = self.write_bytes(
            os.path.join(self.tmp.name, "forged-size.zip"), size_forged
        )
        self.assert_preflight_rejection(size_path, {"ARCHIVE_TOO_LARGE"})

    def test_forged_zip64_eocd_is_rejected_before_zipfile(self):
        self.make_staging(payload_size=4096)
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 1024):
            self.d.pack_delivery(
                self.root, self.out, profile="large", compression="stored"
            )
        original = bytearray(self.read_bytes(self.out))
        zip64 = original.find(_ZIP64_EOCD_SIG)
        eocd = self.find_eocd(original)
        self.assertGreaterEqual(zip64, 0)

        count_forged = bytearray(original)
        # Keep the classic EOCD aggregate consistent so the ZIP64 count is the
        # value the preflight rejects.
        struct.pack_into("<H", count_forged, eocd + 8, 9999)
        struct.pack_into("<H", count_forged, eocd + 10, 9999)
        struct.pack_into("<Q", count_forged, zip64 + 24, 9999)
        struct.pack_into("<Q", count_forged, zip64 + 32, 9999)
        count_path = self.write_bytes(
            os.path.join(self.tmp.name, "forged-zip64-count.zip"), count_forged
        )
        self.assert_preflight_rejection(count_path, {"ENTRY_LIMIT"})

        size_forged = bytearray(original)
        struct.pack_into("<I", size_forged, eocd + 12, 5 * 1024 * 1024)
        struct.pack_into("<Q", size_forged, zip64 + 40, 5 * 1024 * 1024)
        size_path = self.write_bytes(
            os.path.join(self.tmp.name, "forged-zip64-size.zip"), size_forged
        )
        self.assert_preflight_rejection(size_path, {"ARCHIVE_TOO_LARGE"})

    def test_multidisk_records_are_rejected_before_zipfile(self):
        self.make_staging()
        self.d.pack_delivery(self.root, self.out)
        original = bytearray(self.read_bytes(self.out))
        eocd = self.find_eocd(original)
        struct.pack_into("<H", original, eocd + 4, 1)
        path = self.write_bytes(os.path.join(self.tmp.name, "multidisk.zip"), original)
        self.assert_preflight_rejection(path, {"ARCHIVE_INVALID"})

    def test_large_does_not_relax_metadata_or_manifest_caps(self):
        self.make_staging()
        self.assertEqual(
            self.d.get_limits("large")["max_metadata_bytes"],
            self.d.DEFAULT_LIMITS["max_metadata_bytes"],
        )
        self.assertEqual(
            self.d.get_limits("large")["max_manifest_bytes"],
            self.d.DEFAULT_LIMITS["max_manifest_bytes"],
        )
        self.d.pack_delivery(self.root, self.out, profile="large")
        self.expect_error(
            "METADATA_LIMIT",
            self.d.verify_delivery,
            self.out,
            profile="large",
            limits={"max_metadata_bytes": 10},
        )
        other = os.path.join(self.tmp.name, "metadata-limited.zip")
        self.expect_error(
            "METADATA_LIMIT",
            self.d.pack_delivery,
            self.root,
            other,
            profile="large",
            limits={"max_metadata_bytes": 10},
        )
        self.assertFalse(os.path.exists(other))


class BoundedReadAndCleanupTests(LargeDeliveryBase):
    def test_verify_uses_fixed_sized_reads(self):
        self.make_staging(payload_size=200000)
        self.d.pack_delivery(self.root, self.out, profile="large")

        requested = []
        original_read = zipfile.ZipExtFile.read

        def spy(instance, size=-1):
            requested.append(size)
            return original_read(instance, size)

        with mock.patch.object(zipfile.ZipExtFile, "read", spy):
            self.d.verify_delivery(self.out, profile="large")

        self.assertTrue(requested)
        self.assertTrue(all(size == self.d._CHUNK for size in requested))
        self.assertGreater(len(requested), 4)

    def test_real_read_loop_enforces_the_file_cap(self):
        self.make_staging(payload_size=50)
        self.d.pack_delivery(self.root, self.out, profile="large", compression="stored")

        original_open = zipfile.ZipFile.open

        class ExtraReader:
            def __init__(self, inner):
                self.inner = inner
                self.sent = False

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def read(self, size=-1):
                if not self.sent:
                    self.sent = True
                    return b"x" * 2000
                return b""

        def wrapper(instance, name, mode="r", *args, **kwargs):
            inner = original_open(instance, name, mode, *args, **kwargs)
            filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
            if mode == "r" and filename == "payload.bin":
                return ExtraReader(inner)
            return inner

        with mock.patch.object(zipfile.ZipFile, "open", wrapper):
            self.expect_error(
                "FILE_LIMIT",
                self.d.verify_delivery,
                self.out,
                profile="large",
                limits={"max_file_bytes": 1024},
            )

    def test_failed_pack_removes_only_its_partial_output(self):
        self.make_staging()
        existing = self.write_bytes(self.out, b"keep me\n")
        digest = file_digest(existing)

        self.expect_error(
            "OUTPUT_EXISTS",
            self.d.pack_delivery,
            self.root,
            self.out,
            profile="large",
            compression="stored",
        )
        self.assertEqual(file_digest(existing), digest)
        self.assertEqual(self.read_bytes(existing), b"keep me\n")

        for patch_target, code in (
            ("_dump_manifest", "MANIFEST_LIMIT"),
            ("_write_zip_bytes", "IO_ERROR"),
        ):
            with self.subTest(patch_target=patch_target):
                partial = os.path.join(self.tmp.name, "partial-%s.zip" % patch_target)
                with mock.patch.object(
                    self.d,
                    patch_target,
                    side_effect=self.d.DeliveryError(code),
                ):
                    self.expect_error(
                        code,
                        self.d.pack_delivery,
                        self.root,
                        partial,
                        profile="large",
                        compression="stored",
                    )
                self.assertFalse(os.path.exists(partial))

    def test_verify_failure_does_not_create_or_modify_files(self):
        self.make_staging()
        self.d.pack_delivery(self.root, self.out, profile="large")
        with zipfile.ZipFile(self.out) as archive:
            manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
        original_digest = file_digest(self.out)
        for entry in manifest["files"]:
            if entry["path"] == "payload.bin":
                entry["sha256"] = "0" * 64
        replacement = json.dumps(manifest).encode("utf-8")
        with zipfile.ZipFile(self.out) as source:
            with zipfile.ZipFile(
                os.path.join(self.tmp.name, "tampered.zip"), "w", compression=zipfile.ZIP_STORED
            ) as target:
                for info in source.infolist():
                    data = replacement if info.filename == "MANIFEST.json" else source.read(info.filename)
                    clone = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
                    clone.compress_type = zipfile.ZIP_STORED
                    clone.create_system = info.create_system or 3
                    clone.external_attr = info.external_attr
                    target.writestr(clone, data)
        tampered = os.path.join(self.tmp.name, "tampered.zip")
        self.expect_error(
            "ARCHIVE_HASH_MISMATCH",
            self.d.verify_delivery,
            tampered,
            profile="large",
        )
        self.assertEqual(file_digest(self.out), original_digest)


class EocdAmbiguityTests(LargeDeliveryBase):
    """The bounded preflight and ``zipfile.ZipFile`` must trust one EOCD.

    ``zipfile.ZipFile`` selects the *last* EOCD signature in its trailing
    window and trusts that record's declared comment length, so a normal ZIP
    comment that embeds another EOCD record could otherwise let the preflight
    validate the real (small) directory while ``ZipFile`` followed the fake
    record and allocated an unchecked directory.  These regressions pin the
    strict rejection and the ordinary-comment/ZIP64 compatibility.
    """

    #: Smallest prefix that lets a fake ``cd_size`` of 5 MiB resolve to a
    #: non-negative ``ZipFile`` central-directory offset (the reported repro).
    _PREFIX = 5 * 1024 * 1024
    _FAKE_CD_SIZE = 5 * 1024 * 1024

    def append_comment(self, path, comment):
        raw = self.read_bytes(path)
        eocd = self.find_eocd(raw)
        mutated = bytearray(raw[: eocd + self.d._EOCD_MIN_SIZE])
        struct.pack_into("<H", mutated, eocd + 20, len(comment))
        mutated += comment
        return self.write_bytes(path, bytes(mutated))

    def fake_comment_archive(self):
        """A valid package whose comment embeds a fake 5 MiB-directory EOCD."""
        self.make_staging()
        self.d.pack_delivery(self.root, self.out, profile="large", compression="stored")
        raw = self.read_bytes(self.out)
        eocd = self.find_eocd(raw)

        fake_record = struct.pack(
            "<4s4H2LH",
            _EOCD_SIG,
            0,
            0,  # disk number, directory disk
            1,
            1,  # entries on disk, entries total
            self._FAKE_CD_SIZE,
            0,  # central-directory offset
            0,  # comment length: the trailing byte makes this a mismatch
        )
        comment = fake_record + b"x"
        mutated = bytearray(raw[: eocd + self.d._EOCD_MIN_SIZE])
        struct.pack_into("<H", mutated, eocd + 20, len(comment))
        data = b"\x00" * self._PREFIX + bytes(mutated) + comment
        return self.write_bytes(
            os.path.join(self.tmp.name, "ambiguous-comment.zip"), data
        )

    def spy_open(self, reads):
        actual_open = open

        def opening(file, mode="r", *args, **kwargs):
            handle = actual_open(file, mode, *args, **kwargs)
            if "b" in mode and "r" in mode:
                return _ReadSpy(handle, reads)
            return handle

        return opening

    def test_fake_comment_eocd_rejected_before_zipfile_construction(self):
        path = self.fake_comment_archive()
        reads = []

        def fail(*args, **kwargs):
            raise AssertionError("zipfile.ZipFile was constructed before preflight")

        with mock.patch.object(self.d, "open", self.spy_open(reads), create=True):
            with mock.patch.object(zipfile, "ZipFile", fail):
                self.expect_error(
                    "ARCHIVE_INVALID",
                    self.d.verify_delivery,
                    path,
                    profile="large",
                )
        self.assertTrue(reads)
        self.assertNotIn(self._FAKE_CD_SIZE, reads)
        for size in reads:
            self.assertGreaterEqual(size, 0)
            self.assertLessEqual(
                size, self.d._EOCD_MIN_SIZE + self.d._EOCD_MAX_COMMENT
            )

    def test_fake_comment_eocd_never_requests_an_oversized_read(self):
        path = self.fake_comment_archive()
        reads = []

        with mock.patch.object(self.d, "open", self.spy_open(reads), create=True):
            self.expect_error(
                "ARCHIVE_INVALID", self.d.verify_delivery, path, profile="large"
            )

        self.assertTrue(reads)
        self.assertNotIn(self._FAKE_CD_SIZE, reads)
        for size in reads:
            self.assertGreaterEqual(size, 0)
            self.assertLessEqual(
                size, self.d._EOCD_MIN_SIZE + self.d._EOCD_MAX_COMMENT
            )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(["verify", path, "--profile", "large"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "ARCHIVE_INVALID"})
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_ordinary_archive_comment_still_verifies(self):
        self.make_staging()
        self.d.pack_delivery(self.root, self.out)
        comment = b"ordinary delivery comment without an end record"
        self.append_comment(self.out, comment)

        stats = self.d.verify_delivery(self.out)
        self.assertEqual(stats["files"], 4)
        self.assertEqual(stats["archive_sha256"], file_digest(self.out))

        with self.d.open_bounded_archive(self.out) as archive:
            self.assertEqual(archive.comment, comment)
            self.assertEqual(len(archive.infolist()), 4)

    def test_zip64_archive_with_ordinary_comment_still_verifies(self):
        self.make_staging(payload_size=4096)
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 1024):
            self.d.pack_delivery(
                self.root, self.out, profile="large", compression="stored"
            )
        comment = b"zip64 ordinary archive comment"
        self.append_comment(self.out, comment)

        raw = self.read_bytes(self.out)
        self.assertIn(_ZIP64_EOCD_SIG, raw)
        self.assertIn(_ZIP64_LOCATOR_SIG, raw)

        stats = self.d.verify_delivery(self.out, profile="large")
        self.assertEqual(stats["files"], 4)
        self.assertEqual(stats["archive_sha256"], file_digest(self.out))
        # The small ZIP64 archive with a comment is still valid under the
        # standard profile caps.
        self.assertEqual(self.d.verify_delivery(self.out), stats)

        with self.d.open_bounded_archive(self.out, profile="large") as archive:
            self.assertEqual(archive.comment, comment)
            self.assertGreaterEqual(
                archive.getinfo("payload.bin").extract_version, 45
            )


class EocdTailSelectionTests(LargeDeliveryBase):
    """Pin ``_find_eocd`` to the exact record ``zipfile`` would select.

    ``zipfile._EndRecData`` first tries the fixed 22-byte no-comment block and
    only otherwise searches the whole trailing window for the *last* EOCD
    signature.  A signature encoded in an earlier record's own numeric fields
    (for example ``cd_offset``) must not make the preflight accept one record
    while ``ZipFile`` unpacks another.  These are tiny in-memory regressions;
    they never build a multi-MiB fixture.
    """

    _SIG_AS_OFFSET = int.from_bytes(_EOCD_SIG, "little")

    def eocd_record(self, *, cd_size=0, cd_offset=0, comment=b""):
        return struct.pack(
            "<4s4H2LH",
            _EOCD_SIG,
            0,
            0,  # disk number, directory disk
            1,
            1,  # entries on disk, entries total
            cd_size,
            cd_offset,
            len(comment),
        )

    def find(self, data):
        handle = io.BytesIO(data)
        return self.d._find_eocd(handle, len(data))

    def test_commented_numeric_field_signature_is_rejected(self):
        comment = b"an ordinary-looking archive comment"
        record = self.eocd_record(cd_offset=self._SIG_AS_OFFSET, comment=comment)
        data = b"\x00" * 128 + record + comment
        real = 128

        self.assertEqual(data[real : real + 4], _EOCD_SIG)
        # The real record's comment length lands exactly at EOF, so a
        # first-match loop would have accepted it ...
        self.assertEqual(real + self.d._EOCD_MIN_SIZE + len(comment), len(data))
        # ... but the *last* signature is the one encoded in cd_offset, which
        # is what zipfile would unpack.
        self.assertEqual(data.rfind(_EOCD_SIG), real + 16)

        self.expect_error("ARCHIVE_INVALID", self.find, data)

    def test_no_comment_fast_path_ignores_numeric_field_signature(self):
        record = self.eocd_record(cd_offset=self._SIG_AS_OFFSET)
        data = b"\x00" * 128 + record

        position, fields = self.find(data)

        self.assertEqual(position, len(data) - self.d._EOCD_MIN_SIZE)
        self.assertEqual(fields[0], _EOCD_SIG)
        self.assertEqual(fields[6], self._SIG_AS_OFFSET)
        self.assertEqual(fields[7], 0)
        # The signature really is present inside the no-comment record, so the
        # fast path (not a last-signature search) is what accepts it.
        self.assertEqual(data.rfind(_EOCD_SIG), len(data) - self.d._EOCD_MIN_SIZE + 16)

    def test_commented_record_is_used_when_it_is_the_last_signature(self):
        comment = b"plain comment"
        record = self.eocd_record(cd_size=46, cd_offset=0, comment=comment)
        data = b"\x00" * 128 + record + comment

        position, fields = self.find(data)

        self.assertEqual(position, 128)
        self.assertEqual(fields[7], len(comment))
        self.assertEqual(len(data) - position - self.d._EOCD_MIN_SIZE, len(comment))


class OldSuiteCompatibilityTests(unittest.TestCase):
    """Run the original 52/31 suites unchanged against the new scripts."""

    def run_old_suite(self, module, expected_count, label):
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        self.assertEqual(
            result.testsRun,
            expected_count,
            "%s suite count changed: %s" % (label, stream.getvalue()),
        )
        if not result.wasSuccessful():
            details = stream.getvalue()
            if len(details) > 4000:
                details = details[-4000:]
            self.fail("%s suite failed:\n%s" % (label, details))

    def test_original_base_suite_still_passes(self):
        ensure_modules()
        base = load_module(_BASE_TESTS, "large_delivery_original_base")
        base.load_tool(_TOOL.__file__)
        self.run_old_suite(base, 52, "original delivery")

    def test_original_release_suite_still_passes(self):
        ensure_modules()
        installer = _INSTALLER or os.path.join(
            os.path.dirname(os.path.abspath(_RELEASE_TESTS)), "install.py"
        )
        release = load_module(_RELEASE_TESTS, "large_delivery_original_release")
        release._CHECKER_SRC = os.path.abspath(_CHECKER.__file__)
        release._INSTALLER_SRC = os.path.abspath(installer)
        release._TRUSTED_SRC = os.path.abspath(_TOOL.__file__)
        release._TRUSTED_MODULE = None
        self.run_old_suite(release, 31, "original release")


def main():
    global _TOOL, _CHECKER, _BASE_TESTS, _RELEASE_TESTS, _INSTALLER

    parser = argparse.ArgumentParser(
        description="Run large-delivery and original-suite compatibility tests."
    )
    parser.add_argument("--tool", default=_DEFAULT_TOOL, help="path to delivery.py")
    parser.add_argument("--checker", default=_DEFAULT_CHECKER, help="path to check_handoff.py")
    parser.add_argument(
        "--base-tests", default=_DEFAULT_BASE_TESTS, help="path to original test_delivery.py"
    )
    parser.add_argument(
        "--release-tests", default=_DEFAULT_RELEASE_TESTS, help="path to original test_release.py"
    )
    parser.add_argument(
        "--installer",
        default=None,
        help="path to install.py (defaults beside the release tests)",
    )
    args, remaining = parser.parse_known_args()

    _TOOL = load_module(args.tool, "large_delivery_tool")
    _CHECKER = load_module(args.checker, "large_delivery_checker")
    _BASE_TESTS = os.path.abspath(args.base_tests)
    _RELEASE_TESTS = os.path.abspath(args.release_tests)
    _INSTALLER = os.path.abspath(args.installer) if args.installer else None

    unittest.main(argv=[sys.argv[0]] + remaining)


if __name__ == "__main__":
    main()
