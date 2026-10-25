"""Install the local, already-unpacked ``package-delivery`` skill.

Final location: ``<repo>/scripts/install.py``.  The source skill lives in
``<repo>/package-delivery/`` and the coordinator-generated manifest is
``<repo>/release-manifest.json``.  The trusted path/safety helper is imported
from ``../package-delivery/scripts/delivery.py`` (the accepted helper), so this
script reuses the audited path rules instead of growing a second framework.

Usage::

    python scripts/install.py [--client codex|claude] [--dest <dir>]
                              [--replace] [--dry-run]

Only files listed in ``release-manifest.json`` and rooted at
``package-delivery/`` are copied.  Missing or extra source files, unsafe,
duplicate (case-insensitively) or obvious-secret paths, links/junctions,
special files, and size/digest mismatches are refused.  The required
``VERSION`` file must equal the manifest version, which must be ``N.N.N``.

The installer never touches the network, user environment variables, system
configuration, the registry, or execution policy; it never executes the skill
it copies.  It refuses to overwrite an existing destination unless
``--replace`` is given.  Replacement first renames the old destination to a
unique sibling ``package-delivery.backup-<id>`` and only then moves a fully
copied and re-verified temporary tree into place.  A failure removes only the
installer's own temporary directory/lock and restores the backup when it can;
it never deletes the original directory or the backup.

Successful JSON::

    {"installed_version": "1.1.0", "destination": "<abs path>",
     "backup": null|"<abs path>", "dry_run": false}

Failure codes (``InstallError.code``) are fixed strings; the CLI prints only
the code (never file contents) and exits non-zero.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import uuid
from pathlib import Path

__all__ = ["InstallError", "install", "main"]

_SOURCE_DIR = "package-delivery"
_HELPER_REL = "package-delivery/scripts/delivery.py"
_SKILL_REL = "package-delivery/SKILL.md"
_VERSION_REL = "package-delivery/VERSION"

_SEMVER_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_SHA_RE = re.compile(r"[0-9a-f]{64}")

_CHUNK = 64 * 1024
_MAX_VERSION_BYTES = 4096


class InstallError(Exception):
    """Fixed-code failure raised by the installer."""

    ARGS_INVALID = "ARGS_INVALID"
    HELPER_MISSING = "HELPER_MISSING"
    HELPER_INVALID = "HELPER_INVALID"
    HELPER_UNLISTED = "HELPER_UNLISTED"
    HELPER_HASH_MISMATCH = "HELPER_HASH_MISMATCH"
    MANIFEST_MISSING = "MANIFEST_MISSING"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    MANIFEST_TOO_LARGE = "MANIFEST_TOO_LARGE"
    MANIFEST_DUPLICATE_PATH = "MANIFEST_DUPLICATE_PATH"
    MANIFEST_PATH_INVALID = "MANIFEST_PATH_INVALID"
    SKILL_MISSING = "SKILL_MISSING"
    VERSION_MISSING = "VERSION_MISSING"
    VERSION_INVALID = "VERSION_INVALID"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    SOURCE_ROOT_INVALID = "SOURCE_ROOT_INVALID"
    SOURCE_ENTRY_LINK = "SOURCE_ENTRY_LINK"
    SOURCE_ENTRY_NOT_REGULAR = "SOURCE_ENTRY_NOT_REGULAR"
    SOURCE_FILE_MISSING = "SOURCE_FILE_MISSING"
    SOURCE_UNLISTED_FILE = "SOURCE_UNLISTED_FILE"
    SOURCE_CASE_ALIAS = "SOURCE_CASE_ALIAS"
    SOURCE_SIZE_MISMATCH = "SOURCE_SIZE_MISMATCH"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    SOURCE_LIMIT = "SOURCE_LIMIT"
    DEST_INVALID = "DEST_INVALID"
    DEST_EXISTS = "DEST_EXISTS"
    DEST_NOT_DIRECTORY = "DEST_NOT_DIRECTORY"
    DEST_LINK = "DEST_LINK"
    DEST_ANCESTOR_LINK = "DEST_ANCESTOR_LINK"
    DEST_PARENT_NOT_DIRECTORY = "DEST_PARENT_NOT_DIRECTORY"
    DEST_INSIDE_SOURCE = "DEST_INSIDE_SOURCE"
    DEST_ANCESTOR_OF_SOURCE = "DEST_ANCESTOR_OF_SOURCE"
    LOCKED = "LOCKED"
    BACKUP_FAILED = "BACKUP_FAILED"
    COPY_FAILED = "COPY_FAILED"
    INSTALL_FAILED = "INSTALL_FAILED"
    IO_ERROR = "IO_ERROR"

    def __init__(self, code: str, message: str = "", backup=None) -> None:
        self.code = code
        self.backup = backup
        super().__init__(message or code)


# ---------------------------------------------------------------------------
# Small local helpers (not a second path framework)
# ---------------------------------------------------------------------------


def _stat_is_link(st) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt":
        attrs = getattr(st, "st_file_attributes", 0) or 0
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attrs & reparse:
            return True
    return False


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _strict_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _loads(data: bytes, code: str):
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InstallError(code, "JSON is invalid") from None


def _read_bounded(path: str, limit: int) -> bytes:
    try:
        st = os.lstat(path)
    except OSError:
        raise InstallError(InstallError.MANIFEST_MISSING, "manifest is missing") from None
    if _stat_is_link(st):
        raise InstallError(InstallError.MANIFEST_INVALID, "manifest must not be a link")
    if not stat.S_ISREG(st.st_mode):
        raise InstallError(InstallError.MANIFEST_INVALID, "manifest must be a file")
    if st.st_size > limit:
        raise InstallError(InstallError.MANIFEST_TOO_LARGE, "manifest is too large")
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise InstallError(InstallError.IO_ERROR, "could not read the manifest") from None
    if len(data) > limit:
        raise InstallError(InstallError.MANIFEST_TOO_LARGE, "manifest is too large")
    return data


def _digest_file(path: str, limit: int) -> tuple:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise InstallError(InstallError.SOURCE_LIMIT, "file exceeds the limit")
                digest.update(chunk)
    except InstallError:
        raise
    except OSError:
        raise InstallError(InstallError.IO_ERROR, "could not read a source file") from None
    return size, digest.hexdigest()


def _normalise(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _same_or_child(child: str, parent: str) -> bool:
    child = _normalise(child)
    parent = _normalise(parent)
    if child == parent:
        return True
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def _is_ancestor(ancestor: str, child: str) -> bool:
    return _normalise(ancestor) != _normalise(child) and _same_or_child(child, ancestor)


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


# ---------------------------------------------------------------------------
# Trusted helper and manifest
# ---------------------------------------------------------------------------


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_helper(path: str):
    if not os.path.isfile(path):
        raise InstallError(InstallError.HELPER_MISSING, "trusted delivery.py is missing")
    spec = importlib.util.spec_from_file_location(
        "_package_delivery_trusted_for_install", path
    )
    if spec is None or spec.loader is None:
        raise InstallError(InstallError.HELPER_INVALID, "could not import delivery.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise InstallError(InstallError.HELPER_INVALID, "could not import delivery.py") from None
    finally:
        sys.dont_write_bytecode = previous
    for attribute in ("DEFAULT_LIMITS", "DeliveryError", "_validate_rel_path", "_path_key", "_is_link_like"):
        if not hasattr(module, attribute):
            raise InstallError(
                InstallError.HELPER_INVALID, "trusted helper does not expose the needed rules"
            )
    return module


def _minimal_manifest(data: bytes) -> dict:
    obj = _loads(data, InstallError.MANIFEST_INVALID)
    if not isinstance(obj, dict) or set(obj) != {"version", "files"}:
        raise InstallError(InstallError.MANIFEST_INVALID, "bad manifest object")
    if not isinstance(obj.get("version"), str) or obj["version"].strip() == "":
        raise InstallError(InstallError.MANIFEST_INVALID, "manifest version is invalid")
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        raise InstallError(InstallError.MANIFEST_INVALID, "manifest files must be non-empty")
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise InstallError(InstallError.MANIFEST_INVALID, "bad manifest entry")
        path = item.get("path")
        size = item.get("bytes")
        digest = item.get("sha256")
        if not isinstance(path, str):
            raise InstallError(InstallError.MANIFEST_INVALID, "bad manifest path")
        if type(size) is not int or size < 0:
            raise InstallError(InstallError.MANIFEST_INVALID, "bad manifest size")
        if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
            raise InstallError(InstallError.MANIFEST_INVALID, "bad manifest digest")
    return obj


def _verify_listed_helper(obj: dict, source_root: str) -> str:
    matches = [entry for entry in obj["files"] if entry["path"] == _HELPER_REL]
    if not matches:
        raise InstallError(
            InstallError.HELPER_UNLISTED, "the trusted helper must be listed exactly once"
        )
    if len(matches) > 1:
        raise InstallError(
            InstallError.MANIFEST_DUPLICATE_PATH, "the trusted helper is listed more than once"
        )
    entry = matches[0]
    helper = os.path.join(source_root, "scripts", "delivery.py")
    try:
        st = os.lstat(helper)
    except OSError:
        raise InstallError(InstallError.HELPER_MISSING, "trusted delivery.py is missing") from None
    if _stat_is_link(st) or not stat.S_ISREG(st.st_mode):
        raise InstallError(InstallError.HELPER_MISSING, "trusted delivery.py is not a regular file")
    if st.st_size != entry["bytes"]:
        raise InstallError(
            InstallError.HELPER_HASH_MISMATCH, "the trusted helper does not match the manifest"
        )
    size, digest = _digest_file(helper, entry["bytes"] + 1)
    if size != entry["bytes"] or digest != entry["sha256"]:
        raise InstallError(
            InstallError.HELPER_HASH_MISMATCH, "the trusted helper does not match the manifest"
        )
    return helper


def _validate_manifest(obj: dict, source_root: str, delivery) -> tuple:
    version = obj["version"]
    if _SEMVER_RE.fullmatch(version) is None:
        raise InstallError(InstallError.MANIFEST_INVALID, "manifest version is not N.N.N")

    limits = delivery.DEFAULT_LIMITS
    entries = []
    seen_exact = set()
    seen_keys = set()
    total = 0
    for item in obj["files"]:
        path = item["path"]
        try:
            segments = delivery._validate_rel_path(path)
        except delivery.DeliveryError as exc:
            raise InstallError(exc.code, "manifest path failed the trusted path rules") from None
        if len(segments) < 2 or segments[0] != _SOURCE_DIR:
            raise InstallError(
                InstallError.MANIFEST_PATH_INVALID,
                "manifest paths must stay under package-delivery/",
            )
        key = delivery._path_key(path)
        if path in seen_exact or key in seen_keys:
            raise InstallError(
                InstallError.MANIFEST_DUPLICATE_PATH, "duplicate manifest path"
            )
        seen_exact.add(path)
        seen_keys.add(key)

        size = item["bytes"]
        digest = item["sha256"]
        if type(size) is not int or size < 0:
            raise InstallError(InstallError.MANIFEST_INVALID, "bad manifest size")
        if _SHA_RE.fullmatch(digest) is None:
            raise InstallError(InstallError.MANIFEST_INVALID, "bad manifest digest")
        if size > limits["max_file_bytes"]:
            raise InstallError(InstallError.SOURCE_LIMIT, "a listed file exceeds the limit")
        total += size
        if total > limits["max_total_bytes"]:
            raise InstallError(InstallError.SOURCE_LIMIT, "the source exceeds the total limit")

        rel = "/".join(segments[1:])
        if rel == "VERSION" and size > _MAX_VERSION_BYTES:
            raise InstallError(InstallError.VERSION_INVALID, "VERSION is unreasonably large")
        entries.append(
            {"path": path, "rel": rel, "bytes": size, "sha256": digest}
        )

    if len(entries) > limits["max_entries"]:
        raise InstallError(InstallError.SOURCE_LIMIT, "too many listed files")
    paths = {entry["path"] for entry in entries}
    if _SKILL_REL not in paths:
        raise InstallError(InstallError.SKILL_MISSING, "manifest must list SKILL.md")
    if _VERSION_REL not in paths:
        raise InstallError(InstallError.VERSION_MISSING, "manifest must list VERSION")
    if _HELPER_REL not in paths:
        raise InstallError(InstallError.HELPER_UNLISTED, "manifest must list the helper")
    return version, entries


# ---------------------------------------------------------------------------
# Source tree validation
# ---------------------------------------------------------------------------


def _walk_source(root: str, delivery, limits) -> dict:
    if not os.path.lexists(root):
        raise InstallError(InstallError.SOURCE_ROOT_INVALID, "source skill directory is missing")
    try:
        st = os.lstat(root)
    except OSError:
        raise InstallError(InstallError.SOURCE_ROOT_INVALID, "source is not accessible") from None
    if delivery._is_link_like(root, st) or not stat.S_ISDIR(st.st_mode):
        raise InstallError(InstallError.SOURCE_ROOT_INVALID, "source must be a real directory")

    files = {}
    count = 0
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            raise InstallError(InstallError.IO_ERROR, "could not scan the source") from None
        for entry in entries:
            full = entry.path
            try:
                item_st = os.lstat(full)
            except OSError:
                raise InstallError(InstallError.IO_ERROR, "could not stat the source") from None
            if delivery._is_link_like(full, item_st):
                raise InstallError(
                    InstallError.SOURCE_ENTRY_LINK, "source contains a link or junction"
                )
            rel = prefix + entry.name
            if stat.S_ISDIR(item_st.st_mode):
                stack.append((full, rel + "/"))
            elif stat.S_ISREG(item_st.st_mode):
                count += 1
                if count > limits["max_entries"]:
                    raise InstallError(InstallError.SOURCE_LIMIT, "too many source files")
                files[rel] = full
            else:
                raise InstallError(
                    InstallError.SOURCE_ENTRY_NOT_REGULAR,
                    "source contains a special file",
                )
    return files


def _compare_tree(files: dict, entries, delivery) -> None:
    actual_keys = {}
    for rel in files:
        key = delivery._path_key(rel)
        if key in actual_keys:
            raise InstallError(
                InstallError.SOURCE_CASE_ALIAS, "source has case-aliased paths"
            )
        actual_keys[key] = rel

    expected_keys = {delivery._path_key(entry["rel"]): entry for entry in entries}
    for key, entry in expected_keys.items():
        actual = actual_keys.get(key)
        if actual is None:
            raise InstallError(
                InstallError.SOURCE_FILE_MISSING, "a listed source file is missing"
            )
        if actual != entry["rel"]:
            raise InstallError(
                InstallError.SOURCE_CASE_ALIAS, "source path case does not match the manifest"
            )
    for key in actual_keys:
        if key not in expected_keys:
            raise InstallError(
                InstallError.SOURCE_UNLISTED_FILE, "source has an unlisted file"
            )


def _hash_file(path: str, limits, total: dict, capture: bool = False) -> tuple:
    digest = hashlib.sha256()
    size = 0
    collected = bytearray()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limits["max_file_bytes"]:
                    raise InstallError(InstallError.SOURCE_LIMIT, "file exceeds the limit")
                total["value"] += len(chunk)
                if total["value"] > limits["max_total_bytes"]:
                    raise InstallError(InstallError.SOURCE_LIMIT, "source exceeds the total limit")
                digest.update(chunk)
                if capture and len(collected) < _MAX_VERSION_BYTES:
                    collected.extend(chunk[: _MAX_VERSION_BYTES - len(collected)])
    except InstallError:
        raise
    except OSError:
        raise InstallError(InstallError.IO_ERROR, "could not read a source file") from None
    return digest.hexdigest(), size, bytes(collected) if capture else None


def _scan_source_contents(files: dict, entries, delivery, limits) -> bytes:
    total = {"value": 0}
    version_bytes = None
    for entry in sorted(entries, key=lambda item: item["rel"]):
        path = files[entry["rel"]]
        try:
            st = os.lstat(path)
        except OSError:
            raise InstallError(InstallError.IO_ERROR, "could not stat a source file") from None
        if delivery._is_link_like(path, st) or not stat.S_ISREG(st.st_mode):
            raise InstallError(
                InstallError.SOURCE_ENTRY_NOT_REGULAR, "a listed source file is not regular"
            )
        if st.st_size != entry["bytes"]:
            raise InstallError(
                InstallError.SOURCE_SIZE_MISMATCH, "a source file size does not match"
            )
        digest, size, captured = _hash_file(
            path,
            limits,
            total,
            capture=entry["rel"] == "VERSION",
        )
        if size != entry["bytes"]:
            raise InstallError(
                InstallError.SOURCE_SIZE_MISMATCH, "a source file size does not match"
            )
        if digest != entry["sha256"]:
            raise InstallError(
                InstallError.SOURCE_HASH_MISMATCH, "a source file digest does not match"
            )
        if captured is not None:
            version_bytes = captured
    if version_bytes is None:
        raise InstallError(InstallError.VERSION_MISSING, "VERSION was not read")
    return version_bytes


def _check_version(version: str, raw: bytes) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InstallError(InstallError.VERSION_INVALID, "VERSION is not UTF-8") from None
    if text.strip() != version:
        raise InstallError(
            InstallError.VERSION_MISMATCH, "VERSION does not match the manifest version"
        )


# ---------------------------------------------------------------------------
# Destination handling
# ---------------------------------------------------------------------------


def _resolve_destination(client: str, dest) -> str:
    if dest is not None:
        try:
            raw = os.fspath(dest)
        except TypeError:
            raise InstallError(InstallError.DEST_INVALID, "destination must be a path") from None
        if not isinstance(raw, str) or raw.strip() == "":
            raise InstallError(InstallError.DEST_INVALID, "destination must be a path")
        return os.path.abspath(raw)

    if client == "codex":
        codex_home = os.environ.get("CODEX_HOME", "")
        if not isinstance(codex_home, str):
            codex_home = ""
        codex_home = codex_home.strip()
        if codex_home:
            base = os.path.join(codex_home, "skills")
        else:
            base = str(Path.home() / ".codex" / "skills")
    else:
        base = str(Path.home() / ".claude" / "skills")
    return os.path.abspath(os.path.join(base, "package-delivery"))


def _check_ancestors(parent: str, delivery) -> None:
    current = parent
    while True:
        if os.path.lexists(current):
            try:
                st = os.lstat(current)
            except OSError:
                raise InstallError(InstallError.IO_ERROR, "could not stat the destination parent") from None
            if delivery._is_link_like(current, st):
                raise InstallError(
                    InstallError.DEST_ANCESTOR_LINK, "a destination ancestor is a link"
                )
            if not stat.S_ISDIR(st.st_mode):
                raise InstallError(
                    InstallError.DEST_PARENT_NOT_DIRECTORY,
                    "a destination ancestor is not a directory",
                )
        upward = os.path.dirname(current)
        if upward == current:
            break
        current = upward


def _check_target_type(dest: str, delivery) -> bool:
    if not os.path.lexists(dest):
        return False
    try:
        st = os.lstat(dest)
    except OSError:
        raise InstallError(InstallError.IO_ERROR, "could not stat the destination") from None
    if delivery._is_link_like(dest, st):
        raise InstallError(InstallError.DEST_LINK, "destination must not be a link")
    if not stat.S_ISDIR(st.st_mode):
        raise InstallError(
            InstallError.DEST_NOT_DIRECTORY, "destination must be an ordinary directory"
        )
    return True


def _validate_destination(dest: str, repo_root: str, source_root: str, delivery, replace: bool) -> bool:
    parent = os.path.dirname(dest)
    if not parent or parent == dest or os.path.basename(dest) in ("", ".", ".."):
        raise InstallError(InstallError.DEST_INVALID, "destination is not a usable directory")
    _check_ancestors(parent, delivery)
    exists = _check_target_type(dest, delivery)

    dest_real = os.path.realpath(dest)
    repo_real = os.path.realpath(repo_root)
    source_real = os.path.realpath(source_root)
    if _same_or_child(dest_real, repo_real):
        raise InstallError(
            InstallError.DEST_INSIDE_SOURCE, "destination must not be inside the source repository"
        )
    if _is_ancestor(dest_real, repo_real) or _is_ancestor(dest_real, source_real):
        raise InstallError(
            InstallError.DEST_ANCESTOR_OF_SOURCE,
            "destination must not be an ancestor of the source repository",
        )

    if exists and not replace:
        raise InstallError(InstallError.DEST_EXISTS, "destination exists; use --replace")
    return exists


def _backup_candidate(dest: str) -> str:
    parent = os.path.dirname(dest)
    for _ in range(64):
        candidate = os.path.join(parent, "package-delivery.backup-%s" % uuid.uuid4().hex[:12])
        if not os.path.lexists(candidate):
            return candidate
    raise InstallError(InstallError.BACKUP_FAILED, "could not allocate a backup name")


class _Lock:
    def __init__(self, path: str, identity, token: bytes) -> None:
        self.path = path
        self.identity = identity
        self.token = token

    def release(self) -> None:
        try:
            st = os.lstat(self.path)
        except OSError:
            return
        if (st.st_dev, st.st_ino) != self.identity:
            return
        try:
            with open(self.path, "rb") as handle:
                data = handle.read(64)
        except OSError:
            return
        if data != self.token:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass


def _acquire_lock(parent: str, dest_name: str) -> _Lock:
    lock_path = os.path.join(parent, ".%s.install.lock" % dest_name)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise InstallError(InstallError.LOCKED, "another install holds the destination lock") from None
        raise InstallError(InstallError.INSTALL_FAILED, "could not create the lock") from None
    identity = os.fstat(descriptor)
    token = uuid.uuid4().hex.encode("ascii")
    try:
        os.write(descriptor, token)
    finally:
        os.close(descriptor)
    return _Lock(lock_path, (identity.st_dev, identity.st_ino), token)


# ---------------------------------------------------------------------------
# Copy and install
# ---------------------------------------------------------------------------


def _copy_tree(temp_root: str, files: dict, entries, delivery, limits) -> None:
    total = {"value": 0}
    for entry in sorted(entries, key=lambda item: item["rel"]):
        source = files[entry["rel"]]
        try:
            st = os.lstat(source)
        except OSError:
            raise InstallError(InstallError.IO_ERROR, "could not stat a source file") from None
        if delivery._is_link_like(source, st) or not stat.S_ISREG(st.st_mode):
            raise InstallError(
                InstallError.SOURCE_ENTRY_NOT_REGULAR, "a source file is not regular"
            )
        if st.st_size != entry["bytes"]:
            raise InstallError(
                InstallError.SOURCE_SIZE_MISMATCH, "a source file size changed"
            )
        target = os.path.join(temp_root, *entry["rel"].split("/"))
        directory = os.path.dirname(target)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            raise InstallError(InstallError.COPY_FAILED, "could not create the copy tree") from None

        digest = hashlib.sha256()
        size = 0
        try:
            with open(source, "rb") as reader, open(target, "xb") as writer:
                while True:
                    chunk = reader.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limits["max_file_bytes"]:
                        raise InstallError(InstallError.SOURCE_LIMIT, "file exceeds the limit")
                    total["value"] += len(chunk)
                    if total["value"] > limits["max_total_bytes"]:
                        raise InstallError(InstallError.SOURCE_LIMIT, "source exceeds the total limit")
                    digest.update(chunk)
                    writer.write(chunk)
        except InstallError:
            raise
        except OSError:
            raise InstallError(InstallError.COPY_FAILED, "could not copy a skill file") from None
        if size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise InstallError(
                InstallError.SOURCE_HASH_MISMATCH, "a source file changed while copying"
            )


def _verify_temp(temp_root: str, entries, delivery, limits) -> None:
    files = _walk_source(temp_root, delivery, limits)
    _compare_tree(files, entries, delivery)
    total = {"value": 0}
    for entry in sorted(entries, key=lambda item: item["rel"]):
        digest, size, _ = _hash_file(files[entry["rel"]], limits, total)
        if size != entry["bytes"] or digest != entry["sha256"]:
            raise InstallError(
                InstallError.COPY_FAILED, "the temporary copy did not verify"
            )


def install(*, client: str = "codex", dest=None, replace: bool = False, dry_run: bool = False) -> dict:
    """Install the local skill and return the public result dictionary."""
    if client not in ("codex", "claude"):
        raise InstallError(InstallError.ARGS_INVALID, "client must be codex or claude")
    if not isinstance(replace, bool) or not isinstance(dry_run, bool):
        raise InstallError(InstallError.ARGS_INVALID, "replace/dry_run must be booleans")

    repo_root = _repo_root()
    source_root = os.path.join(repo_root, _SOURCE_DIR)
    manifest_path = os.path.join(repo_root, "release-manifest.json")

    limits = None  # filled after the trusted helper is loaded
    manifest_bytes = _read_bounded(manifest_path, 512 * 1024)
    manifest = _minimal_manifest(manifest_bytes)
    helper_path = _verify_listed_helper(manifest, source_root)
    delivery = _load_helper(helper_path)
    limits = delivery.DEFAULT_LIMITS

    version, entries = _validate_manifest(manifest, source_root, delivery)
    files = _walk_source(source_root, delivery, limits)
    _compare_tree(files, entries, delivery)
    version_bytes = _scan_source_contents(files, entries, delivery, limits)
    _check_version(version, version_bytes)

    destination = _resolve_destination(client, dest)
    exists = _validate_destination(destination, repo_root, source_root, delivery, replace)
    planned_backup = _backup_candidate(destination) if exists else None

    if dry_run:
        return {
            "installed_version": version,
            "destination": destination,
            "backup": planned_backup,
            "dry_run": True,
        }

    parent = os.path.dirname(destination)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        raise InstallError(InstallError.INSTALL_FAILED, "could not create the destination parent") from None

    lock = _acquire_lock(parent, os.path.basename(destination))
    temp_root = None
    backup = None
    try:
        exists_now = _check_target_type(destination, delivery)
        if exists_now and not replace:
            raise InstallError(InstallError.DEST_EXISTS, "destination exists; use --replace")
        if exists_now:
            backup = _backup_candidate(destination)

        try:
            temp_root = tempfile.mkdtemp(
                prefix=".%s.tmp-" % os.path.basename(destination), dir=parent
            )
        except OSError:
            raise InstallError(InstallError.INSTALL_FAILED, "could not create a temporary tree") from None

        _copy_tree(temp_root, files, entries, delivery, limits)
        _verify_temp(temp_root, entries, delivery, limits)

        if exists_now:
            try:
                os.rename(destination, backup)
            except OSError:
                raise InstallError(InstallError.BACKUP_FAILED, "could not move the old destination") from None

        try:
            os.rename(temp_root, destination)
        except OSError:
            restored = False
            if exists_now and backup is not None:
                try:
                    os.rename(backup, destination)
                    restored = True
                except OSError:
                    restored = False
            raise InstallError(
                InstallError.INSTALL_FAILED,
                "could not move the new skill into place",
                backup=None if restored or not exists_now else backup,
            ) from None
        temp_root = None

        return {
            "installed_version": version,
            "destination": destination,
            "backup": backup,
            "dry_run": False,
        }
    finally:
        if temp_root is not None and os.path.lexists(temp_root):
            try:
                shutil.rmtree(temp_root)
            except OSError:
                pass
        lock.release()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):  # noqa: D401 - argparse hook
        raise InstallError(InstallError.ARGS_INVALID, "invalid arguments")


def main(argv=None) -> int:
    parser = _ArgumentParser(
        prog="install.py",
        description="Install the local package-delivery skill (stdlib only).",
    )
    parser.add_argument("--client", choices=("codex", "claude"), default="codex")
    parser.add_argument("--dest", default=None, help="final skill directory")
    parser.add_argument("--replace", action="store_true", help="replace an existing skill")
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    try:
        args = parser.parse_args(argv)
        result = install(
            client=args.client,
            dest=args.dest,
            replace=args.replace,
            dry_run=args.dry_run,
        )
    except InstallError as exc:
        payload = {"error": exc.code}
        if exc.backup:
            payload["backup"] = str(exc.backup)
        print(json.dumps(payload, separators=(",", ":")), file=sys.stderr)
        return 1
    except OSError:
        print(
            json.dumps({"error": InstallError.IO_ERROR}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
