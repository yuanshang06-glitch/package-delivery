"""Fixture tests for the package-delivery v1.1.0 checker and installer.

Run either form::

    python tests/test_release.py
    python tests/test_release.py --checker <path> --installer <path> [--delivery <path>]

``--checker`` and ``--installer`` name the scripts under test.  The tests build
their own synthetic repository/worktree, manifest, skill source, and delivery
ZIPs inside ``tempfile`` directories.  They never touch a real user profile,
network, package index, or configuration.

The trusted helper (``delivery.py``) is copied into the synthetic repository
next to the checker / under ``package-delivery/scripts/`` so both scripts use
exactly the layout they have in the real release.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile

sys.dont_write_bytecode = True

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CHECKER = os.path.abspath(os.path.join(_HERE, "..", "src", "check_handoff.py"))
_DEFAULT_INSTALLER = os.path.abspath(os.path.join(_HERE, "..", "src", "install.py"))

_CHECKER_SRC = None
_INSTALLER_SRC = None
_TRUSTED_SRC = None
_TRUSTED_MODULE = None


# ---------------------------------------------------------------------------
# Module loading and fixture helpers
# ---------------------------------------------------------------------------


def load_module(path, prefix):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise RuntimeError("script not found: %s" % path)
    name = "%s_%s_%s" % (prefix, hashlib.sha1(path.encode("utf-8")).hexdigest()[:8], uuid.uuid4().hex[:6])
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import: %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trusted_module():
    global _TRUSTED_MODULE
    if _TRUSTED_MODULE is None:
        _TRUSTED_MODULE = load_module(_TRUSTED_SRC, "trusted_delivery")
    return _TRUSTED_MODULE


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


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
            with open(path, "rb") as handle:
                data = handle.read()
            result[os.path.relpath(path, root).replace(os.sep, "/")] = (
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
    return result


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def write_file(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def remove_directory_link(link):
    if os.name == "nt":
        os.rmdir(link)
    else:
        os.unlink(link)


def rewrite_zip(source, target, replace=None):
    replace = replace or {}
    with zipfile.ZipFile(source) as source_zip:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as target_zip:
            for info in source_zip.infolist():
                data = replace.get(info.filename, source_zip.read(info.filename))
                clone = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
                clone.compress_type = zipfile.ZIP_STORED
                clone.create_system = info.create_system or 3
                clone.external_attr = info.external_attr
                target_zip.writestr(clone, data)
    return target


def make_directory_link(link, target):
    """Create a directory junction (Windows) or symlink (POSIX), no pipes."""
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", link, target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        return completed.returncode == 0 and os.path.isdir(link)
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return os.path.isdir(link)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.trusted = trusted_module()

    # -- synthetic repository -------------------------------------------------

    def build_repo(
        self,
        *,
        version="1.1.0",
        skill_body="# package-delivery\n\nFixture skill.\n",
        extra_source=None,
        manifest_version=None,
        checker=True,
    ):
        repo = os.path.join(self.base, "repo")
        source = os.path.join(repo, "package-delivery")
        scripts = os.path.join(source, "scripts")
        os.makedirs(scripts, exist_ok=True)
        write_file(os.path.join(scripts, "delivery.py"), read_bytes(_TRUSTED_SRC))
        if checker:
            write_file(os.path.join(scripts, "check_handoff.py"), read_bytes(_CHECKER_SRC))
        write_file(os.path.join(source, "SKILL.md"), skill_body)
        write_file(os.path.join(source, "VERSION"), version + "\n")
        for relative, data in (extra_source or {}).items():
            write_file(os.path.join(source, *relative.split("/")), data)
        write_file(os.path.join(repo, "README.md"), "# repo readme, not part of the skill\n")
        installer_path = os.path.join(repo, "scripts", "install.py")
        write_file(installer_path, read_bytes(_INSTALLER_SRC))

        self.write_manifest(repo, manifest_version or version)
        return repo

    def source_root(self, repo):
        return os.path.join(repo, "package-delivery")

    def manifest_entries(self, repo):
        source = self.source_root(repo)
        entries = []
        for base, dirs, files in os.walk(source):
            dirs.sort()
            files.sort()
            for name in files:
                full = os.path.join(base, name)
                relative = os.path.relpath(full, source).replace(os.sep, "/")
                with open(full, "rb") as handle:
                    data = handle.read()
                entries.append(
                    {
                        "path": "package-delivery/" + relative,
                        "bytes": len(data),
                        "sha256": sha256_bytes(data),
                    }
                )
        return sorted(entries, key=lambda entry: entry["path"])

    def write_manifest(self, repo, version, entries=None):
        if entries is None:
            entries = self.manifest_entries(repo)
        payload = {"version": version, "files": entries}
        write_file(
            os.path.join(repo, "release-manifest.json"),
            json.dumps(payload, ensure_ascii=False),
        )

    def read_manifest(self, repo):
        with open(os.path.join(repo, "release-manifest.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def refresh_manifest(self, repo, version, mutate=None):
        entries = self.manifest_entries(repo)
        if mutate is not None:
            mutate(entries)
        self.write_manifest(repo, version, entries)

    def load_installer(self, repo):
        return load_module(os.path.join(repo, "scripts", "install.py"), "release_installer")

    def load_checker(self, repo):
        return load_module(
            os.path.join(repo, "package-delivery", "scripts", "check_handoff.py"),
            "release_checker",
        )

    # -- delivery fixtures ----------------------------------------------------

    def make_delivery(self, repo, *, traceability=None, include_traceability=True,
                      status="completed", unfinished=None):
        staging = os.path.join(self.base, "staging")
        shutil.rmtree(staging, ignore_errors=True)
        write_file(os.path.join(staging, "START-HERE.md"), "# start\n\nread me\n")
        artifact = b"artifact body\n"
        write_file(os.path.join(staging, "artifacts", "a.py"), artifact)
        write_file(os.path.join(staging, "evidence", "check.txt"), "check passed\n")
        meta = {
            "schema_version": "collaborator-delivery/1",
            "task_id": "package-delivery-011",
            "attempt_id": "package-delivery-011-a1",
            "title": "release fixture",
            "summary": "synthetic delivery for release tests",
            "status": status,
            "independent_acceptance": "UNKNOWN",
            "files": ["START-HERE.md", "artifacts/a.py", "evidence/check.txt"],
            "checks": [],
            "unfinished": list(unfinished or []),
            "requirements": ["Python 3.11+"],
            "next_steps": ["read START-HERE.md"],
        }
        if status != "completed" and not meta["unfinished"]:
            meta["unfinished"] = ["fixture work still open"]
        if include_traceability:
            meta["traceability"] = (
                traceability
                if traceability is not None
                else self.default_traceability(sha256_bytes(artifact))
            )
        write_file(os.path.join(staging, "DELIVERY.json"), json.dumps(meta, ensure_ascii=False))
        archive = os.path.join(self.base, "delivery-%s.zip" % uuid.uuid4().hex[:8])
        self.trusted.pack_delivery(staging, archive)
        return archive

    def default_traceability(self, artifact_sha):
        return {
            "schema_version": "collaborator-traceability/1",
            "criteria": [
                {
                    "id": "AC1",
                    "requirement": "交付包含可追溯工件",
                    "status": "met",
                    "artifacts": ["artifacts/a.py"],
                    "verification_ids": ["V1"],
                    "reason": None,
                }
            ],
            "verifications": [self.verification("V1", artifact_sha)],
            "revision": None,
        }

    def verification(self, vid="V1", artifact_sha=None, **overrides):
        record = {
            "id": vid,
            "kind": "unit",
            "execution": "current",
            "status": "passed",
            "command": ["python", "-m", "unittest"],
            "cwd": ".",
            "environment": {"os": "Windows", "python": "3.11.9"},
            "exit_code": 0,
            "evidence": "evidence/check.txt",
            "subjects": [{"path": "artifacts/a.py", "sha256": artifact_sha}],
            "reason": None,
            "source": None,
        }
        record.update(overrides)
        return record

    def assert_handoff_error(self, code, checker, archive):
        with self.assertRaises(checker.HandoffError) as caught:
            checker.check_handoff(archive)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def assert_install_error(self, code, installer, **kwargs):
        with self.assertRaises(installer.InstallError) as caught:
            installer.install(**kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


# ---------------------------------------------------------------------------
# check_handoff tests
# ---------------------------------------------------------------------------


class CheckHandoffTests(BaseCase):
    def test_roundtrip_and_cli_json(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        archive = self.make_delivery(repo)
        expected_sha = sha256_file(archive)

        result = checker.check_handoff(archive)

        self.assertEqual(
            set(result),
            {"archive_sha256", "criteria_count", "verification_count", "independent_acceptance"},
        )
        self.assertEqual(result["archive_sha256"], expected_sha)
        self.assertEqual(result["criteria_count"], 1)
        self.assertEqual(result["verification_count"], 1)
        self.assertEqual(result["independent_acceptance"], "UNKNOWN")
        self.assertEqual(set(result), set(json.loads(json.dumps(result))))

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = checker.main([archive])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), result)

    def test_missing_traceability_extension(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        archive = self.make_delivery(repo, include_traceability=False)
        self.assert_handoff_error("TRACEABILITY_MISSING", checker, archive)

    def test_dangling_and_duplicate_references(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")

        dangling = self.default_traceability(artifact_sha)
        dangling["criteria"][0]["verification_ids"] = ["V9"]
        archive = self.make_delivery(repo, traceability=dangling)
        self.assert_handoff_error("REFERENCE_DANGLING", checker, archive)

        duplicate = self.default_traceability(artifact_sha)
        duplicate["criteria"][0]["verification_ids"] = ["V1", "V1"]
        archive = self.make_delivery(repo, traceability=duplicate)
        self.assert_handoff_error("REFERENCE_DUPLICATE", checker, archive)

    def test_old_or_stale_subject_hash_rejected(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        traceability = self.default_traceability("0" * 64)
        archive = self.make_delivery(repo, traceability=traceability)
        self.assert_handoff_error("SUBJECT_HASH_MISMATCH", checker, archive)

    def test_completed_delivery_requires_every_criterion_met(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        traceability = self.default_traceability(sha256_bytes(b"artifact body\n"))
        traceability["criteria"][0].update(
            {"status": "unmet", "artifacts": [], "verification_ids": [], "reason": "尚未完成"}
        )
        archive = self.make_delivery(repo, traceability=traceability, status="completed")
        self.assert_handoff_error("COMPLETED_CRITERIA_NOT_MET", checker, archive)

    def test_illegal_status_and_bool_exit_code(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")

        bad_status = self.default_traceability(artifact_sha)
        bad_status["criteria"][0]["status"] = "done"
        archive = self.make_delivery(repo, traceability=bad_status)
        self.assert_handoff_error("CRITERION_STATUS_INVALID", checker, archive)

        bad_exit = self.default_traceability(artifact_sha)
        bad_exit["verifications"][0]["exit_code"] = True
        archive = self.make_delivery(repo, traceability=bad_exit)
        self.assert_handoff_error("EXIT_CODE_INVALID", checker, archive)

    def test_inherited_verification_is_accepted(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")
        inherited = self.verification(
            "V1",
            artifact_sha,
            execution="inherited",
            source="evidence/check.txt",
            reason="继承自上一包，未在本环境重跑",
        )
        traceability = {
            "schema_version": "collaborator-traceability/1",
            "criteria": [
                {
                    "id": "AC1",
                    "requirement": "交付包含可追溯工件",
                    "status": "met",
                    "artifacts": ["artifacts/a.py"],
                    "verification_ids": ["V1"],
                    "reason": None,
                }
            ],
            "verifications": [inherited],
            "revision": None,
        }
        archive = self.make_delivery(repo, traceability=traceability)
        result = checker.check_handoff(archive)
        self.assertEqual(result["verification_count"], 1)

    def test_inherited_cannot_pretend_to_be_current(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")
        bad = self.verification(
            "V1",
            artifact_sha,
            execution="inherited",
            source="evidence/check.txt",
            reason=None,
        )
        traceability = self.default_traceability(artifact_sha)
        traceability["verifications"] = [bad]
        archive = self.make_delivery(repo, traceability=traceability)
        self.assert_handoff_error("CRITERION_REASON_REQUIRED", checker, archive)

    def test_not_run_verification_is_accepted(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        traceability = {
            "schema_version": "collaborator-traceability/1",
            "criteria": [
                {
                    "id": "AC1",
                    "requirement": "尚未运行的外部验证",
                    "status": "not_run",
                    "artifacts": [],
                    "verification_ids": ["V1"],
                    "reason": "等待接收方环境",
                }
            ],
            "verifications": [
                self.verification(
                    "V1",
                    None,
                    kind="live",
                    execution="not_run",
                    status="not_run",
                    command=None,
                    environment=None,
                    exit_code=None,
                    evidence=None,
                    subjects=[],
                    reason="尚未运行",
                    source=None,
                )
            ],
            "revision": None,
        }
        archive = self.make_delivery(repo, traceability=traceability, status="partial")
        result = checker.check_handoff(archive)
        self.assertEqual(result["criteria_count"], 1)

    def test_revision_must_bind_current_not_inherited_evidence(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")
        inherited = self.verification(
            "V0",
            artifact_sha,
            execution="inherited",
            source="evidence/check.txt",
            reason="上一包证据",
        )
        traceability = self.default_traceability(artifact_sha)
        traceability["verifications"].append(inherited)
        traceability["revision"] = {
            "previous_archive_sha256": "a" * 64,
            "changes": [
                {
                    "criterion_id": "AC1",
                    "description": "更新验证",
                    "verification_ids": ["V0"],
                }
            ],
        }
        archive = self.make_delivery(repo, traceability=traceability)
        self.assert_handoff_error("REVISION_INHERITED_REFERENCE", checker, archive)

        traceability["revision"]["changes"][0]["verification_ids"] = ["V1"]
        archive = self.make_delivery(repo, traceability=traceability)
        self.assertEqual(checker.check_handoff(archive)["verification_count"], 2)

    def test_paths_must_be_declared_payload(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")

        bad_artifact = self.default_traceability(artifact_sha)
        bad_artifact["criteria"][0]["artifacts"] = ["artifacts/missing.py"]
        archive = self.make_delivery(repo, traceability=bad_artifact)
        self.assert_handoff_error("PATH_NOT_DECLARED", checker, archive)

        bad_evidence = self.default_traceability(artifact_sha)
        bad_evidence["verifications"][0]["evidence"] = "evidence/missing.txt"
        archive = self.make_delivery(repo, traceability=bad_evidence)
        self.assert_handoff_error("EVIDENCE_INVALID", checker, archive)

        bad_cwd = self.default_traceability(artifact_sha)
        bad_cwd["verifications"][0]["cwd"] = "../outside"
        archive = self.make_delivery(repo, traceability=bad_cwd)
        self.assert_handoff_error("CWD_INVALID", checker, archive)

    def test_trusted_verifier_runs_before_traceability(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        archive = self.make_delivery(repo)
        tampered = os.path.join(self.base, "tampered.zip")

        with zipfile.ZipFile(archive) as handle:
            delivery = json.loads(handle.read("DELIVERY.json").decode("utf-8"))
            manifest = json.loads(handle.read("MANIFEST.json").decode("utf-8"))
            original_start = handle.read("START-HERE.md")
        # Break traceability and make the metadata hash consistent, but leave
        # the payload hash stale: the trusted structural verifier must win.
        delivery.pop("traceability", None)
        new_delivery = json.dumps(delivery, ensure_ascii=False).encode("utf-8")
        for entry in manifest["files"]:
            if entry["path"] == "DELIVERY.json":
                entry["bytes"] = len(new_delivery)
                entry["sha256"] = sha256_bytes(new_delivery)
        new_manifest = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        tampered_start = original_start[:-1] + b"!"
        self.assertNotEqual(tampered_start, original_start)
        rewrite_zip(
            archive,
            tampered,
            replace={
                "DELIVERY.json": new_delivery,
                "MANIFEST.json": new_manifest,
                "START-HERE.md": tampered_start,
            },
        )

        error = self.assert_handoff_error("ARCHIVE_HASH_MISMATCH", checker, tampered)
        self.assertNotIn("traceability", str(error))

    def test_cli_error_is_code_only(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        traceability = self.default_traceability(sha256_bytes(b"artifact body\n"))
        traceability["criteria"][0]["status"] = "TOP-SECRET-MARKER"
        archive = self.make_delivery(repo, traceability=traceability)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = checker.main([archive])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "CRITERION_STATUS_INVALID"})
        self.assertNotIn("TOP-SECRET-MARKER", stderr.getvalue())

    def test_enum_fields_reject_non_string_types(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")
        cases = [
            ("criteria", "status", [], "CRITERION_STATUS_INVALID"),
            ("verifications", "kind", [], "VERIFICATION_KIND_INVALID"),
            ("verifications", "execution", {}, "VERIFICATION_EXECUTION_INVALID"),
            ("verifications", "status", True, "VERIFICATION_STATUS_INVALID"),
        ]
        for section, field, value, expected in cases:
            with self.subTest(field=field):
                traceability = self.default_traceability(artifact_sha)
                traceability[section][0][field] = value
                archive = self.make_delivery(repo, traceability=traceability)
                self.assert_handoff_error(expected, checker, archive)

    def test_non_string_enum_cli_is_fixed_json(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        traceability = self.default_traceability(sha256_bytes(b"artifact body\n"))
        traceability["verifications"][0]["kind"] = []
        archive = self.make_delivery(repo, traceability=traceability)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = checker.main([archive])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "VERIFICATION_KIND_INVALID"})

    def test_met_artifacts_must_be_covered_by_linked_subjects(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")

        unrelated = self.default_traceability(artifact_sha)
        unrelated["criteria"][0]["artifacts"] = ["START-HERE.md"]
        archive = self.make_delivery(repo, traceability=unrelated)
        self.assert_handoff_error("CRITERION_MET_INVALID", checker, archive)

    def test_met_artifact_coverage_uses_union_of_linked_verifications(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")
        start_sha = sha256_bytes(b"# start\n\nread me\n")
        traceability = {
            "schema_version": "collaborator-traceability/1",
            "criteria": [
                {
                    "id": "AC1",
                    "requirement": "两个产物均由关联验证覆盖",
                    "status": "met",
                    "artifacts": ["artifacts/a.py", "START-HERE.md"],
                    "verification_ids": ["V1", "V2"],
                    "reason": None,
                }
            ],
            "verifications": [
                self.verification("V1", artifact_sha),
                self.verification(
                    "V2",
                    artifact_sha,
                    subjects=[{"path": "START-HERE.md", "sha256": start_sha}],
                ),
            ],
            "revision": None,
        }
        archive = self.make_delivery(repo, traceability=traceability)
        self.assertEqual(checker.check_handoff(archive)["criteria_count"], 1)

    def test_unmet_and_not_run_criteria_need_no_subject_coverage(self):
        repo = self.build_repo()
        checker = self.load_checker(repo)
        artifact_sha = sha256_bytes(b"artifact body\n")
        traceability = {
            "schema_version": "collaborator-traceability/1",
            "criteria": [
                {
                    "id": "AC1",
                    "requirement": "尚未满足但列出了目标产物",
                    "status": "unmet",
                    "artifacts": ["START-HERE.md"],
                    "verification_ids": ["V1"],
                    "reason": "仍在实现中",
                },
                {
                    "id": "AC2",
                    "requirement": "尚未运行的验收",
                    "status": "not_run",
                    "artifacts": ["artifacts/a.py"],
                    "verification_ids": [],
                    "reason": "等待接收方环境",
                },
            ],
            "verifications": [self.verification("V1", artifact_sha)],
            "revision": None,
        }
        archive = self.make_delivery(repo, traceability=traceability, status="partial")
        self.assertEqual(checker.check_handoff(archive)["criteria_count"], 2)


# ---------------------------------------------------------------------------
# install.py tests
# ---------------------------------------------------------------------------


class InstallTests(BaseCase):
    def test_first_install_copies_only_manifest_files(self):
        repo = self.build_repo(extra_source={"references/format.md": "format notes\n"})
        installer = self.load_installer(repo)
        source_before = snapshot(self.source_root(repo))
        dest = os.path.join(self.base, "skills", "package-delivery")

        result = installer.install(dest=dest)

        self.assertEqual(
            set(result),
            {"installed_version", "destination", "backup", "dry_run"},
        )
        self.assertEqual(result["installed_version"], "1.1.0")
        self.assertEqual(result["destination"], os.path.abspath(dest))
        self.assertIsNone(result["backup"])
        self.assertFalse(result["dry_run"])

        manifest = self.read_manifest(repo)
        expected = {
            entry["path"][len("package-delivery/"):] for entry in manifest["files"]
        }
        installed = {
            os.path.relpath(os.path.join(base, name), dest).replace(os.sep, "/")
            for base, _dirs, names in os.walk(dest)
            for name in names
        }
        self.assertEqual(installed, expected)
        self.assertNotIn("README.md", installed)
        self.assertNotIn("scripts/install.py", installed)
        self.assertFalse(os.path.exists(os.path.join(dest, "release-manifest.json")))
        for relative in expected:
            with open(os.path.join(dest, *relative.split("/")), "rb") as handle:
                data = handle.read()
            matching = [e for e in manifest["files"] if e["path"].endswith("/" + relative)]
            self.assertTrue(matching)
            self.assertEqual(sha256_bytes(data), matching[0]["sha256"])
        self.assertEqual(snapshot(self.source_root(repo)), source_before)

        leftovers = [
            name for name in os.listdir(os.path.dirname(dest))
            if name.startswith(".package-delivery")
        ]
        self.assertEqual(leftovers, [])

    def test_existing_destination_is_not_overwritten(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        parent = os.path.join(self.base, "skills")
        os.makedirs(parent)
        dest = os.path.join(parent, "package-delivery")

        for marker in (None, b"keep me\n"):
            with self.subTest(marker=marker):
                if marker is None:
                    os.makedirs(dest, exist_ok=True)
                else:
                    write_file(os.path.join(dest, "keep.txt"), marker)
                self.assert_install_error("DEST_EXISTS", installer, dest=dest)
                if marker is None:
                    self.assertEqual(os.listdir(dest), [])
                else:
                    with open(os.path.join(dest, "keep.txt"), "rb") as handle:
                        self.assertEqual(handle.read(), marker)
                if marker is not None:
                    shutil.rmtree(dest)
                else:
                    os.rmdir(dest)

    def test_replace_backs_up_original_bytes(self):
        repo = self.build_repo(skill_body="old skill\n")
        installer = self.load_installer(repo)
        parent = os.path.join(self.base, "skills")
        dest = os.path.join(parent, "package-delivery")
        installer.install(dest=dest)
        old_bytes = read_bytes(os.path.join(dest, "SKILL.md"))

        write_file(os.path.join(self.source_root(repo), "SKILL.md"), "new skill\n")
        write_file(os.path.join(self.source_root(repo), "VERSION"), "1.2.0\n")
        self.refresh_manifest(repo, "1.2.0")

        result = installer.install(dest=dest, replace=True)

        self.assertEqual(result["installed_version"], "1.2.0")
        self.assertEqual(result["destination"], os.path.abspath(dest))
        self.assertIsNotNone(result["backup"])
        backup = result["backup"]
        self.assertEqual(os.path.dirname(backup), os.path.abspath(parent))
        self.assertTrue(os.path.basename(backup).startswith("package-delivery.backup-"))
        self.assertTrue(os.path.isdir(backup))
        with open(os.path.join(backup, "SKILL.md"), "rb") as handle:
            self.assertEqual(handle.read(), old_bytes)
        with open(os.path.join(dest, "SKILL.md"), "rb") as handle:
            self.assertEqual(handle.read(), b"new skill\n")
        self.assertTrue(os.path.exists(backup))
        leftovers = [
            name for name in os.listdir(parent)
            if name.startswith(".package-delivery")
        ]
        self.assertEqual(leftovers, [])

    def test_tampered_source_is_rejected(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        dest = os.path.join(self.base, "skills", "package-delivery")
        skill = os.path.join(self.source_root(repo), "SKILL.md")
        original = read_bytes(skill)
        write_file(skill, b"Z" * len(original))
        self.assertEqual(len(read_bytes(skill)), len(original))
        self.assert_install_error("SOURCE_HASH_MISMATCH", installer, dest=dest)
        self.assertFalse(os.path.exists(dest))

    def test_missing_and_unlisted_source_files_are_rejected(self):
        repo = self.build_repo(extra_source={"references/format.md": "format notes\n"})
        installer = self.load_installer(repo)
        dest = os.path.join(self.base, "skills", "package-delivery")

        write_file(os.path.join(self.source_root(repo), "notes.txt"), "unlisted\n")
        self.assert_install_error("SOURCE_UNLISTED_FILE", installer, dest=dest)
        os.remove(os.path.join(self.source_root(repo), "notes.txt"))

        os.remove(os.path.join(self.source_root(repo), "references", "format.md"))
        self.assert_install_error("SOURCE_FILE_MISSING", installer, dest=dest)

    def test_dry_run_writes_nothing(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        parent = os.path.join(self.base, "new-parent")
        dest = os.path.join(parent, "skills", "package-delivery")

        result = installer.install(dest=dest, dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["installed_version"], "1.1.0")
        self.assertEqual(result["destination"], os.path.abspath(dest))
        self.assertIsNone(result["backup"])
        self.assertFalse(os.path.exists(parent))

        # A dry-run over an existing destination must also be write-free.
        real_dest = os.path.join(self.base, "skills", "package-delivery")
        installer.install(dest=real_dest)
        before = snapshot(real_dest)
        planned = installer.install(dest=real_dest, replace=True, dry_run=True)
        self.assertTrue(planned["dry_run"])
        self.assertIsNotNone(planned["backup"])
        self.assertEqual(snapshot(real_dest), before)
        self.assertFalse(os.path.exists(planned["backup"]))
        self.assertEqual(
            [name for name in os.listdir(os.path.dirname(real_dest)) if name.startswith(".package-delivery")],
            [],
        )

    def test_version_mismatch_and_bad_semver(self):
        repo = self.build_repo(version="1.1.0")
        installer = self.load_installer(repo)
        dest = os.path.join(self.base, "skills", "package-delivery")
        write_file(os.path.join(self.source_root(repo), "VERSION"), "1.0.0\n")
        self.refresh_manifest(repo, "1.1.0")
        self.assert_install_error("VERSION_MISMATCH", installer, dest=dest)

        bad_repo = self.build_repo(manifest_version="1.1")
        bad_installer = self.load_installer(bad_repo)
        self.assert_install_error(
            "MANIFEST_INVALID", bad_installer, dest=os.path.join(self.base, "other", "package-delivery")
        )

    def test_manifest_path_rules(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        dest = os.path.join(self.base, "skills", "package-delivery")
        manifest = self.read_manifest(repo)
        original = json.loads(json.dumps(manifest))

        alias = json.loads(json.dumps(original))
        alias["files"].append(
            {"path": "package-delivery/skill.md", "bytes": 1, "sha256": "0" * 64}
        )
        self.write_manifest(repo, "1.1.0", alias["files"])
        self.assert_install_error("MANIFEST_DUPLICATE_PATH", installer, dest=dest)

        secret = json.loads(json.dumps(original))
        secret["files"].append(
            {"path": "package-delivery/.env", "bytes": 1, "sha256": "0" * 64}
        )
        self.write_manifest(repo, "1.1.0", secret["files"])
        self.assert_install_error("PATH_SECRET", installer, dest=dest)

        self.write_manifest(repo, "1.1.0", original["files"])

    def test_links_and_junctions_are_rejected(self):
        if not hasattr(os, "symlink") and os.name != "nt":
            self.skipTest("links unavailable")
        repo = self.build_repo()
        installer = self.load_installer(repo)
        dest = os.path.join(self.base, "skills", "package-delivery")

        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not make_directory_link(dest, outside):
            self.skipTest("cannot create a directory link here")
        self.assert_install_error("DEST_LINK", installer, dest=dest)
        remove_directory_link(dest)

        linked_parent = os.path.join(self.base, "linked-parent")
        if not make_directory_link(linked_parent, os.path.dirname(dest)):
            self.skipTest("cannot create the ancestor link here")
        self.assert_install_error(
            "DEST_ANCESTOR_LINK", installer, dest=os.path.join(linked_parent, "package-delivery")
        )

        source_link = os.path.join(self.source_root(repo), "linked-extra")
        if not make_directory_link(source_link, outside):
            self.skipTest("cannot create the source link here")
        self.assert_install_error("SOURCE_ENTRY_LINK", installer, dest=dest)

    def test_destination_inside_or_above_source_rejected(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        inside = os.path.join(self.source_root(repo), "installed")
        self.assert_install_error("DEST_INSIDE_SOURCE", installer, dest=inside)
        self.assertFalse(os.path.exists(inside))

        self.assert_install_error("DEST_INSIDE_SOURCE", installer, dest=repo)
        self.assert_install_error("DEST_ANCESTOR_OF_SOURCE", installer, dest=self.base)

    def test_lock_is_not_stolen(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        dest = os.path.join(self.base, "skills", "package-delivery")
        parent = os.path.dirname(dest)
        os.makedirs(parent)
        lock = os.path.join(parent, ".package-delivery.install.lock")
        write_file(lock, "someone-else\n")

        self.assert_install_error("LOCKED", installer, dest=dest)
        self.assertTrue(os.path.exists(lock))
        with open(lock, "rb") as handle:
            self.assertEqual(handle.read(), b"someone-else\n")
        self.assertFalse(os.path.exists(dest))

    def test_codex_home_selects_default_destination(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        codex_home = os.path.join(self.base, "codex-home")
        previous = os.environ.get("CODEX_HOME")
        try:
            os.environ["CODEX_HOME"] = codex_home
            result = installer.install(dry_run=True)
        finally:
            if previous is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous
        self.assertEqual(
            result["destination"],
            os.path.abspath(os.path.join(codex_home, "skills", "package-delivery")),
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse(os.path.exists(codex_home))

    def test_cli_success_and_error_json(self):
        repo = self.build_repo()
        installer = self.load_installer(repo)
        dest = os.path.join(self.base, "skills", "package-delivery")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = installer.main(["--dest", dest])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["installed_version"], "1.1.0")
        self.assertIsNone(payload["backup"])
        self.assertFalse(payload["dry_run"])

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = installer.main(["--dest", dest])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "DEST_EXISTS"})
        self.assertNotIn("Fixture skill", stderr.getvalue())


def main():
    global _CHECKER_SRC, _INSTALLER_SRC, _TRUSTED_SRC

    parser = argparse.ArgumentParser(description="Run package-delivery release tests.")
    parser.add_argument("--checker", default=_DEFAULT_CHECKER, help="path to check_handoff.py")
    parser.add_argument("--installer", default=_DEFAULT_INSTALLER, help="path to install.py")
    parser.add_argument(
        "--delivery",
        default=None,
        help="path to the trusted delivery.py (defaults to a sibling or inputs/delivery.py)",
    )
    args, remaining = parser.parse_known_args()

    _CHECKER_SRC = os.path.abspath(args.checker)
    _INSTALLER_SRC = os.path.abspath(args.installer)
    candidates = []
    if args.delivery:
        candidates.append(os.path.abspath(args.delivery))
    candidates.append(os.path.join(os.path.dirname(_CHECKER_SRC), "delivery.py"))
    candidates.append(
        os.path.join(
            os.path.dirname(_INSTALLER_SRC), "..", "package-delivery", "scripts", "delivery.py"
        )
    )
    candidates.append(os.path.abspath(os.path.join(_HERE, "..", "inputs", "delivery.py")))
    candidates.append(os.path.abspath(os.path.join(os.getcwd(), "inputs", "delivery.py")))
    for candidate in candidates:
        if os.path.isfile(candidate):
            _TRUSTED_SRC = os.path.abspath(candidate)
            break
    if _TRUSTED_SRC is None:
        raise SystemExit("could not locate the trusted delivery.py; pass --delivery <path>")
    for path in (_CHECKER_SRC, _INSTALLER_SRC):
        if not os.path.isfile(path):
            raise SystemExit("script not found: %s" % path)

    unittest.main(argv=[sys.argv[0]] + remaining)


if __name__ == "__main__":
    main()
