"""Check the optional traceability extension of a ``collaborator-delivery/1`` ZIP.

This script is meant to live next to the accepted, trusted ``delivery.py``
helper (``package-delivery/scripts/``).  It *first* calls that helper's
``verify_delivery()`` on the archive, so every structural rule of the base
format is enforced by the accepted implementation.  Only after that does it
read the already-verified ``DELIVERY.json`` and ``MANIFEST.json`` entries
directly from the ZIP (they are never extracted or executed) and check the
``traceability`` object introduced for v1.1.0.

Usage::

    python check_handoff.py <archive>

Public API::

    check_handoff(archive) -> dict
    HandoffError.code      # fixed, machine-readable failure code

The successful result is::

    {"archive_sha256": "<hex>", "criteria_count": <int>,
     "verification_count": <int>, "independent_acceptance": "UNKNOWN"}

The checker only verifies internal consistency of the declared records.  It
does not run any command, does not prove that prose requirements are true or
good, and never claims independent acceptance.

Failure codes owned by this module (``HandoffError.code``)::

    ARGS_INVALID, HELPER_MISSING, HELPER_INVALID, IO_ERROR, ARCHIVE_INVALID,
    METADATA_INVALID, MANIFEST_INVALID, METADATA_LIMIT, MANIFEST_LIMIT,
    TRACEABILITY_MISSING, TRACEABILITY_INVALID, CRITERIA_INVALID,
    VERIFICATIONS_INVALID, CRITERION_ID_INVALID, CRITERION_STATUS_INVALID,
    CRITERION_REASON_INVALID, CRITERION_REASON_REQUIRED,
    CRITERION_MET_INVALID, VERIFICATION_ID_INVALID, VERIFICATION_KIND_INVALID,
    VERIFICATION_EXECUTION_INVALID, VERIFICATION_STATUS_INVALID,
    VERIFICATION_STATE_INVALID, COMMAND_INVALID, CWD_INVALID,
    ENVIRONMENT_INVALID, EXIT_CODE_INVALID, VERIFICATION_STATUS_MISMATCH,
    EVIDENCE_INVALID, SUBJECTS_INVALID, SUBJECT_INVALID,
    SUBJECT_HASH_MISMATCH, SOURCE_INVALID, PATH_NOT_DECLARED,
    REFERENCE_DANGLING, REFERENCE_DUPLICATE, REVISION_INVALID,
    REVISION_REFERENCE_INVALID, REVISION_INHERITED_REFERENCE,
    COMPLETED_CRITERIA_NOT_MET.

Codes raised by the trusted helper (for example ``ARCHIVE_HASH_MISMATCH``)
are passed through unchanged.  Error output never contains metadata text,
paths from inside the archive, or file contents.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import zipfile

__all__ = ["HandoffError", "check_handoff", "main"]

_TRACEABILITY_SCHEMA = "collaborator-traceability/1"
_DELIVERY_SCHEMA = "collaborator-delivery/1"
_MANIFEST_SCHEMA = "delivery-manifest/1"

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")

_CRITERION_STATUSES = frozenset({"met", "unmet", "not_run"})
_VERIFICATION_KINDS = frozenset(
    {"static", "unit", "integration", "simulated", "live", "manual"}
)
_EXECUTIONS = frozenset({"current", "inherited", "not_run"})
_VERIFICATION_STATUSES = frozenset({"passed", "failed", "not_run"})

_DELIVERY_HELPER = None


class HandoffError(Exception):
    """Fixed-code failure raised by the traceability checker."""

    ARGS_INVALID = "ARGS_INVALID"
    HELPER_MISSING = "HELPER_MISSING"
    HELPER_INVALID = "HELPER_INVALID"
    IO_ERROR = "IO_ERROR"
    ARCHIVE_INVALID = "ARCHIVE_INVALID"
    METADATA_INVALID = "METADATA_INVALID"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    METADATA_LIMIT = "METADATA_LIMIT"
    MANIFEST_LIMIT = "MANIFEST_LIMIT"
    TRACEABILITY_MISSING = "TRACEABILITY_MISSING"
    TRACEABILITY_INVALID = "TRACEABILITY_INVALID"
    CRITERIA_INVALID = "CRITERIA_INVALID"
    VERIFICATIONS_INVALID = "VERIFICATIONS_INVALID"
    CRITERION_ID_INVALID = "CRITERION_ID_INVALID"
    CRITERION_STATUS_INVALID = "CRITERION_STATUS_INVALID"
    CRITERION_REASON_INVALID = "CRITERION_REASON_INVALID"
    CRITERION_REASON_REQUIRED = "CRITERION_REASON_REQUIRED"
    CRITERION_MET_INVALID = "CRITERION_MET_INVALID"
    VERIFICATION_ID_INVALID = "VERIFICATION_ID_INVALID"
    VERIFICATION_KIND_INVALID = "VERIFICATION_KIND_INVALID"
    VERIFICATION_EXECUTION_INVALID = "VERIFICATION_EXECUTION_INVALID"
    VERIFICATION_STATUS_INVALID = "VERIFICATION_STATUS_INVALID"
    VERIFICATION_STATE_INVALID = "VERIFICATION_STATE_INVALID"
    COMMAND_INVALID = "COMMAND_INVALID"
    CWD_INVALID = "CWD_INVALID"
    ENVIRONMENT_INVALID = "ENVIRONMENT_INVALID"
    EXIT_CODE_INVALID = "EXIT_CODE_INVALID"
    VERIFICATION_STATUS_MISMATCH = "VERIFICATION_STATUS_MISMATCH"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    SUBJECTS_INVALID = "SUBJECTS_INVALID"
    SUBJECT_INVALID = "SUBJECT_INVALID"
    SUBJECT_HASH_MISMATCH = "SUBJECT_HASH_MISMATCH"
    SOURCE_INVALID = "SOURCE_INVALID"
    PATH_NOT_DECLARED = "PATH_NOT_DECLARED"
    REFERENCE_DANGLING = "REFERENCE_DANGLING"
    REFERENCE_DUPLICATE = "REFERENCE_DUPLICATE"
    REVISION_INVALID = "REVISION_INVALID"
    REVISION_REFERENCE_INVALID = "REVISION_REFERENCE_INVALID"
    REVISION_INHERITED_REFERENCE = "REVISION_INHERITED_REFERENCE"
    COMPLETED_CRITERIA_NOT_MET = "COMPLETED_CRITERIA_NOT_MET"

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


# ---------------------------------------------------------------------------
# Trusted helper (must sit beside this script)
# ---------------------------------------------------------------------------


def _load_delivery_helper():
    global _DELIVERY_HELPER
    if _DELIVERY_HELPER is not None:
        return _DELIVERY_HELPER
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delivery.py")
    if not os.path.isfile(path):
        raise HandoffError(
            HandoffError.HELPER_MISSING, "trusted delivery.py is not beside the checker"
        )
    spec = importlib.util.spec_from_file_location(
        "_package_delivery_trusted_for_handoff", path
    )
    if spec is None or spec.loader is None:
        raise HandoffError(HandoffError.HELPER_INVALID, "could not import delivery.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise HandoffError(HandoffError.HELPER_INVALID, "could not import delivery.py") from None
    finally:
        sys.dont_write_bytecode = previous
    _DELIVERY_HELPER = module
    return module


# ---------------------------------------------------------------------------
# Small strict helpers
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


def _loads(data: bytes, code: str):
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise HandoffError(code, "JSON is invalid") from None


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _require_dict(value, code: str, what: str) -> dict:
    if not isinstance(value, dict):
        raise HandoffError(code, "%s must be an object" % what)
    return value


def _require_list(value, code: str, what: str) -> list:
    if not isinstance(value, list):
        raise HandoffError(code, "%s must be a list" % what)
    return value


def _check_id(value, code: str, what: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise HandoffError(code, "%s is not a valid identifier" % what)
    return value


def _check_enum(value, allowed, code: str, what: str) -> str:
    # Reject non-strings before the frozenset lookup: unhashable values such
    # as lists or objects would otherwise raise TypeError instead of the
    # fixed HandoffError the public API and CLI promise.
    if not isinstance(value, str) or value not in allowed:
        raise HandoffError(code, "%s is not a permitted value" % what)
    return value


def _path_key(path: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", path).casefold()


def _declared_path(value, payload: set, code: str):
    if not isinstance(value, str) or value not in payload:
        raise HandoffError(code, "path is not a declared payload file")
    return value


def _check_cwd(value, delivery) -> str:
    if not isinstance(value, str) or not value:
        raise HandoffError(HandoffError.CWD_INVALID, "cwd must be a path string")
    if value == ".":
        return value
    try:
        delivery._validate_rel_path(value)
    except delivery.DeliveryError:
        raise HandoffError(HandoffError.CWD_INVALID, "cwd is not a canonical relative dir") from None
    return value


def _check_args_vector(value, delivery, required: bool) -> None:
    if value is None:
        if required:
            raise HandoffError(HandoffError.COMMAND_INVALID, "command is required")
        return
    if not isinstance(value, list) or not value:
        raise HandoffError(HandoffError.COMMAND_INVALID, "command must be a non-empty argv list")
    for item in value:
        if not isinstance(item, str) or item == "":
            raise HandoffError(HandoffError.COMMAND_INVALID, "argv items must be strings")


def _check_environment(value, required: bool) -> None:
    if value is None:
        if required:
            raise HandoffError(HandoffError.ENVIRONMENT_INVALID, "environment is required")
        return
    if not isinstance(value, dict):
        raise HandoffError(HandoffError.ENVIRONMENT_INVALID, "environment must be an object")
    if required and not value:
        raise HandoffError(HandoffError.ENVIRONMENT_INVALID, "environment must not be empty")
    for key, item in value.items():
        if not isinstance(key, str) or key == "" or not isinstance(item, str):
            raise HandoffError(
                HandoffError.ENVIRONMENT_INVALID, "environment must map strings to strings"
            )


def _read_zip_entry(handle, name: str, limit: int, code: str) -> bytes:
    try:
        info = handle.getinfo(name)
    except KeyError:
        raise HandoffError(HandoffError.ARCHIVE_INVALID, "required entry is missing") from None
    if info.is_dir() or info.file_size > limit:
        raise HandoffError(code, "metadata entry is too large")
    try:
        with handle.open(info, "r") as source:
            data = source.read(limit + 1)
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError):
        raise HandoffError(HandoffError.ARCHIVE_INVALID, "entry is unreadable") from None
    if len(data) > limit:
        raise HandoffError(code, "metadata entry is too large")
    return data


# ---------------------------------------------------------------------------
# Traceability validation
# ---------------------------------------------------------------------------


def _check_argv(value, delivery, required):
    _check_args_vector(value, delivery, required)


def _check_subjects(subjects, payload, manifest_hashes, delivery, required: bool):
    items = _require_list(subjects, HandoffError.SUBJECTS_INVALID, "subjects")
    if required and not items:
        raise HandoffError(HandoffError.SUBJECTS_INVALID, "subjects must not be empty")
    seen = set()
    paths = []
    for item in items:
        subject = _require_dict(item, HandoffError.SUBJECT_INVALID, "subject")
        if set(subject) != {"path", "sha256"}:
            raise HandoffError(HandoffError.SUBJECT_INVALID, "subject has the wrong fields")
        path = _declared_path(subject.get("path"), payload, HandoffError.PATH_NOT_DECLARED)
        digest = subject.get("sha256")
        if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
            raise HandoffError(HandoffError.SUBJECT_INVALID, "subject sha256 is invalid")
        key = _path_key(path)
        if key in seen:
            raise HandoffError(HandoffError.REFERENCE_DUPLICATE, "duplicate subject path")
        seen.add(key)
        expected = manifest_hashes.get(path)
        if expected is None:
            raise HandoffError(HandoffError.MANIFEST_INVALID, "subject is not in the manifest")
        if digest != expected:
            raise HandoffError(
                HandoffError.SUBJECT_HASH_MISMATCH,
                "subject digest does not match the delivered file",
            )
        paths.append(path)
    return paths


def _check_verification(item, payload, manifest_hashes, delivery):
    record = _require_dict(item, HandoffError.VERIFICATIONS_INVALID, "verification")
    required = {
        "id",
        "kind",
        "execution",
        "status",
        "command",
        "cwd",
        "environment",
        "exit_code",
        "evidence",
        "subjects",
        "reason",
        "source",
    }
    if not required.issubset(record):
        raise HandoffError(HandoffError.VERIFICATIONS_INVALID, "verification is missing fields")

    vid = _check_id(record["id"], HandoffError.VERIFICATION_ID_INVALID, "verification id")
    kind = _check_enum(
        record["kind"],
        _VERIFICATION_KINDS,
        HandoffError.VERIFICATION_KIND_INVALID,
        "verification kind",
    )
    execution = _check_enum(
        record["execution"],
        _EXECUTIONS,
        HandoffError.VERIFICATION_EXECUTION_INVALID,
        "verification execution",
    )
    status = _check_enum(
        record["status"],
        _VERIFICATION_STATUSES,
        HandoffError.VERIFICATION_STATUS_INVALID,
        "verification status",
    )

    cwd = _check_cwd(record["cwd"], delivery)
    command = record["command"]
    environment = record["environment"]
    exit_code = record["exit_code"]
    evidence = record["evidence"]
    source = record["source"]
    reason = record["reason"]

    if reason is not None and (not isinstance(reason, str) or reason.strip() == ""):
        raise HandoffError(
            HandoffError.CRITERION_REASON_INVALID, "reason must be null or non-empty text"
        )

    if execution == "not_run":
        if status != "not_run":
            raise HandoffError(
                HandoffError.VERIFICATION_STATE_INVALID,
                "not_run execution must use not_run status",
            )
        if exit_code is not None:
            raise HandoffError(HandoffError.EXIT_CODE_INVALID, "not_run needs exit_code=null")
        if not _nonempty_str(reason):
            raise HandoffError(HandoffError.CRITERION_REASON_REQUIRED, "not_run needs a reason")
        _check_argv(command, delivery, required=False)
        if evidence is not None:
            _declared_path(evidence, payload, HandoffError.EVIDENCE_INVALID)
        _check_environment(environment, required=False)
        subject_paths = _check_subjects(
            record["subjects"], payload, manifest_hashes, delivery, required=False
        )
        if source is not None:
            _declared_path(source, payload, HandoffError.SOURCE_INVALID)
        return {
            "id": vid,
            "execution": execution,
            "status": status,
            "cwd": cwd,
            "subjects": subject_paths,
        }

    # execution is current or inherited: it must have actually been run.
    if status == "not_run":
        raise HandoffError(
            HandoffError.VERIFICATION_STATE_INVALID,
            "current/inherited execution may not be not_run",
        )
    _check_argv(command, delivery, required=True)
    _check_environment(environment, required=True)
    if type(exit_code) is not int:
        raise HandoffError(HandoffError.EXIT_CODE_INVALID, "exit_code must be an int")
    if status == "passed" and exit_code != 0:
        raise HandoffError(
            HandoffError.VERIFICATION_STATUS_MISMATCH, "passed needs exit_code 0"
        )
    if status == "failed" and exit_code == 0:
        raise HandoffError(
            HandoffError.VERIFICATION_STATUS_MISMATCH, "failed needs a non-zero exit_code"
        )
    _declared_path(evidence, payload, HandoffError.EVIDENCE_INVALID)
    subject_paths = _check_subjects(
        record["subjects"], payload, manifest_hashes, delivery, required=True
    )

    if source is not None:
        _declared_path(source, payload, HandoffError.SOURCE_INVALID)

    if execution == "inherited":
        if not _nonempty_str(reason):
            raise HandoffError(
                HandoffError.CRITERION_REASON_REQUIRED,
                "inherited verification needs a reason describing its limits",
            )
        if source is None:
            raise HandoffError(
                HandoffError.SOURCE_INVALID,
                "inherited verification needs a declared source evidence file",
            )
    return {
        "id": vid,
        "execution": execution,
        "status": status,
        "cwd": cwd,
        "subjects": subject_paths,
    }


def _check_criteria(item, payload):
    record = _require_dict(item, HandoffError.CRITERIA_INVALID, "criterion")
    required = {"id", "requirement", "status", "artifacts", "verification_ids", "reason"}
    if not required.issubset(record):
        raise HandoffError(HandoffError.CRITERIA_INVALID, "criterion is missing fields")

    cid = _check_id(record["id"], HandoffError.CRITERION_ID_INVALID, "criterion id")
    if not _nonempty_str(record["requirement"]):
        raise HandoffError(HandoffError.CRITERIA_INVALID, "requirement must be non-empty")
    status = _check_enum(
        record["status"],
        _CRITERION_STATUSES,
        HandoffError.CRITERION_STATUS_INVALID,
        "criterion status",
    )

    artifacts = _require_list(
        record["artifacts"], HandoffError.CRITERIA_INVALID, "artifacts"
    )
    artifact_keys = set()
    clean_artifacts = []
    for artifact in artifacts:
        path = _declared_path(artifact, payload, HandoffError.PATH_NOT_DECLARED)
        key = _path_key(path)
        if key in artifact_keys:
            raise HandoffError(HandoffError.REFERENCE_DUPLICATE, "duplicate artifact path")
        artifact_keys.add(key)
        clean_artifacts.append(path)

    refs = _require_list(
        record["verification_ids"], HandoffError.CRITERIA_INVALID, "verification_ids"
    )
    ref_keys = set()
    for ref in refs:
        _check_id(ref, HandoffError.VERIFICATION_ID_INVALID, "verification reference")
        if ref in ref_keys:
            raise HandoffError(
                HandoffError.REFERENCE_DUPLICATE, "duplicate verification reference"
            )
        ref_keys.add(ref)

    reason = record["reason"]
    if reason is not None and (not isinstance(reason, str) or reason.strip() == ""):
        raise HandoffError(
            HandoffError.CRITERION_REASON_INVALID, "reason must be null or non-empty text"
        )
    if status in ("unmet", "not_run") and not _nonempty_str(reason):
        raise HandoffError(
            HandoffError.CRITERION_REASON_REQUIRED, "unmet/not_run criteria need a reason"
        )
    if status == "met":
        if not clean_artifacts:
            raise HandoffError(
                HandoffError.CRITERION_MET_INVALID, "met criterion needs an artifact"
            )
        if not refs:
            raise HandoffError(
                HandoffError.CRITERION_MET_INVALID, "met criterion needs a verification"
            )

    return {
        "id": cid,
        "status": status,
        "artifacts": artifact_keys,
        "verification_ids": list(refs),
    }


def _check_revision(revision, criteria_by_id, verifications_by_id):
    if revision is None:
        return
    record = _require_dict(revision, HandoffError.REVISION_INVALID, "revision")
    if set(record) != {"previous_archive_sha256", "changes"}:
        raise HandoffError(HandoffError.REVISION_INVALID, "revision has the wrong fields")
    previous = record["previous_archive_sha256"]
    if not isinstance(previous, str) or _SHA_RE.fullmatch(previous) is None:
        raise HandoffError(
            HandoffError.REVISION_INVALID, "previous_archive_sha256 is invalid"
        )
    changes = _require_list(record["changes"], HandoffError.REVISION_INVALID, "changes")
    if not changes:
        raise HandoffError(HandoffError.REVISION_INVALID, "changes must not be empty")

    for item in changes:
        change = _require_dict(item, HandoffError.REVISION_INVALID, "change")
        if set(change) != {"criterion_id", "description", "verification_ids"}:
            raise HandoffError(HandoffError.REVISION_INVALID, "change has the wrong fields")
        criterion_id = change["criterion_id"]
        if not isinstance(criterion_id, str) or criterion_id not in criteria_by_id:
            raise HandoffError(
                HandoffError.REVISION_REFERENCE_INVALID,
                "change must reference an existing criterion",
            )
        if not _nonempty_str(change["description"]):
            raise HandoffError(HandoffError.REVISION_INVALID, "change description is required")
        refs = _require_list(
            change["verification_ids"], HandoffError.REVISION_INVALID, "verification_ids"
        )
        if not refs:
            raise HandoffError(
                HandoffError.REVISION_INVALID, "change must reference verification records"
            )
        seen = set()
        for ref in refs:
            if not isinstance(ref, str) or ref not in verifications_by_id:
                raise HandoffError(
                    HandoffError.REVISION_REFERENCE_INVALID,
                    "change references an unknown verification",
                )
            if ref in seen:
                raise HandoffError(
                    HandoffError.REFERENCE_DUPLICATE, "duplicate verification reference"
                )
            seen.add(ref)
            if verifications_by_id[ref]["execution"] == "inherited":
                raise HandoffError(
                    HandoffError.REVISION_INHERITED_REFERENCE,
                    "a revision may not rely only on inherited evidence",
                )


def _check_traceability(trace, meta, payload, manifest_hashes, delivery) -> dict:
    trace = _require_dict(trace, HandoffError.TRACEABILITY_INVALID, "traceability")
    if trace.get("schema_version") != _TRACEABILITY_SCHEMA:
        raise HandoffError(HandoffError.TRACEABILITY_INVALID, "bad traceability schema")

    criteria = _require_list(
        trace.get("criteria"), HandoffError.CRITERIA_INVALID, "criteria"
    )
    if not criteria:
        raise HandoffError(HandoffError.CRITERIA_INVALID, "criteria must not be empty")
    verifications = _require_list(
        trace.get("verifications"), HandoffError.VERIFICATIONS_INVALID, "verifications"
    )
    if not verifications:
        raise HandoffError(
            HandoffError.VERIFICATIONS_INVALID, "verifications must not be empty"
        )

    parsed_criteria = []
    criteria_by_id = {}
    for item in criteria:
        parsed = _check_criteria(item, payload)
        if parsed["id"] in criteria_by_id:
            raise HandoffError(HandoffError.CRITERIA_INVALID, "duplicate criterion id")
        criteria_by_id[parsed["id"]] = parsed
        parsed_criteria.append(parsed)

    parsed_verifications = []
    verifications_by_id = {}
    for item in verifications:
        parsed = _check_verification(item, payload, manifest_hashes, delivery)
        if parsed["id"] in verifications_by_id:
            raise HandoffError(
                HandoffError.VERIFICATIONS_INVALID, "duplicate verification id"
            )
        verifications_by_id[parsed["id"]] = parsed
        parsed_verifications.append(parsed)

    for parsed in parsed_criteria:
        covered = set()
        for ref in parsed["verification_ids"]:
            record = verifications_by_id.get(ref)
            if record is None:
                raise HandoffError(
                    HandoffError.REFERENCE_DANGLING,
                    "criterion references an unknown verification",
                )
            if parsed["status"] == "met" and record["status"] != "passed":
                raise HandoffError(
                    HandoffError.CRITERION_MET_INVALID,
                    "a met criterion needs passed verification records",
                )
            if record["status"] == "passed":
                covered.update(_path_key(path) for path in record["subjects"])
        # The artifacts a criterion claims must be covered by the union of the
        # subjects its linked passed verifications actually exercised.  Unmet
        # or not_run criteria make no such claim, so they need no coverage.
        if parsed["status"] == "met" and parsed["artifacts"] - covered:
            raise HandoffError(
                HandoffError.CRITERION_MET_INVALID,
                "a met criterion artifact is not covered by a passed verification subject",
            )

    _check_revision(trace.get("revision"), criteria_by_id, verifications_by_id)

    if meta.get("status") == "completed":
        for parsed in parsed_criteria:
            if parsed["status"] != "met":
                raise HandoffError(
                    HandoffError.COMPLETED_CRITERIA_NOT_MET,
                    "completed delivery has a criterion that is not met",
                )

    return {
        "criteria_count": len(parsed_criteria),
        "verification_count": len(parsed_verifications),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_handoff(archive):
    """Verify ``archive`` structurally, then check its traceability extension."""
    try:
        archive_path = os.fspath(archive)
    except TypeError:
        raise HandoffError(HandoffError.ARCHIVE_INVALID, "archive must be a path") from None
    if not isinstance(archive_path, str):
        raise HandoffError(HandoffError.ARCHIVE_INVALID, "archive must be a str path")

    delivery = _load_delivery_helper()
    try:
        stats = delivery.verify_delivery(archive_path)
    except delivery.DeliveryError as exc:
        raise HandoffError(exc.code, "the trusted delivery verifier rejected the archive") from None
    except OSError:
        raise HandoffError(HandoffError.IO_ERROR, "could not read the archive") from None

    limits = delivery.DEFAULT_LIMITS
    try:
        with zipfile.ZipFile(archive_path, "r") as handle:
            delivery_bytes = _read_zip_entry(
                handle,
                "DELIVERY.json",
                limits["max_metadata_bytes"],
                HandoffError.METADATA_LIMIT,
            )
            manifest_bytes = _read_zip_entry(
                handle,
                "MANIFEST.json",
                limits["max_manifest_bytes"],
                HandoffError.MANIFEST_LIMIT,
            )
    except HandoffError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, NotImplementedError):
        raise HandoffError(HandoffError.ARCHIVE_INVALID, "archive is unreadable") from None

    meta = _loads(delivery_bytes, HandoffError.METADATA_INVALID)
    if not isinstance(meta, dict) or meta.get("schema_version") != _DELIVERY_SCHEMA:
        raise HandoffError(HandoffError.METADATA_INVALID, "bad delivery metadata")
    raw_files = meta.get("files")
    if not isinstance(raw_files, list):
        raise HandoffError(HandoffError.METADATA_INVALID, "metadata files are invalid")
    payload = set(raw_files)

    manifest = _loads(manifest_bytes, HandoffError.MANIFEST_INVALID)
    manifest_hashes = _parse_manifest_hashes(manifest)

    if "traceability" not in meta:
        raise HandoffError(
            HandoffError.TRACEABILITY_MISSING, "DELIVERY.json has no traceability extension"
        )

    counts = _check_traceability(
        meta["traceability"], meta, payload, manifest_hashes, delivery
    )
    return {
        "archive_sha256": stats["archive_sha256"],
        "criteria_count": counts["criteria_count"],
        "verification_count": counts["verification_count"],
        "independent_acceptance": "UNKNOWN",
    }


def _parse_manifest_hashes(manifest) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != _MANIFEST_SCHEMA:
        raise HandoffError(HandoffError.MANIFEST_INVALID, "bad manifest object")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise HandoffError(HandoffError.MANIFEST_INVALID, "manifest files are invalid")
    result = {}
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise HandoffError(HandoffError.MANIFEST_INVALID, "bad manifest entry")
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise HandoffError(HandoffError.MANIFEST_INVALID, "bad manifest entry")
        if _SHA_RE.fullmatch(digest) is None:
            raise HandoffError(HandoffError.MANIFEST_INVALID, "bad manifest digest")
        if type(item.get("bytes")) is not int or item["bytes"] < 0:
            raise HandoffError(HandoffError.MANIFEST_INVALID, "bad manifest size")
        if path in result:
            raise HandoffError(HandoffError.MANIFEST_INVALID, "duplicate manifest path")
        result[path] = digest
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):  # noqa: D401 - argparse hook
        raise HandoffError(HandoffError.ARGS_INVALID, "invalid arguments")


def main(argv=None) -> int:
    parser = _ArgumentParser(
        prog="check_handoff.py",
        description="Check a collaborator-delivery/1 ZIP traceability extension.",
    )
    parser.add_argument("archive", help="delivery ZIP path")
    try:
        args = parser.parse_args(argv)
        result = check_handoff(args.archive)
    except HandoffError as exc:
        print(
            json.dumps({"error": exc.code}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1
    except OSError:
        print(
            json.dumps({"error": HandoffError.IO_ERROR}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
