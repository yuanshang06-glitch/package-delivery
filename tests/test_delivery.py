"""Fixture tests for the package-delivery helper (task 008).

Run either form::

    python tests/test_delivery.py
    python tests/test_delivery.py --tool /path/to/delivery.py

The tests import ``delivery.py`` in-process with ``importlib`` (no subprocess
pipes), build all fixtures inside ``tempfile`` directories, and never touch a
real project, user package, network, or configuration.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_TOOL = os.path.abspath(os.path.join(_HERE, "..", "src", "delivery.py"))

_MODULE = None


def load_tool(path):
    global _MODULE
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise RuntimeError("delivery tool not found: %s" % path)
    spec = importlib.util.spec_from_file_location("delivery_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import delivery tool: %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE = module
    return module


def tool():
    if _MODULE is None:
        load_tool(_DEFAULT_TOOL)
    return _MODULE


def sha256_file(path):
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
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as handle:
                data = handle.read()
            result[relative] = (len(data), hashlib.sha256(data).hexdigest())
    return result


def rewrite_zip(source, target, replace=None, drop=(), extra=None):
    """Rebuild a ZIP with stored entries, optionally changing its payload."""
    replace = replace or {}
    drop = set(drop)
    with zipfile.ZipFile(source) as source_zip:
        infos = source_zip.infolist()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as target_zip:
            for info in infos:
                if info.filename in drop:
                    continue
                if info.filename in replace:
                    data = replace[info.filename]
                else:
                    data = source_zip.read(info.filename)
                replacement = zipfile.ZipInfo(
                    info.filename, date_time=(1980, 1, 1, 0, 0, 0)
                )
                replacement.compress_type = zipfile.ZIP_STORED
                replacement.create_system = info.create_system or 3
                replacement.external_attr = info.external_attr
                target_zip.writestr(replacement, data)
            for name, data in (extra or {}).items():
                target_zip.writestr(name, data)
    return target


def add_entry(source, target, name, data, external_attr=None):
    with zipfile.ZipFile(source) as source_zip:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as target_zip:
            for info in source_zip.infolist():
                replacement = zipfile.ZipInfo(
                    info.filename, date_time=(1980, 1, 1, 0, 0, 0)
                )
                replacement.compress_type = zipfile.ZIP_STORED
                replacement.create_system = info.create_system or 3
                replacement.external_attr = info.external_attr
                target_zip.writestr(replacement, source_zip.read(info.filename))
            new_info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            new_info.compress_type = zipfile.ZIP_STORED
            new_info.create_system = 3
            if external_attr is not None:
                new_info.external_attr = external_attr
            target_zip.writestr(new_info, data)
    return target


def make_junction(link, target):
    """Create an unprivileged Windows junction for fixture setup (no pipes)."""
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return completed.returncode == 0 and os.path.isdir(link)


def set_encrypted_flag(path):
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    position = 0
    while True:
        position = data.find(b"PK\x03\x04", position)
        if position < 0:
            break
        data[position + 6] |= 0x01
        position += 4
    position = 0
    while True:
        position = data.find(b"PK\x01\x02", position)
        if position < 0:
            break
        data[position + 8] |= 0x01
        position += 4
    with open(path, "wb") as handle:
        handle.write(bytes(data))


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = tool()
        self.root = os.path.join(self.tmp.name, "staging")
        os.makedirs(self.root)
        self.out = os.path.join(self.tmp.name, "delivery.zip")

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

    def default_meta(self, **overrides):
        meta = {
            "schema_version": "collaborator-delivery/1",
            "task_id": "package-delivery-008",
            "attempt_id": "package-delivery-008-a1",
            "title": "测试交付",
            "summary": "用于验证打包助手的夹具交付",
            "status": "completed",
            "independent_acceptance": "UNKNOWN",
            "files": [
                "START-HERE.md",
                "artifacts/结果 报告.md",
                "evidence/checks.json",
            ],
            "checks": [
                {
                    "name": "结构与摘要核对",
                    "status": "passed",
                    "evidence": "evidence/checks.json",
                }
            ],
            "unfinished": [],
            "requirements": ["Python 3.11+"],
            "next_steps": ["先打开 START-HERE.md"],
        }
        meta.update(overrides)
        return meta

    def write_delivery(self, meta):
        self.write("DELIVERY.json", json.dumps(meta, ensure_ascii=False))

    def make_default_staging(self, **overrides):
        meta = self.default_meta(**overrides)
        self.write("START-HERE.md", "# 开始\n\n先读这里。\n")
        self.write("artifacts/结果 报告.md", "结果正文\n")
        self.write("evidence/checks.json", '{"ok": true}\n')
        self.write_delivery(meta)
        return meta

    def expect_error(self, code, func, *args, **kwargs):
        with self.assertRaises(self.d.DeliveryError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def expect_metadata_error(self, meta, code):
        self.write_delivery(meta)
        self.expect_error(code, self.d.pack_delivery, self.root, self.out)
        self.assertFalse(os.path.exists(self.out))


class RoundTripTests(DeliveryTest):
    def test_roundtrip_chinese_spaces_stats_and_manifest(self):
        self.make_default_staging()
        before = snapshot(self.root)

        stats = self.d.pack_delivery(self.root, self.out)

        self.assertEqual(snapshot(self.root), before, "packing must not modify staging")
        self.assertEqual(stats["files"], 5)
        self.assertRegex(stats["archive_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(stats["archive_sha256"], sha256_file(self.out))

        with zipfile.ZipFile(self.out) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "DELIVERY.json",
                    "MANIFEST.json",
                    "START-HERE.md",
                    "artifacts/结果 报告.md",
                    "evidence/checks.json",
                },
            )
            self.assertEqual(stats["total_bytes"], sum(i.file_size for i in archive.infolist()))
            manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
            self.assertEqual(manifest["schema_version"], "delivery-manifest/1")
            self.assertEqual(
                {entry["path"] for entry in manifest["files"]}, names - {"MANIFEST.json"}
            )
            for entry in manifest["files"]:
                data = archive.read(entry["path"])
                self.assertEqual(entry["bytes"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())

        self.assertEqual(self.d.verify_delivery(self.out), stats)
        self.assertEqual(self.d.verify_delivery(self.out)["files"], 5)

    def test_whitelist_only_and_source_unchanged(self):
        self.make_default_staging()
        self.write("extra/not-listed.txt", "ignore me")
        before = snapshot(self.root)

        stats = self.d.pack_delivery(self.root, self.out)

        self.assertEqual(snapshot(self.root), before)
        self.assertTrue(os.path.exists(self.path("extra/not-listed.txt")))
        with zipfile.ZipFile(self.out) as archive:
            self.assertNotIn("extra/not-listed.txt", archive.namelist())
        self.assertEqual(stats["files"], 5)

    def test_empty_payload_file_allowed(self):
        self.write("START-HERE.md", "# hi\n")
        self.write("empty.bin", b"")
        self.write_delivery(
            self.default_meta(files=["START-HERE.md", "empty.bin"], checks=[])
        )

        stats = self.d.pack_delivery(self.root, self.out)

        self.assertEqual(self.d.verify_delivery(self.out), stats)
        with zipfile.ZipFile(self.out) as archive:
            manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
        empty = [e for e in manifest["files"] if e["path"] == "empty.bin"]
        self.assertEqual(empty[0]["bytes"], 0)
        self.assertEqual(empty[0]["sha256"], hashlib.sha256(b"").hexdigest())

    def test_accepts_pathlike_arguments(self):
        self.make_default_staging()

        stats = self.d.pack_delivery(pathlib.Path(self.root), pathlib.Path(self.out))

        self.assertEqual(self.d.verify_delivery(pathlib.Path(self.out)), stats)


class FormatRuleTests(DeliveryTest):
    def test_completed_rejects_unfinished_items(self):
        self.expect_metadata_error(
            self.default_meta(status="completed", unfinished=["还剩一项"]),
            "METADATA_INVALID",
        )

    def test_completed_rejects_failed_check(self):
        meta = self.default_meta(
            checks=[
                {
                    "name": "失败检查",
                    "status": "failed",
                    "evidence": "evidence/checks.json",
                }
            ]
        )
        self.expect_metadata_error(meta, "METADATA_INVALID")

    def test_partial_and_blocked_require_unfinished(self):
        for status in ("partial", "blocked"):
            with self.subTest(status=status):
                self.expect_metadata_error(
                    self.default_meta(status=status, unfinished=[]),
                    "METADATA_INVALID",
                )

    def test_partial_with_unfinished_packs_and_roundtrips(self):
        meta = self.make_default_staging(
            status="partial",
            unfinished=["补充真实任务包联调"],
            checks=[
                {"name": "本地夹具自测", "status": "passed", "evidence": "evidence/checks.json"},
                {"name": "跨工具实测", "status": "not_run", "reason": "由接收方另开真实任务"},
            ],
        )

        stats = self.d.pack_delivery(self.root, self.out)

        self.assertEqual(self.d.verify_delivery(self.out), stats)
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(
                json.loads(archive.read("DELIVERY.json").decode("utf-8"))["status"],
                "partial",
            )

    def test_attempt_id_is_optional(self):
        meta = self.make_default_staging()
        meta.pop("attempt_id")
        self.write_delivery(meta)

        stats = self.d.pack_delivery(self.root, self.out)

        self.assertEqual(self.d.verify_delivery(self.out), stats)

    def test_not_run_requires_reason(self):
        meta = self.default_meta(checks=[{"name": "未跑项", "status": "not_run"}])
        self.expect_metadata_error(meta, "METADATA_INVALID")

    def test_passed_evidence_must_be_declared(self):
        meta = self.default_meta(
            checks=[{"name": "坏证据", "status": "passed", "evidence": "evidence/missing.json"}]
        )
        self.expect_metadata_error(meta, "METADATA_INVALID")

    def test_independent_acceptance_must_be_unknown(self):
        for value in ("ACCEPTED", "accepted", True, None):
            with self.subTest(value=value):
                self.expect_metadata_error(
                    self.default_meta(independent_acceptance=value), "METADATA_INVALID"
                )

    def test_unknown_field_cannot_claim_acceptance(self):
        meta = self.default_meta(accepted=True)
        self.expect_metadata_error(meta, "METADATA_ACCEPTANCE_CLAIM")

    def test_required_metadata_fields(self):
        cases = [
            ({"schema_version": "other/1"}, "METADATA_INVALID"),
            ({"task_id": "Upper-Case"}, "METADATA_INVALID"),
            ({"task_id": "-leading"}, "METADATA_INVALID"),
            ({"task_id": "x" * 129}, "METADATA_INVALID"),
            ({"attempt_id": "bad id"}, "METADATA_INVALID"),
            ({"title": "  "}, "METADATA_INVALID"),
            ({"summary": ""}, "METADATA_INVALID"),
            ({"status": "done"}, "METADATA_INVALID"),
            ({"files": []}, "FILES_INVALID"),
            ({"files": ["artifacts/a.md"]}, "FILES_INVALID"),
            ({"files": ["START-HERE.md", "DELIVERY.json"]}, "FILES_INVALID"),
            ({"files": ["START-HERE.md", "MANIFEST.json"]}, "FILES_INVALID"),
            ({"files": ["START-HERE.md", "START-HERE.md"]}, "FILES_INVALID"),
            ({"files": ["START-HERE.md", "start-here.md"]}, "FILES_INVALID"),
            ({"checks": "nope"}, "METADATA_INVALID"),
            ({"unfinished": "nope"}, "METADATA_INVALID"),
            ({"requirements": [""]}, "METADATA_INVALID"),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides):
                self.expect_metadata_error(self.default_meta(**overrides), code)


class PathRuleTests(DeliveryTest):
    def test_traversal_and_platform_unsafe_paths(self):
        cases = [
            "../outside.md",
            "a/../../outside.md",
            "/absolute.md",
            "a\\b.md",
            "C:/drive.md",
            "a//b.md",
            "a/./b.md",
            "trailing.",
            "trailing ",
            "CON.txt",
            "aux",
            "a/<bad>.md",
            "a|b.md",
        ]
        for bad in cases:
            with self.subTest(path=bad):
                self.expect_metadata_error(
                    self.default_meta(files=["START-HERE.md", bad]), "PATH_INVALID"
                )

    def test_obvious_secret_paths_rejected(self):
        cases = [
            ".env",
            ".env.local",
            ".env.production",
            ".git/config",
            ".ssh/id_rsa",
            "inner/.git/config",
            "keys/server.pem",
            "server.KEY",
            "id_rsa",
            "id_ed25519",
            "credentials.json",
        ]
        for bad in cases:
            with self.subTest(path=bad):
                self.expect_metadata_error(
                    self.default_meta(files=["START-HERE.md", bad], checks=[]), "PATH_SECRET"
                )

    def test_env_templates_are_allowed(self):
        files = ["START-HERE.md", ".env.example", ".env.sample", ".env.template"]
        self.write("START-HERE.md", "# hi\n")
        for name in files[1:]:
            self.write(name, "SAMPLE=1\n")
        self.write_delivery(self.default_meta(files=files, checks=[]))

        stats = self.d.pack_delivery(self.root, self.out)

        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(set(archive.namelist()), set(files) | {"DELIVERY.json", "MANIFEST.json"})
        self.assertEqual(self.d.verify_delivery(self.out), stats)

    def test_directory_instead_of_file_rejected(self):
        self.make_default_staging()
        os.makedirs(self.path("adir"))
        self.write_delivery(
            self.default_meta(files=["START-HERE.md", "adir"], checks=[])
        )
        self.expect_error("FILE_NOT_REGULAR", self.d.pack_delivery, self.root, self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_symlink_file_rejected(self):
        self.make_default_staging()
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "wb") as handle:
            handle.write(b"outside\n")
        link = self.path("linked.md")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("cannot create symlinks here: %s" % exc)
        self.write_delivery(
            self.default_meta(files=["START-HERE.md", "linked.md"], checks=[])
        )
        self.expect_error("PATH_UNSAFE", self.d.pack_delivery, self.root, self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_symlink_parent_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        self.write("START-HERE.md", "# hi\n")
        outside_dir = os.path.join(self.tmp.name, "outside-dir")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "file.md"), "wb") as handle:
            handle.write(b"outside\n")
        link_dir = self.path("linked-dir")
        try:
            os.symlink(outside_dir, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("cannot create directory symlinks here: %s" % exc)
        self.write_delivery(
            self.default_meta(files=["START-HERE.md", "linked-dir/file.md"], checks=[])
        )
        self.expect_error("PATH_UNSAFE", self.d.pack_delivery, self.root, self.out)

    def test_junction_parent_rejected(self):
        if os.name != "nt":
            self.skipTest("junctions are a Windows fixture")
        self.write("START-HERE.md", "# hi\n")
        outside_dir = os.path.join(self.tmp.name, "junction-target")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "file.md"), "wb") as handle:
            handle.write(b"outside\n")
        junction = self.path("junction-dir")
        if not make_junction(junction, outside_dir):
            self.skipTest("cannot create a junction here")
        self.write_delivery(
            self.default_meta(files=["START-HERE.md", "junction-dir/file.md"], checks=[])
        )
        self.expect_error("PATH_UNSAFE", self.d.pack_delivery, self.root, self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_junction_root_rejected(self):
        if os.name != "nt":
            self.skipTest("junctions are a Windows fixture")
        self.make_default_staging()
        junction_root = os.path.join(self.tmp.name, "root-junction")
        if not make_junction(junction_root, self.root):
            self.skipTest("cannot create a junction here")
        self.expect_error("ROOT_INVALID", self.d.pack_delivery, junction_root, self.out)
        self.assertFalse(os.path.exists(self.out))


class OutputTests(DeliveryTest):
    def test_existing_output_is_never_overwritten(self):
        self.make_default_staging()
        self.d.pack_delivery(self.root, self.out)
        digest = sha256_file(self.out)

        self.expect_error("OUTPUT_EXISTS", self.d.pack_delivery, self.root, self.out)

        self.assertEqual(sha256_file(self.out), digest)

    def test_empty_existing_output_rejected(self):
        self.make_default_staging()
        open(self.out, "wb").close()
        self.expect_error("OUTPUT_EXISTS", self.d.pack_delivery, self.root, self.out)

    def test_existing_directory_output_rejected(self):
        self.make_default_staging()
        os.makedirs(self.out)
        self.expect_error("OUTPUT_EXISTS", self.d.pack_delivery, self.root, self.out)

    def test_output_parent_must_exist(self):
        self.make_default_staging()
        missing_parent = os.path.join(self.tmp.name, "missing-dir", "out.zip")
        self.expect_error(
            "OUTPUT_PARENT_MISSING", self.d.pack_delivery, self.root, missing_parent
        )

    def test_failure_removes_only_the_new_output(self):
        self.write("START-HERE.md", "# hi\n")
        self.write_delivery(
            self.default_meta(files=["START-HERE.md", "missing.md"], checks=[])
        )
        self.expect_error("FILE_MISSING", self.d.pack_delivery, self.root, self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_failure_after_partial_write_cleans_up(self):
        self.write("START-HERE.md", "# hi\n")
        self.write("big.bin", b"x" * 4096)
        self.write_delivery(
            self.default_meta(files=["START-HERE.md", "big.bin"], checks=[])
        )
        self.expect_error(
            "FILE_LIMIT",
            self.d.pack_delivery,
            self.root,
            self.out,
            limits={"max_file_bytes": 1024},
        )
        self.assertFalse(os.path.exists(self.out))


class LimitTests(DeliveryTest):
    def test_invalid_limit_overrides(self):
        self.make_default_staging()
        too_big = self.d.DEFAULT_LIMITS["max_file_bytes"] + 1
        bads = [
            True,
            -1,
            "16",
            1.5,
            {"max_entries": True},
            {"max_file_bytes": -1},
            {"max_file_bytes": "16"},
            {"max_total_bytes": 1.5},
            {"unknown_limit": 1},
            {"max_file_bytes": too_big},
        ]
        for bad in bads:
            with self.subTest(limits=bad):
                self.expect_error(
                    "INVALID_LIMITS",
                    self.d.pack_delivery,
                    self.root,
                    self.out,
                    limits=bad,
                )

    def test_reduced_entry_limit(self):
        self.make_default_staging()
        self.expect_error(
            "ENTRY_LIMIT",
            self.d.pack_delivery,
            self.root,
            self.out,
            limits={"max_entries": 3},
        )
        self.assertFalse(os.path.exists(self.out))

    def test_reduced_metadata_limit(self):
        self.make_default_staging()
        self.expect_error(
            "METADATA_LIMIT",
            self.d.pack_delivery,
            self.root,
            self.out,
            limits={"max_metadata_bytes": 10},
        )
        self.assertFalse(os.path.exists(self.out))

    def test_reduced_manifest_limit(self):
        self.make_default_staging()
        self.expect_error(
            "MANIFEST_LIMIT",
            self.d.pack_delivery,
            self.root,
            self.out,
            limits={"max_manifest_bytes": 10},
        )
        self.assertFalse(os.path.exists(self.out))

    def test_reduced_total_limit(self):
        self.make_default_staging()
        self.expect_error(
            "TOTAL_LIMIT",
            self.d.pack_delivery,
            self.root,
            self.out,
            limits={"max_total_bytes": 50},
        )
        self.assertFalse(os.path.exists(self.out))

    def test_reduced_file_limit(self):
        self.make_default_staging()
        self.expect_error(
            "FILE_LIMIT",
            self.d.pack_delivery,
            self.root,
            self.out,
            limits={"max_file_bytes": 8},
        )
        self.assertFalse(os.path.exists(self.out))

    def test_verify_honors_reduced_limits(self):
        self.make_default_staging()
        self.d.pack_delivery(self.root, self.out)
        self.expect_error(
            "ENTRY_LIMIT", self.d.verify_delivery, self.out, limits={"max_entries": 3}
        )
        self.expect_error(
            "TOTAL_LIMIT", self.d.verify_delivery, self.out, limits={"max_total_bytes": 10}
        )
        self.expect_error(
            "METADATA_LIMIT",
            self.d.verify_delivery,
            self.out,
            limits={"max_metadata_bytes": 10},
        )


class VerifyTamperTests(DeliveryTest):
    def setUp(self):
        super().setUp()
        self.make_default_staging()
        self.d.pack_delivery(self.root, self.out)
        self.bad = os.path.join(self.tmp.name, "tampered.zip")

    def test_tampered_payload_bytes_rejected(self):
        with zipfile.ZipFile(self.out) as archive:
            original = archive.read("START-HERE.md")
        replacement = original[:-1] + b"!"
        rewrite_zip(self.out, self.bad, replace={"START-HERE.md": replacement})
        self.expect_error("ARCHIVE_HASH_MISMATCH", self.d.verify_delivery, self.bad)

    def test_tampered_delivery_metadata_rejected(self):
        with zipfile.ZipFile(self.out) as archive:
            original = archive.read("DELIVERY.json")
        replacement = original.replace(b"package-delivery-008", b"package-delivery-009")
        self.assertNotEqual(replacement, original)
        rewrite_zip(self.out, self.bad, replace={"DELIVERY.json": replacement})
        self.expect_error("ARCHIVE_HASH_MISMATCH", self.d.verify_delivery, self.bad)

    def test_zip_path_traversal_entry_rejected(self):
        add_entry(self.out, self.bad, "../evil.txt", b"evil\n")
        self.expect_error("PATH_INVALID", self.d.verify_delivery, self.bad)

    def test_case_alias_duplicate_entry_rejected(self):
        add_entry(self.out, self.bad, "start-here.md", b"alias\n")
        self.expect_error("DUPLICATE_ENTRY", self.d.verify_delivery, self.bad)

    def test_tampered_manifest_digest_rejected(self):
        with zipfile.ZipFile(self.out) as archive:
            manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
        for entry in manifest["files"]:
            if entry["path"] == "START-HERE.md":
                entry["sha256"] = "0" * 64
        rewrite_zip(
            self.out,
            self.bad,
            replace={"MANIFEST.json": json.dumps(manifest).encode("utf-8")},
        )
        self.expect_error("ARCHIVE_HASH_MISMATCH", self.d.verify_delivery, self.bad)

    def test_manifest_schema_and_set_mismatch_rejected(self):
        with zipfile.ZipFile(self.out) as archive:
            manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
        bad_schema = {"schema_version": "other/1", "files": manifest["files"]}
        rewrite_zip(
            self.out,
            self.bad,
            replace={"MANIFEST.json": json.dumps(bad_schema).encode("utf-8")},
        )
        self.expect_error("MANIFEST_INVALID", self.d.verify_delivery, self.bad)

        missing = dict(manifest)
        missing["files"] = [e for e in manifest["files"] if e["path"] != "START-HERE.md"]
        rewrite_zip(
            self.out,
            self.bad,
            replace={"MANIFEST.json": json.dumps(missing).encode("utf-8")},
        )
        self.expect_error("MANIFEST_INVALID", self.d.verify_delivery, self.bad)

    def test_missing_zip_entry_rejected(self):
        rewrite_zip(self.out, self.bad, drop={"START-HERE.md"})
        self.expect_error("ARCHIVE_ENTRY_MISMATCH", self.d.verify_delivery, self.bad)

    def test_extra_zip_entry_rejected(self):
        rewrite_zip(self.out, self.bad, extra={"extra.txt": b"extra\n"})
        self.expect_error("ARCHIVE_ENTRY_MISMATCH", self.d.verify_delivery, self.bad)

    def test_duplicate_zip_entry_rejected(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(self.out) as source:
                with zipfile.ZipFile(self.bad, "w", compression=zipfile.ZIP_STORED) as target:
                    for info in source.infolist():
                        target.writestr(info.filename, source.read(info.filename))
                    target.writestr("START-HERE.md", b"duplicate\n")
        self.expect_error("DUPLICATE_ENTRY", self.d.verify_delivery, self.bad)

    def test_not_a_zip_rejected(self):
        with open(self.bad, "wb") as handle:
            handle.write(b"this is not a zip file")
        self.expect_error("ARCHIVE_INVALID", self.d.verify_delivery, self.bad)

    def test_bad_crc_rejected(self):
        marker = b"CORRUPT-ME-0123456789-ABCDEF"
        self.write("START-HERE.md", marker)
        self.write_delivery(self.default_meta(checks=[]))
        good = os.path.join(self.tmp.name, "good.zip")
        self.d.pack_delivery(self.root, good)
        stored = os.path.join(self.tmp.name, "stored.zip")
        rewrite_zip(good, stored)
        with open(stored, "rb") as handle:
            data = bytearray(handle.read())
        position = data.find(marker)
        self.assertGreaterEqual(position, 0)
        data[position + 5] ^= 0xFF
        with open(self.bad, "wb") as handle:
            handle.write(bytes(data))
        self.expect_error("ARCHIVE_INVALID", self.d.verify_delivery, self.bad)

    def test_symlink_entry_rejected(self):
        add_entry(self.out, self.bad, "link.md", b"../../etc/passwd", 0o120777 << 16)
        self.expect_error("ENTRY_SYMLINK", self.d.verify_delivery, self.bad)

    def test_directory_entry_rejected(self):
        add_entry(self.out, self.bad, "adir/", b"", (0o40755 << 16) | 0x10)
        self.expect_error("ENTRY_NOT_FILE", self.d.verify_delivery, self.bad)

    def test_encrypted_entry_rejected(self):
        rewrite_zip(self.out, self.bad)
        set_encrypted_flag(self.bad)
        self.expect_error("ARCHIVE_ENCRYPTED", self.d.verify_delivery, self.bad)

    def test_missing_archive_rejected(self):
        self.expect_error(
            "ARCHIVE_NOT_FOUND",
            self.d.verify_delivery,
            os.path.join(self.tmp.name, "absent.zip"),
        )


class CliTests(DeliveryTest):
    def test_pack_verify_and_error_output(self):
        self.make_default_staging()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(["pack", "--root", self.root, "--out", self.out])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        stats = json.loads(stdout.getvalue())
        self.assertEqual(set(stats), {"archive_sha256", "files", "total_bytes"})

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(["verify", self.out])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), stats)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(["verify", os.path.join(self.tmp.name, "absent.zip")])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "ARCHIVE_NOT_FOUND"})

    def test_cli_pack_refuses_existing_output(self):
        self.make_default_staging()
        self.d.pack_delivery(self.root, self.out)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.d.main(["pack", "--root", self.root, "--out", self.out])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "OUTPUT_EXISTS"})


def main():
    parser = argparse.ArgumentParser(description="Run package-delivery fixture tests.")
    parser.add_argument("--tool", default=_DEFAULT_TOOL, help="path to delivery.py")
    args, remaining = parser.parse_known_args()
    load_tool(args.tool)
    unittest.main(argv=[sys.argv[0]] + remaining)


if __name__ == "__main__":
    main()
