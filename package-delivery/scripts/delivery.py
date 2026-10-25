"""Deterministic pack/verify helper for "collaborator-delivery/1" packages.

This is a small, portable Python 3.11+ standard-library tool implementing the
``package-delivery`` skill's delivery format (see
``references/delivery-format.md``).  It only reads the declared whitelist in
``DELIVERY.json`` and the files named there; it never walks or scans a project
tree.

Usage::

    python delivery.py pack --root <staging-dir> --out <new.zip> \
        [--profile standard|large] [--compression deflated|stored]
    python delivery.py verify <zip> [--profile standard|large]

Public API::

    pack_delivery(root, out, *, limits=None, profile="standard",
                  compression="deflated") -> dict
    verify_delivery(archive, *, limits=None, profile="standard") -> dict
    check_handoff(archive, *, profile="standard")  # in check_handoff.py
    get_limits(profile="standard", limits=None) -> dict
    open_bounded_archive(path, *, limits=None, profile="standard")
    DeliveryError.code  # fixed, machine-readable failure code

Both successful pack/verify calls return the same stats dict::

    {"archive_sha256": "<hex>", "files": <zip entry count>,
     "total_bytes": <uncompressed bytes of every entry>}

``profile`` is exactly ``"standard"`` or ``"large"``.  ``standard`` keeps the
historical caps; ``large`` uses ``LARGE_LIMITS`` (8 GiB per file and 16 GiB
total) but keeps the same entry, metadata, manifest, and central-directory
caps.  There is deliberately no automatic profile inference: a receiver must
pass ``profile="large"`` explicitly to accept a large package.

``compression`` is exactly ``"deflated"`` or ``"stored"`` for packing.  Stored
avoids recompressing already-compressed engineering resources; deflated is the
backward-compatible default.

``limits`` may be a mapping that only *tightens* the caps of the selected
profile, which is useful for small fixture tests.  It must contain integer (not
bool) values in ``0 <= value <= selected_profile[key]``; unknown keys are
rejected.  Raising a cap, including raising a standard cap toward a large cap,
is rejected rather than silently switching profiles.

``open_bounded_archive`` is the shared bounded reader used by
``verify_delivery`` and the traceability checker.  Before constructing
``zipfile.ZipFile`` it parses the EOCD (or ZIP64 EOCD/locator), caps the
central-directory bytes at 4 MiB and the entry count at the selected profile,
then reads the fixed-size central-directory records.  It never calls CPython
private ZIP helpers and never lets metadata claim an unbounded allocation.
Malformed, multi-disk, or inconsistent records fail ``ARCHIVE_INVALID``; an
entry count above the selected profile fails ``ENTRY_LIMIT``; and a central
directory above the fixed 4 MiB cap fails ``ARCHIVE_TOO_LARGE``.  An archive
comment that embeds another EOCD signature is ambiguous (the public
``zipfile`` reader selects the last one), so it is rejected rather than
letting the preflight and ``ZipFile`` disagree about which directory to read.  The raw-file
cap is ``max_total_bytes + ceil(max_total_bytes/1000) + 8 MiB`` so an
essentially incompressible stored package is not rejected by its own encoding
overhead.

Trust model: this is an ordinary local tool for a trusted operator.  It does
not isolate hostile code, does not execute package content, does not scan file
contents for secrets, and does not claim authorship or independent acceptance.
The privacy interception below rejects *obvious credentials paths* only; it is
not a substitute for content review.

Failure codes (``DeliveryError.code``)::

    INVALID_LIMITS, INVALID_PROFILE, INVALID_COMPRESSION, ROOT_INVALID,
    METADATA_MISSING, METADATA_INVALID, METADATA_LIMIT,
    METADATA_ACCEPTANCE_CLAIM, FILES_INVALID, PATH_INVALID, PATH_SECRET,
    PATH_UNSAFE, FILE_MISSING, FILE_NOT_REGULAR, FILE_LIMIT, ENTRY_LIMIT,
    TOTAL_LIMIT, MANIFEST_INVALID, MANIFEST_LIMIT, OUTPUT_INVALID,
    OUTPUT_EXISTS, OUTPUT_PARENT_MISSING, ARCHIVE_NOT_FOUND, ARCHIVE_INVALID,
    ARCHIVE_TOO_LARGE, ARCHIVE_ENCRYPTED, ENTRY_NOT_FILE, ENTRY_SYMLINK,
    DUPLICATE_ENTRY, ARCHIVE_ENTRY_MISMATCH, ARCHIVE_SIZE_MISMATCH,
    ARCHIVE_HASH_MISMATCH, IO_ERROR.

Error messages never include file names, metadata text, or file contents.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import struct
import sys
import unicodedata
import zipfile
from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "DEFAULT_LIMITS",
    "LARGE_LIMITS",
    "DeliveryError",
    "get_limits",
    "main",
    "open_bounded_archive",
    "pack_delivery",
    "verify_delivery",
]

_FIXED_LIMITS = {
    "max_entries": 512,
    "max_file_bytes": 64 * 1024 * 1024,
    "max_total_bytes": 128 * 1024 * 1024,
    "max_metadata_bytes": 64 * 1024,
    "max_manifest_bytes": 512 * 1024,
}

#: Immutable fixed caps for the historical standard profile.  ``limits=`` may
#: only tighten these values.
DEFAULT_LIMITS = MappingProxyType(_FIXED_LIMITS)

#: Immutable caps for the explicit large-file profile.  Entry, metadata,
#: manifest, and central-directory caps deliberately stay at the standard
#: values; only per-file and total payload bytes grow.
LARGE_LIMITS = MappingProxyType(
    {
        "max_entries": 512,
        "max_file_bytes": 8 * 1024 * 1024 * 1024,
        "max_total_bytes": 16 * 1024 * 1024 * 1024,
        "max_metadata_bytes": 64 * 1024,
        "max_manifest_bytes": 512 * 1024,
    }
)

_PROFILE_LIMITS = {"standard": DEFAULT_LIMITS, "large": LARGE_LIMITS}
_COMPRESSION_TYPES = {
    "deflated": zipfile.ZIP_DEFLATED,
    "stored": zipfile.ZIP_STORED,
}

_DELIVERY_SCHEMA = "collaborator-delivery/1"
_MANIFEST_SCHEMA = "delivery-manifest/1"

_CHUNK = 64 * 1024
_MAX_PATH = 4096
_MAX_CENTRAL_DIRECTORY = 4 * 1024 * 1024
_ZIP64_EXTRA_HEADROOM = 8 * 1024 * 1024

_EOCD_SIG = b"PK\x05\x06"
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_CENTRAL_SIG = b"PK\x01\x02"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_ZIP64_LOCATOR_STRUCT = struct.Struct("<4sLQL")
_ZIP64_EOCD_STRUCT = struct.Struct("<4sQ2H2L4Q")
_CENTRAL_HEADER_SIZE = 46
_EOCD_MIN_SIZE = 22
_EOCD_MAX_COMMENT = 0xFFFF
_ZIP64_LOCATOR_SIZE = 20
_ZIP64_EOCD_SIZE = 56
_UINT16_SENTINEL = 0xFFFF
_UINT32_SENTINEL = 0xFFFFFFFF

_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")

_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_PRIVATE_SEGMENTS = frozenset({".git", ".ssh"})
_PRIVATE_NAMES = frozenset({"id_rsa", "id_ed25519", "credentials.json"})
_BAD_SEGMENT_CHARS = frozenset('<>:"\\|?*')

_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {"com%d" % i for i in range(1, 10)}
    | {"lpt%d" % i for i in range(1, 10)}
)

_KNOWN_META_KEYS = frozenset(
    {
        "schema_version",
        "task_id",
        "attempt_id",
        "title",
        "summary",
        "status",
        "independent_acceptance",
        "files",
        "checks",
        "unfinished",
        "requirements",
        "next_steps",
    }
)


def _normalise_key(key: str) -> str:
    return "".join(ch for ch in key.casefold() if ch.isalnum())


_FORBIDDEN_ACCEPTANCE_KEYS = frozenset(
    _normalise_key(name)
    for name in (
        "accepted",
        "acceptance",
        "acceptance_status",
        "acceptance_result",
        "accepted_by",
        "approval",
        "approved",
        "verdict",
        "review_result",
        "review_status",
        "reviewer_verdict",
        "independent_acceptance_result",
        "independent_acceptance_status",
        "independent_acceptance",
    )
)


class DeliveryError(Exception):
    """Fixed-code failure raised by pack/verify.

    ``code`` is one of the stable identifiers documented in the module
    docstring.  The human-readable message is intentionally generic so that
    secrets, paths, or file content are never echoed.
    """

    #: Limit overrides were not a valid tightening mapping.
    INVALID_LIMITS = "INVALID_LIMITS"
    INVALID_PROFILE = "INVALID_PROFILE"
    INVALID_COMPRESSION = "INVALID_COMPRESSION"
    ROOT_INVALID = "ROOT_INVALID"
    METADATA_MISSING = "METADATA_MISSING"
    METADATA_INVALID = "METADATA_INVALID"
    METADATA_LIMIT = "METADATA_LIMIT"
    METADATA_ACCEPTANCE_CLAIM = "METADATA_ACCEPTANCE_CLAIM"
    FILES_INVALID = "FILES_INVALID"
    PATH_INVALID = "PATH_INVALID"
    PATH_SECRET = "PATH_SECRET"
    PATH_UNSAFE = "PATH_UNSAFE"
    FILE_MISSING = "FILE_MISSING"
    FILE_NOT_REGULAR = "FILE_NOT_REGULAR"
    FILE_LIMIT = "FILE_LIMIT"
    ENTRY_LIMIT = "ENTRY_LIMIT"
    TOTAL_LIMIT = "TOTAL_LIMIT"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    MANIFEST_LIMIT = "MANIFEST_LIMIT"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    OUTPUT_PARENT_MISSING = "OUTPUT_PARENT_MISSING"
    ARCHIVE_NOT_FOUND = "ARCHIVE_NOT_FOUND"
    ARCHIVE_INVALID = "ARCHIVE_INVALID"
    ARCHIVE_TOO_LARGE = "ARCHIVE_TOO_LARGE"
    ARCHIVE_ENCRYPTED = "ARCHIVE_ENCRYPTED"
    ENTRY_NOT_FILE = "ENTRY_NOT_FILE"
    ENTRY_SYMLINK = "ENTRY_SYMLINK"
    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    ARCHIVE_ENTRY_MISMATCH = "ARCHIVE_ENTRY_MISMATCH"
    ARCHIVE_SIZE_MISMATCH = "ARCHIVE_SIZE_MISMATCH"
    ARCHIVE_HASH_MISMATCH = "ARCHIVE_HASH_MISMATCH"
    IO_ERROR = "IO_ERROR"

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def get_limits(profile="standard", limits=None):
    """Return a full limits dict for ``profile``, applying only tighter caps.

    ``profile`` must be exactly ``"standard"`` or ``"large"`` -- there is no
    inference from archive metadata.  ``limits`` may only lower the selected
    profile's caps; raising a standard cap cannot turn it into the large
    profile.
    """
    if not isinstance(profile, str) or profile not in _PROFILE_LIMITS:
        raise DeliveryError(DeliveryError.INVALID_PROFILE, "unknown delivery profile")
    base = _PROFILE_LIMITS[profile]
    if limits is None:
        return dict(base)
    if not isinstance(limits, Mapping):
        raise DeliveryError(DeliveryError.INVALID_LIMITS, "limits must be a mapping")
    resolved = dict(base)
    for key, value in limits.items():
        if key not in base:
            raise DeliveryError(DeliveryError.INVALID_LIMITS, "unknown limit key")
        if type(value) is not int or value < 0:
            raise DeliveryError(DeliveryError.INVALID_LIMITS, "limit must be a non-negative int")
        if value > base[key]:
            raise DeliveryError(
                DeliveryError.INVALID_LIMITS,
                "limits may only tighten the selected profile",
            )
        resolved[key] = value
    return resolved


def _resolve_limits(limits, profile="standard"):
    """Backward-compatible internal wrapper around :func:`get_limits`."""
    return get_limits(profile, limits)


def _resolve_compression(compression):
    if not isinstance(compression, str) or compression not in _COMPRESSION_TYPES:
        raise DeliveryError(DeliveryError.INVALID_COMPRESSION, "unknown compression mode")
    return _COMPRESSION_TYPES[compression]


# ---------------------------------------------------------------------------
# Paths and filesystem safety
# ---------------------------------------------------------------------------


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _validate_rel_path(path: str):
    """Validate a package-relative path and return its segments.

    The rule is safe for both POSIX and Windows receivers: forward slashes only,
    no absolute paths, no empty/dot segments, no control characters, no
    Windows-forbidden characters or reserved device names, no trailing dot or
    space, and no obvious credentials paths.
    """
    if not isinstance(path, str):
        raise DeliveryError(DeliveryError.PATH_INVALID, "path must be a string")
    if not path or len(path) > _MAX_PATH:
        raise DeliveryError(DeliveryError.PATH_INVALID, "path is empty or too long")
    if path.startswith("/") or path.startswith("\\"):
        raise DeliveryError(DeliveryError.PATH_INVALID, "path must be relative")
    segments = path.split("/")
    if any(segment == "" for segment in segments):
        raise DeliveryError(DeliveryError.PATH_INVALID, "path has an empty segment")

    for segment in segments:
        if segment in (".", ".."):
            raise DeliveryError(DeliveryError.PATH_INVALID, "path has a dot segment")
        if segment.endswith(".") or segment.endswith(" "):
            raise DeliveryError(DeliveryError.PATH_INVALID, "path segment has a bad ending")
        if any(
            ch in _BAD_SEGMENT_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F
            for ch in segment
        ):
            raise DeliveryError(DeliveryError.PATH_INVALID, "path has a forbidden character")
        if segment.split(".", 1)[0].casefold() in _RESERVED_STEMS:
            raise DeliveryError(DeliveryError.PATH_INVALID, "path uses a reserved name")

        lowered = segment.casefold()
        if lowered in _PRIVATE_SEGMENTS:
            raise DeliveryError(DeliveryError.PATH_SECRET, "path is an obvious secret path")
        if lowered == ".env" or (
            lowered.startswith(".env.") and lowered not in _ENV_TEMPLATES
        ):
            raise DeliveryError(DeliveryError.PATH_SECRET, "path is an obvious secret path")
        if lowered in _PRIVATE_NAMES:
            raise DeliveryError(DeliveryError.PATH_SECRET, "path is an obvious secret path")
        if lowered.endswith((".pem", ".key")):
            raise DeliveryError(DeliveryError.PATH_SECRET, "path is an obvious secret path")
    return tuple(segments)


def _is_link_like(path: str, st) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt":
        attrs = getattr(st, "st_file_attributes", 0) or 0
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attrs & reparse:
            return True
    return False


def _coerce_path(value, code: str) -> str:
    if isinstance(value, str):
        return value
    try:
        text = os.fspath(value)
    except TypeError:
        raise DeliveryError(code, "expected a filesystem path") from None
    if not isinstance(text, str):
        raise DeliveryError(code, "expected a str filesystem path") from None
    return text


def _validate_root(root: str) -> str:
    if not isinstance(root, str):
        raise DeliveryError(DeliveryError.ROOT_INVALID, "root must be a path string")
    try:
        st = os.lstat(root)
    except OSError:
        raise DeliveryError(DeliveryError.ROOT_INVALID, "root is not accessible") from None
    if _is_link_like(root, st) or not stat.S_ISDIR(st.st_mode):
        raise DeliveryError(DeliveryError.ROOT_INVALID, "root must be a real directory")
    return os.path.abspath(root)


def _ensure_regular_file(root: str, segments) -> str:
    current = root
    for segment in segments[:-1]:
        current = os.path.join(current, segment)
        try:
            st = os.lstat(current)
        except OSError:
            raise DeliveryError(DeliveryError.FILE_MISSING, "declared file is missing") from None
        if _is_link_like(current, st):
            raise DeliveryError(DeliveryError.PATH_UNSAFE, "declared path crosses a link")
        if not stat.S_ISDIR(st.st_mode):
            raise DeliveryError(DeliveryError.FILE_NOT_REGULAR, "declared parent is not a dir")

    final = os.path.join(current, segments[-1])
    try:
        st = os.lstat(final)
    except OSError:
        raise DeliveryError(DeliveryError.FILE_MISSING, "declared file is missing") from None
    if _is_link_like(final, st):
        raise DeliveryError(DeliveryError.PATH_UNSAFE, "declared file is a link")
    if not stat.S_ISREG(st.st_mode):
        raise DeliveryError(DeliveryError.FILE_NOT_REGULAR, "declared entry is not a file")
    return final


# ---------------------------------------------------------------------------
# Strict JSON and metadata validation
# ---------------------------------------------------------------------------


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _strict_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _loads_json(data: bytes, code: str):
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise DeliveryError(code, "JSON is invalid") from None


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _require_id(value) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DeliveryError(DeliveryError.METADATA_INVALID, "identifier is invalid")


def _validate_str_list(value) -> None:
    if not isinstance(value, list):
        raise DeliveryError(DeliveryError.METADATA_INVALID, "expected a list of strings")
    for item in value:
        if not _nonempty_str(item):
            raise DeliveryError(DeliveryError.METADATA_INVALID, "list item is not a string")


def _validate_metadata(meta) -> None:
    if not isinstance(meta, dict):
        raise DeliveryError(DeliveryError.METADATA_INVALID, "metadata must be an object")

    for key in meta:
        if key not in _KNOWN_META_KEYS and _normalise_key(key) in _FORBIDDEN_ACCEPTANCE_KEYS:
            raise DeliveryError(
                DeliveryError.METADATA_ACCEPTANCE_CLAIM,
                "metadata may not claim independent acceptance",
            )

    if meta.get("schema_version") != _DELIVERY_SCHEMA:
        raise DeliveryError(DeliveryError.METADATA_INVALID, "bad schema_version")
    _require_id(meta.get("task_id"))
    attempt_id = meta.get("attempt_id")
    if attempt_id is not None:
        _require_id(attempt_id)

    if not _nonempty_str(meta.get("title")):
        raise DeliveryError(DeliveryError.METADATA_INVALID, "title is required")
    if not _nonempty_str(meta.get("summary")):
        raise DeliveryError(DeliveryError.METADATA_INVALID, "summary is required")

    status = meta.get("status")
    if status not in ("completed", "partial", "blocked"):
        raise DeliveryError(DeliveryError.METADATA_INVALID, "bad status")
    if meta.get("independent_acceptance") != "UNKNOWN":
        raise DeliveryError(
            DeliveryError.METADATA_INVALID, "independent_acceptance must be UNKNOWN"
        )

    raw_files = meta.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise DeliveryError(DeliveryError.FILES_INVALID, "files must be a non-empty list")
    files = []
    seen = set()
    for item in raw_files:
        if not isinstance(item, str):
            raise DeliveryError(DeliveryError.FILES_INVALID, "files entries must be strings")
        _validate_rel_path(item)
        if item.casefold() in ("delivery.json", "manifest.json"):
            raise DeliveryError(DeliveryError.FILES_INVALID, "reserved name in files")
        key = _path_key(item)
        if key in seen:
            raise DeliveryError(DeliveryError.FILES_INVALID, "duplicate file path")
        seen.add(key)
        files.append(item)
    if "START-HERE.md" not in files:
        raise DeliveryError(DeliveryError.FILES_INVALID, "files must contain START-HERE.md")

    checks = meta.get("checks", [])
    if not isinstance(checks, list):
        raise DeliveryError(DeliveryError.METADATA_INVALID, "checks must be a list")
    files_set = set(files)
    for item in checks:
        if not isinstance(item, dict):
            raise DeliveryError(DeliveryError.METADATA_INVALID, "check must be an object")
        if not _nonempty_str(item.get("name")):
            raise DeliveryError(DeliveryError.METADATA_INVALID, "check name is required")
        check_status = item.get("status")
        if check_status in ("passed", "failed"):
            evidence = item.get("evidence")
            if not isinstance(evidence, str) or evidence not in files_set:
                raise DeliveryError(
                    DeliveryError.METADATA_INVALID,
                    "check evidence must be a declared file",
                )
        elif check_status == "not_run":
            if not _nonempty_str(item.get("reason")):
                raise DeliveryError(
                    DeliveryError.METADATA_INVALID,
                    "not_run check needs a reason",
                )
        else:
            raise DeliveryError(DeliveryError.METADATA_INVALID, "bad check status")

    unfinished = meta.get("unfinished", [])
    _validate_str_list(unfinished)
    _validate_str_list(meta.get("requirements", []))
    _validate_str_list(meta.get("next_steps", []))

    if status == "completed":
        if unfinished:
            raise DeliveryError(DeliveryError.METADATA_INVALID, "completed has unfinished items")
        if any(item.get("status") == "failed" for item in checks):
            raise DeliveryError(DeliveryError.METADATA_INVALID, "completed has a failed check")
    else:
        if not unfinished:
            raise DeliveryError(
                DeliveryError.METADATA_INVALID, "partial/blocked needs an unfinished item"
            )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _dump_manifest(entries) -> bytes:
    ordered = sorted(entries, key=lambda entry: entry["path"])
    obj = {"schema_version": _MANIFEST_SCHEMA, "files": ordered}
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_manifest(data: bytes):
    obj = _loads_json(data, DeliveryError.MANIFEST_INVALID)
    if not isinstance(obj, dict) or set(obj) != {"schema_version", "files"}:
        raise DeliveryError(DeliveryError.MANIFEST_INVALID, "bad manifest object")
    if obj.get("schema_version") != _MANIFEST_SCHEMA:
        raise DeliveryError(DeliveryError.MANIFEST_INVALID, "bad manifest schema_version")
    raw_entries = obj.get("files")
    if not isinstance(raw_entries, list):
        raise DeliveryError(DeliveryError.MANIFEST_INVALID, "manifest files must be a list")

    entries = {}
    seen = set()
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise DeliveryError(DeliveryError.MANIFEST_INVALID, "bad manifest entry")
        path = item.get("path")
        if not isinstance(path, str):
            raise DeliveryError(DeliveryError.MANIFEST_INVALID, "bad manifest path")
        _validate_rel_path(path)
        if path == "MANIFEST.json":
            raise DeliveryError(
                DeliveryError.MANIFEST_INVALID, "manifest must not list itself"
            )
        key = _path_key(path)
        if path in entries or key in seen:
            raise DeliveryError(DeliveryError.MANIFEST_INVALID, "duplicate manifest path")
        seen.add(key)
        size = item.get("bytes")
        if type(size) is not int or size < 0:
            raise DeliveryError(DeliveryError.MANIFEST_INVALID, "bad manifest size")
        digest = item.get("sha256")
        if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
            raise DeliveryError(DeliveryError.MANIFEST_INVALID, "bad manifest digest")
        entries[path] = {"bytes": size, "sha256": digest}
    return entries


def _expected_manifest_paths(files) -> set:
    return {"DELIVERY.json"} | set(files)


# ---------------------------------------------------------------------------
# Bounded reads
# ---------------------------------------------------------------------------


def _read_metadata_file(root: str, limit: int) -> bytes:
    path = os.path.join(root, "DELIVERY.json")
    try:
        st = os.lstat(path)
    except OSError:
        raise DeliveryError(DeliveryError.METADATA_MISSING, "DELIVERY.json is missing") from None
    if _is_link_like(path, st):
        raise DeliveryError(DeliveryError.PATH_UNSAFE, "DELIVERY.json is a link")
    if not stat.S_ISREG(st.st_mode):
        raise DeliveryError(DeliveryError.METADATA_MISSING, "DELIVERY.json is not a file")
    if st.st_size > limit:
        raise DeliveryError(DeliveryError.METADATA_LIMIT, "DELIVERY.json is too large")
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise DeliveryError(DeliveryError.IO_ERROR, "could not read DELIVERY.json") from None
    if len(data) > limit:
        raise DeliveryError(DeliveryError.METADATA_LIMIT, "DELIVERY.json is too large")
    return data


def _zip_info(name: str, compression: int = zipfile.ZIP_DEFLATED) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _write_zip_bytes(
    handle: zipfile.ZipFile,
    name: str,
    data: bytes,
    compression: int = zipfile.ZIP_DEFLATED,
    force_zip64: bool = False,
) -> None:
    info = _zip_info(name, compression)
    # The size is already known and small; this keeps the header decision
    # explicit instead of relying on a stream-sized guess.
    info.file_size = len(data)
    with handle.open(info, "w", force_zip64=force_zip64) as target:
        target.write(data)


def _copy_source_to_zip(
    handle, name, source, limits, running_total, compression, force_zip64=False
):
    """Stream one source file into the ZIP, hashing exactly the written bytes.

    The trusted ``fstat`` size is used only for an early limit decision and to
    make the local-header ZIP64 decision; the read loop still counts the actual
    bytes and enforces both caps, so a file that grows or shrinks during the
    copy cannot bypass the limits.
    """
    info = _zip_info(name, compression)
    digest = hashlib.sha256()
    written = 0
    try:
        source_handle = open(source, "rb")
    except OSError:
        raise DeliveryError(DeliveryError.FILE_MISSING, "declared file is missing") from None
    with source_handle:
        st = os.fstat(source_handle.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise DeliveryError(
                DeliveryError.FILE_NOT_REGULAR, "declared entry is not a regular file"
            )
        if st.st_size > limits["max_file_bytes"]:
            raise DeliveryError(DeliveryError.FILE_LIMIT, "file exceeds limit")
        if running_total + st.st_size > limits["max_total_bytes"]:
            raise DeliveryError(DeliveryError.TOTAL_LIMIT, "delivery exceeds limit")
        info.file_size = st.st_size
        with handle.open(info, "w", force_zip64=force_zip64) as target:
            while True:
                chunk = source_handle.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limits["max_file_bytes"]:
                    raise DeliveryError(DeliveryError.FILE_LIMIT, "file exceeds limit")
                if running_total + written > limits["max_total_bytes"]:
                    raise DeliveryError(DeliveryError.TOTAL_LIMIT, "delivery exceeds limit")
                digest.update(chunk)
                target.write(chunk)
    return written, digest.hexdigest()


# ---------------------------------------------------------------------------
# Output target handling
# ---------------------------------------------------------------------------


def _validate_output_target(out: str) -> None:
    if not isinstance(out, str):
        raise DeliveryError(DeliveryError.OUTPUT_INVALID, "out must be a path string")
    if os.path.lexists(out):
        raise DeliveryError(DeliveryError.OUTPUT_EXISTS, "output already exists")
    parent = os.path.dirname(os.path.abspath(out))
    if not parent:
        parent = os.curdir
    try:
        st = os.stat(parent)
    except OSError:
        raise DeliveryError(
            DeliveryError.OUTPUT_PARENT_MISSING, "output parent does not exist"
        ) from None
    if not stat.S_ISDIR(st.st_mode):
        raise DeliveryError(
            DeliveryError.OUTPUT_PARENT_MISSING, "output parent is not a directory"
        )


def _discard_new_output(out: str, identity) -> None:
    try:
        st = os.lstat(out)
    except OSError:
        return
    if identity is not None and (st.st_dev, st.st_ino) != identity:
        return
    if stat.S_ISREG(st.st_mode) and not _is_link_like(out, st):
        try:
            os.remove(out)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------


def pack_delivery(root, out, *, limits=None, profile="standard", compression="deflated"):
    """Create ``out`` (must not exist) from the whitelist in ``root``.

    ``root`` is not modified.  Only ``DELIVERY.json`` plus the files it lists are
    read; ``MANIFEST.json`` is generated inside the archive and is not read from
    the staging directory.  On failure any archive created by this call is
    removed; pre-existing outputs are never touched.  The result is re-verified
    with :func:`verify_delivery` before returning.

    ``profile="large"`` enables ZIP64 writing and the large byte caps; the
    standard profile never writes ZIP64.  ``compression="stored"`` avoids
    recompressing already-compressed resources.
    """
    resolved = get_limits(profile, limits)
    compression_type = _resolve_compression(compression)
    large = profile == "large"
    root_path = _validate_root(_coerce_path(root, DeliveryError.ROOT_INVALID))
    out = _coerce_path(out, DeliveryError.OUTPUT_INVALID)
    _validate_output_target(out)

    delivery_bytes = _read_metadata_file(root_path, resolved["max_metadata_bytes"])
    meta = _loads_json(delivery_bytes, DeliveryError.METADATA_INVALID)
    _validate_metadata(meta)
    files = list(meta["files"])

    if 2 + len(files) > resolved["max_entries"]:
        raise DeliveryError(DeliveryError.ENTRY_LIMIT, "too many archive entries")
    if len(delivery_bytes) > resolved["max_file_bytes"]:
        raise DeliveryError(DeliveryError.FILE_LIMIT, "DELIVERY.json exceeds limit")

    created = False
    identity = None
    descriptor = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(out, flags, 0o666)
        except OSError:
            raise DeliveryError(DeliveryError.IO_ERROR, "could not create output") from None
        created = True
        created_stat = os.fstat(descriptor)
        identity = (created_stat.st_dev, created_stat.st_ino)
        handle = os.fdopen(descriptor, "w+b")
        descriptor = None
        with handle:
            archive = zipfile.ZipFile(
                handle, "w", compression=compression_type, allowZip64=large
            )
            try:
                entries = [
                    {
                        "path": "DELIVERY.json",
                        "bytes": len(delivery_bytes),
                        "sha256": hashlib.sha256(delivery_bytes).hexdigest(),
                    }
                ]
                running_total = len(delivery_bytes)
                for relative in files:
                    segments = _validate_rel_path(relative)
                    source = _ensure_regular_file(root_path, segments)
                    written, digest = _copy_source_to_zip(
                        archive,
                        relative,
                        source,
                        resolved,
                        running_total,
                        compression_type,
                        force_zip64=large,
                    )
                    running_total += written
                    entries.append(
                        {"path": relative, "bytes": written, "sha256": digest}
                    )

                manifest_bytes = _dump_manifest(entries)
                if len(manifest_bytes) > resolved["max_manifest_bytes"]:
                    raise DeliveryError(
                        DeliveryError.MANIFEST_LIMIT, "manifest exceeds limit"
                    )
                if len(manifest_bytes) > resolved["max_file_bytes"]:
                    raise DeliveryError(DeliveryError.FILE_LIMIT, "manifest exceeds limit")
                total = running_total + len(manifest_bytes)
                if total > resolved["max_total_bytes"]:
                    raise DeliveryError(DeliveryError.TOTAL_LIMIT, "delivery exceeds limit")

                _write_zip_bytes(
                    archive,
                    "MANIFEST.json",
                    manifest_bytes,
                    compression_type,
                    force_zip64=large,
                )
                _write_zip_bytes(
                    archive,
                    "DELIVERY.json",
                    delivery_bytes,
                    compression_type,
                    force_zip64=large,
                )
            except zipfile.LargeZipFile:
                raise DeliveryError(
                    DeliveryError.FILE_LIMIT,
                    "ZIP64 is required but not enabled for this profile",
                ) from None
            finally:
                try:
                    archive.close()
                except zipfile.LargeZipFile:
                    raise DeliveryError(
                        DeliveryError.FILE_LIMIT,
                        "ZIP64 is required but not enabled for this profile",
                    ) from None
        return verify_delivery(out, limits=limits, profile=profile)
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created:
            _discard_new_output(out, identity)
        raise


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    OSError,
    ValueError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    struct.error,
    OverflowError,
)


def _archive_raw_cap(limits) -> int:
    """Conservative raw-file cap for encoded ZIP bytes.

    Deflate's worst-case expansion is well under 0.1% of the payload, and the
    fixed 8 MiB headroom covers local/central headers, ZIP64 records, names,
    comments, and small metadata.  Using ``total + total/1000 + 8 MiB`` avoids
    rejecting a legal, essentially incompressible large package while still
    bounding the file we are willing to open and hash.
    """
    total = limits["max_total_bytes"]
    return total + (total + 999) // 1000 + _ZIP64_EXTRA_HEADROOM


def _read_exact_at(handle, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "truncated ZIP record")
    return data


def _find_eocd(handle, file_size: int):
    """Boundedly locate and parse the classic EOCD record.

    The scan reads at most 22 + 65535 trailing bytes, exactly the ZIP limit for
    an archive comment.  The accepted record must be the same one the public
    :class:`zipfile.ZipFile` reader would use, otherwise the preflight could
    validate one central directory while ``ZipFile`` followed another.

    Two selection rules mirror the standard library exactly:

    * No-comment fast path: when the final 22 bytes are an EOCD record whose
      comment length is zero, that record is used directly.  ``zipfile`` checks
      this fixed block before searching, so a normal archive whose own numeric
      field happens to encode an EOCD signature is not falsely rejected.
    * Commented path: ``zipfile`` searches the whole trailing window and trusts
      the *last* EOCD signature it finds.  A record is accepted only when it is
      that last signature in the entire window and its declared comment length
      lands exactly at EOF.  A signature hidden in an earlier record's numeric
      fields (for example the central-directory offset) or in the comment makes
      the selection ambiguous and is rejected outright; ordinary comments
      without an EOCD signature remain fully supported.
    """
    if file_size < _EOCD_MIN_SIZE:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "file is too short for a ZIP")
    tail_length = min(file_size, _EOCD_MIN_SIZE + _EOCD_MAX_COMMENT)
    tail_start = file_size - tail_length
    handle.seek(tail_start)
    tail = handle.read(tail_length)
    if len(tail) != tail_length:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "could not read ZIP tail")

    final = len(tail) - _EOCD_MIN_SIZE
    if tail[final : final + 4] == _EOCD_SIG:
        fields = _EOCD_STRUCT.unpack_from(tail, final)
        if fields[7] == 0:
            return tail_start + final, fields

    # Commented archive: accept only the last signature in the whole window,
    # which is the one ``zipfile`` would unpack.
    index = tail.rfind(_EOCD_SIG)
    if index < 0:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "ZIP end record is missing")
    if index + _EOCD_MIN_SIZE > len(tail):
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "ZIP end record is truncated")
    fields = _EOCD_STRUCT.unpack_from(tail, index)
    if index + _EOCD_MIN_SIZE + fields[7] != len(tail):
        raise DeliveryError(
            DeliveryError.ARCHIVE_INVALID,
            "ambiguous ZIP end record in the trailing window",
        )
    return tail_start + index, fields


def _preflight_central_directory(handle, file_size: int, limits) -> None:
    """Parse EOCD/ZIP64 records and the central directory before ZipFile.

    This is intentionally a small public-structure parser: it never calls
    ``zipfile._EndRecData``, never trusts a declared size before capping it at
    4 MiB, and counts the fixed 46-byte central-directory records itself.
    """
    eocd_pos, eocd = _find_eocd(handle, file_size)
    (
        _signature,
        disk_number,
        cd_disk,
        entries_disk,
        entries_total,
        cd_size,
        cd_offset,
        _comment_len,
    ) = eocd

    if disk_number != 0 or cd_disk != 0 or entries_disk != entries_total:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "multi-disk ZIP is not supported")

    locator = b""
    if eocd_pos >= _ZIP64_LOCATOR_SIZE:
        locator = _read_exact_at(handle, eocd_pos - _ZIP64_LOCATOR_SIZE, _ZIP64_LOCATOR_SIZE)
    has_locator = locator[:4] == _ZIP64_LOCATOR_SIG
    needs_zip64 = (
        entries_disk == _UINT16_SENTINEL
        or entries_total == _UINT16_SENTINEL
        or cd_size == _UINT32_SENTINEL
        or cd_offset == _UINT32_SENTINEL
    )
    zip64_pos = eocd_pos - _ZIP64_LOCATOR_SIZE - _ZIP64_EOCD_SIZE
    if has_locator and zip64_pos >= 0:
        # A stray locator signature inside ordinary bytes just before the EOCD
        # must not hijack a non-ZIP64 archive.  Only a real fixed ZIP64 record
        # immediately before the locator makes the extension authoritative.
        if _read_exact_at(handle, zip64_pos, 4) != _ZIP64_EOCD_SIG:
            if needs_zip64:
                raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "bad ZIP64 end record")
            has_locator = False
    elif has_locator:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "ZIP64 end record is missing")
    if needs_zip64 and not has_locator:
        raise DeliveryError(
            DeliveryError.ARCHIVE_INVALID, "ZIP64 sentinel without a ZIP64 locator"
        )

    if has_locator:
        (
            _locator_signature,
            locator_disk,
            locator_offset,
            locator_disks,
        ) = _ZIP64_LOCATOR_STRUCT.unpack(locator)
        if locator_disk != 0 or locator_disks > 1:
            raise DeliveryError(
                DeliveryError.ARCHIVE_INVALID, "multi-disk ZIP is not supported"
            )
        record = _read_exact_at(handle, zip64_pos, _ZIP64_EOCD_SIZE)
        (
            zip64_signature,
            zip64_size,
            _version_made,
            _version_needed,
            zip64_disk,
            zip64_cd_disk,
            zip64_entries_disk,
            zip64_entries_total,
            zip64_cd_size,
            zip64_cd_offset,
        ) = _ZIP64_EOCD_STRUCT.unpack(record)
        if zip64_signature != _ZIP64_EOCD_SIG:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "bad ZIP64 end record")
        # CPython's reader only understands the fixed 56-byte record without
        # extensible data; reject explicitly instead of letting ZipFile guess.
        if zip64_size != 44:
            raise DeliveryError(
                DeliveryError.ARCHIVE_INVALID, "ZIP64 extensible data is not supported"
            )
        if zip64_disk != 0 or zip64_cd_disk != 0:
            raise DeliveryError(
                DeliveryError.ARCHIVE_INVALID, "multi-disk ZIP is not supported"
            )
        if zip64_entries_disk != zip64_entries_total:
            raise DeliveryError(
                DeliveryError.ARCHIVE_INVALID, "inconsistent ZIP64 entry counts"
            )
        if locator_offset > zip64_pos:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "bad ZIP64 locator offset")
        if entries_total != _UINT16_SENTINEL and entries_total != zip64_entries_total:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "inconsistent entry counts")
        if cd_size != _UINT32_SENTINEL and cd_size != zip64_cd_size:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "inconsistent ZIP64 size")
        if cd_offset != _UINT32_SENTINEL and cd_offset != zip64_cd_offset:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "inconsistent ZIP64 offset")
        entries_total = zip64_entries_total
        cd_size = zip64_cd_size
        cd_offset = zip64_cd_offset
        region_end = zip64_pos
    else:
        region_end = eocd_pos

    if entries_total > limits["max_entries"]:
        raise DeliveryError(DeliveryError.ENTRY_LIMIT, "too many archive entries")
    if cd_size > _MAX_CENTRAL_DIRECTORY:
        raise DeliveryError(
            DeliveryError.ARCHIVE_TOO_LARGE, "central directory exceeds the fixed cap"
        )
    if cd_size < _CENTRAL_HEADER_SIZE * entries_total:
        raise DeliveryError(
            DeliveryError.ARCHIVE_INVALID, "central directory is too small for its entries"
        )
    if cd_offset + cd_size > region_end:
        raise DeliveryError(
            DeliveryError.ARCHIVE_INVALID, "central directory lies outside the archive"
        )
    cd_start = region_end - cd_size
    if cd_start < 0:
        raise DeliveryError(
            DeliveryError.ARCHIVE_INVALID, "central directory lies outside the archive"
        )

    data = _read_exact_at(handle, cd_start, cd_size)
    offset = 0
    count = 0
    while offset < cd_size:
        if cd_size - offset < _CENTRAL_HEADER_SIZE:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "truncated central directory")
        if data[offset : offset + 4] != _CENTRAL_SIG:
            raise DeliveryError(
                DeliveryError.ARCHIVE_INVALID, "bad central directory record signature"
            )
        name_length = int.from_bytes(data[offset + 28 : offset + 30], "little")
        extra_length = int.from_bytes(data[offset + 30 : offset + 32], "little")
        comment_length = int.from_bytes(data[offset + 32 : offset + 34], "little")
        record_length = (
            _CENTRAL_HEADER_SIZE + name_length + extra_length + comment_length
        )
        if record_length > cd_size - offset:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "truncated central directory")
        offset += record_length
        count += 1
        if count > limits["max_entries"]:
            raise DeliveryError(DeliveryError.ENTRY_LIMIT, "too many archive entries")
    if count != entries_total:
        raise DeliveryError(
            DeliveryError.ARCHIVE_INVALID, "central directory entry count is inconsistent"
        )


@contextlib.contextmanager
def open_bounded_archive(path, *, limits=None, profile="standard"):
    """Open a delivery ZIP after bounded central-directory preflight.

    The preflight and the returned ``zipfile.ZipFile`` share one underlying
    handle, so a caller cannot be handed a second uncontrolled read.  The
    yielded object is a normal read-only ``ZipFile``.
    """
    resolved = get_limits(profile, limits)
    archive = _coerce_path(path, DeliveryError.ARCHIVE_INVALID)
    cap = _archive_raw_cap(resolved)
    try:
        st = os.stat(archive)
    except OSError:
        raise DeliveryError(DeliveryError.ARCHIVE_NOT_FOUND, "archive is not accessible") from None
    if not stat.S_ISREG(st.st_mode):
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "archive is not a regular file")
    if st.st_size > cap:
        raise DeliveryError(DeliveryError.ARCHIVE_TOO_LARGE, "archive is too large")
    try:
        raw_handle = open(archive, "rb")
    except OSError:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "archive is not readable") from None

    archive_zip = None
    try:
        try:
            opened_stat = os.fstat(raw_handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise DeliveryError(
                    DeliveryError.ARCHIVE_INVALID, "archive is not a regular file"
                )
            if opened_stat.st_size > cap:
                raise DeliveryError(DeliveryError.ARCHIVE_TOO_LARGE, "archive is too large")
            _preflight_central_directory(raw_handle, opened_stat.st_size, resolved)
            raw_handle.seek(0)
            archive_zip = zipfile.ZipFile(raw_handle, "r")
        except DeliveryError:
            raise
        except _ZIP_READ_ERRORS:
            raise DeliveryError(
                DeliveryError.ARCHIVE_INVALID, "archive is not a readable ZIP"
            ) from None
        try:
            yield archive_zip
        finally:
            archive_zip.close()
    finally:
        raw_handle.close()


def _check_entry_safety(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not isinstance(name, str) or not name:
        raise DeliveryError(DeliveryError.ARCHIVE_ENTRY_MISMATCH, "bad entry name")
    if info.is_dir() or name.endswith("/"):
        raise DeliveryError(DeliveryError.ENTRY_NOT_FILE, "directory entries are not allowed")
    _validate_rel_path(name)
    if info.flag_bits & 0x41:
        raise DeliveryError(DeliveryError.ARCHIVE_ENCRYPTED, "encrypted entries are not allowed")

    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        raise DeliveryError(DeliveryError.ENTRY_SYMLINK, "symlink entries are not allowed")
    if file_type not in (0, stat.S_IFREG):
        raise DeliveryError(DeliveryError.ENTRY_NOT_FILE, "special entries are not allowed")
    if info.external_attr & 0x18:
        raise DeliveryError(DeliveryError.ENTRY_NOT_FILE, "non-file entries are not allowed")


def _scan_entry(handle, info, caps, state, limits, collect=False):
    """Read one entry with hard caps; optionally collect small payload bytes."""
    digest = hashlib.sha256()
    collected = [] if collect else None
    size = 0
    remaining = limits["max_total_bytes"] - state["total"]
    if remaining < 0:
        raise DeliveryError(DeliveryError.TOTAL_LIMIT, "delivery exceeds limit")
    try:
        with handle.open(info, "r") as source:
            while True:
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                for cap, code in caps:
                    if size > cap:
                        raise DeliveryError(code, "entry exceeds limit")
                if size > remaining:
                    raise DeliveryError(DeliveryError.TOTAL_LIMIT, "delivery exceeds limit")
                digest.update(chunk)
                if collect:
                    collected.append(chunk)
    except DeliveryError:
        raise
    except _ZIP_READ_ERRORS:
        raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "archive is corrupt") from None

    if size != info.file_size:
        raise DeliveryError(DeliveryError.ARCHIVE_SIZE_MISMATCH, "entry size is inconsistent")
    state["total"] += size
    data = b"".join(collected) if collect else None
    return size, digest.hexdigest(), data


def _sha256_open_handle(handle, cap: int) -> str:
    """Hash the already-open archive handle in fixed chunks under ``cap``."""
    digest = hashlib.sha256()
    size = 0
    try:
        handle.seek(0)
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > cap:
                raise DeliveryError(DeliveryError.ARCHIVE_TOO_LARGE, "archive is too large")
            digest.update(chunk)
    except DeliveryError:
        raise
    except OSError:
        raise DeliveryError(DeliveryError.IO_ERROR, "could not read archive") from None
    return digest.hexdigest()


def verify_delivery(archive, *, limits=None, profile="standard"):
    """Structurally verify a delivery ZIP without extracting or executing it.

    Checks the exact entry set, path rules, link/special/encrypted entries,
    DELIVERY.json rules, MANIFEST.json schema, per-entry size and SHA-256, all
    fixed limits, and ZIP/CRC integrity.  Returns the same stats dict as
    :func:`pack_delivery`.

    ``profile`` selects the caps; the standard profile deliberately rejects a
    package that exceeds its smaller byte limits unless the receiver explicitly
    asks for ``profile="large"``.
    """
    resolved = get_limits(profile, limits)
    archive = _coerce_path(archive, DeliveryError.ARCHIVE_INVALID)
    raw_cap = _archive_raw_cap(resolved)

    with open_bounded_archive(archive, limits=resolved, profile=profile) as archive_zip:
        try:
            infos = archive_zip.infolist()
            if len(infos) > resolved["max_entries"]:
                raise DeliveryError(DeliveryError.ENTRY_LIMIT, "too many archive entries")

            seen_exact = set()
            seen_keys = set()
            for info in infos:
                _check_entry_safety(info)
                name = info.filename
                key = _path_key(name)
                if name in seen_exact or key in seen_keys:
                    raise DeliveryError(DeliveryError.DUPLICATE_ENTRY, "duplicate entry name")
                seen_exact.add(name)
                seen_keys.add(key)

            # A ZIP64 header may declare a huge member.  Reject from the
            # directory metadata before opening/decompressing the payload;
            # the real read loop below still enforces the same caps.
            declared_total = 0
            for info in infos:
                if info.file_size > resolved["max_file_bytes"]:
                    raise DeliveryError(DeliveryError.FILE_LIMIT, "entry exceeds file limit")
                declared_total += info.file_size
                if declared_total > resolved["max_total_bytes"]:
                    raise DeliveryError(DeliveryError.TOTAL_LIMIT, "delivery exceeds limit")

            by_name = {info.filename: info for info in infos}
            if "DELIVERY.json" not in by_name or "MANIFEST.json" not in by_name:
                raise DeliveryError(
                    DeliveryError.ARCHIVE_ENTRY_MISMATCH, "required entry is missing"
                )

            state = {"total": 0}
            delivery_info = by_name["DELIVERY.json"]
            _, _, delivery_bytes = _scan_entry(
                archive_zip,
                delivery_info,
                [
                    (resolved["max_metadata_bytes"], DeliveryError.METADATA_LIMIT),
                    (resolved["max_file_bytes"], DeliveryError.FILE_LIMIT),
                ],
                state,
                resolved,
                collect=True,
            )
            meta = _loads_json(delivery_bytes, DeliveryError.METADATA_INVALID)
            _validate_metadata(meta)
            files = list(meta["files"])

            manifest_info = by_name["MANIFEST.json"]
            _, _, manifest_bytes = _scan_entry(
                archive_zip,
                manifest_info,
                [
                    (resolved["max_manifest_bytes"], DeliveryError.MANIFEST_LIMIT),
                    (resolved["max_file_bytes"], DeliveryError.FILE_LIMIT),
                ],
                state,
                resolved,
                collect=True,
            )
            manifest_entries = _parse_manifest(manifest_bytes)
            if set(manifest_entries) != _expected_manifest_paths(files):
                raise DeliveryError(
                    DeliveryError.MANIFEST_INVALID, "manifest does not match DELIVERY.json"
                )

            expected_names = _expected_manifest_paths(files) | {"MANIFEST.json"}
            if set(by_name) != expected_names:
                raise DeliveryError(
                    DeliveryError.ARCHIVE_ENTRY_MISMATCH, "entry set does not match manifest"
                )

            delivery_entry = manifest_entries["DELIVERY.json"]
            if delivery_entry["bytes"] != len(delivery_bytes):
                raise DeliveryError(
                    DeliveryError.ARCHIVE_SIZE_MISMATCH, "DELIVERY.json size mismatch"
                )
            if delivery_entry["sha256"] != hashlib.sha256(delivery_bytes).hexdigest():
                raise DeliveryError(
                    DeliveryError.ARCHIVE_HASH_MISMATCH, "DELIVERY.json digest mismatch"
                )

            for relative in files:
                expected = manifest_entries[relative]
                info = by_name[relative]
                size, digest, _ = _scan_entry(
                    archive_zip,
                    info,
                    [(resolved["max_file_bytes"], DeliveryError.FILE_LIMIT)],
                    state,
                    resolved,
                )
                if size != expected["bytes"]:
                    raise DeliveryError(
                        DeliveryError.ARCHIVE_SIZE_MISMATCH, "file size mismatch"
                    )
                if digest != expected["sha256"]:
                    raise DeliveryError(
                        DeliveryError.ARCHIVE_HASH_MISMATCH, "file digest mismatch"
                    )

            archive_digest = _sha256_open_handle(archive_zip.fp, raw_cap)
            return {
                "archive_sha256": archive_digest,
                "files": len(infos),
                "total_bytes": state["total"],
            }
        except DeliveryError:
            raise
        except _ZIP_READ_ERRORS:
            raise DeliveryError(DeliveryError.ARCHIVE_INVALID, "archive is corrupt") from None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="delivery.py",
        description="Pack or verify a collaborator-delivery/1 ZIP (stdlib only).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack_parser = subparsers.add_parser("pack", help="build a new delivery ZIP")
    pack_parser.add_argument("--root", required=True, help="staging directory")
    pack_parser.add_argument("--out", required=True, help="new ZIP path (must not exist)")
    pack_parser.add_argument("--profile", default="standard", help="standard or large")
    pack_parser.add_argument(
        "--compression", default="deflated", help="deflated or stored"
    )

    verify_parser = subparsers.add_parser("verify", help="verify an existing delivery ZIP")
    verify_parser.add_argument("archive", help="ZIP path")
    verify_parser.add_argument("--profile", default="standard", help="standard or large")

    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            stats = pack_delivery(
                args.root,
                args.out,
                profile=args.profile,
                compression=args.compression,
            )
        else:
            stats = verify_delivery(args.archive, profile=args.profile)
    except DeliveryError as exc:
        print(json.dumps({"error": exc.code}, separators=(",", ":")), file=sys.stderr)
        return 1
    except OSError:
        print(json.dumps({"error": DeliveryError.IO_ERROR}, separators=(",", ":")), file=sys.stderr)
        return 1

    print(json.dumps(stats, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
