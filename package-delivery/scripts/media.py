"""Optional audio/video delivery inspection for ``collaborator-delivery/1``.

This module is a *narrow extension* of the trusted ``delivery.py`` helper.  It
does not pack, transcode, download, upload, render, or play anything.  It only
reads the explicit ``media`` declaration in ``DELIVERY.json``, hashes the
declared files, asks a local ``ffprobe`` for technical stream facts, and
returns a JSON report.  The original pack/verify path stays responsible for
package integrity; a missing ``media`` object is reported as ``MEDIA_MISSING``
instead of scanning the project for media files.

Usage::

    python media.py inspect --root staging --out evidence/new-report.json \\
        [--profile standard|large] [--ffprobe /trusted/path/ffprobe] \\
        [--timeout 15]

Public API::

    inspect_media(root, *, profile="standard", ffprobe=None, timeout=15) -> dict
    MediaError.code  # fixed, machine-readable failure code

Python 3.11+ standard library only.  The module imports the read-only trusted
helpers from the sibling ``delivery.py`` so path, metadata, JSON, and limit
rules are identical to the base tool.

Technical status is not independent acceptance.  Every report carries
``independent_acceptance`` = ``"UNKNOWN"`` and a ``manual_checks`` list whose
items stay ``not_run`` because visual quality, listening tests, subtitle or
transcript timing, and editable-project integrity require a human reviewer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import threading

from delivery import (
    DeliveryError,
    _coerce_path,
    _ensure_regular_file,
    _is_link_like,
    _loads_json,
    _path_key,
    _read_metadata_file,
    _validate_metadata,
    _validate_rel_path,
    _validate_root,
    get_limits,
)

__all__ = ["MediaError", "inspect_media", "main"]

#: Schema identifier written into the inspection report.
REPORT_SCHEMA = "media-inspection/1"
#: Schema identifier required inside the optional ``media`` declaration.
MEDIA_SCHEMA = "collaborator-media/1"

MAX_ASSETS = 32
MAX_STREAMS = 64

#: Report check code used when an automatically discovered ffprobe resolves
#: inside the inspected root.  Such a candidate is never executed.
TOOL_UNTRUSTED = "TOOL_UNTRUSTED"

#: Hard caps for one bounded child process.  These are intentionally smaller
#: than any plausible media file: ffprobe only needs to describe the file.
STDOUT_CAP = 1024 * 1024
STDERR_CAP = 64 * 1024
_READ_CHUNK = 8192
_HASH_CHUNK = 64 * 1024

#: Network-free, local-file-only demuxer allow list and a bounded analysis
#: budget.  ``-protocol_whitelist file`` also prevents URL inputs.
FORMAT_WHITELIST = "mov,matroska,avi,wav,mp3,flac,ogg,aac"
PROBESIZE = 5_000_000
ANALYZEDURATION = 5_000_000
_SHOW_ENTRIES = (
    "format=format_name,duration:"
    "stream=index,codec_type,codec_name,width,height,sample_rate,channels,duration:"
    "stream_disposition=attached_pic"
)

COMPANION_ROLES = frozenset(
    {"subtitle", "transcript", "editable_source", "preview", "cover"}
)
_ASSET_KEYS = frozenset({"path", "kind", "require_audio", "expected", "companions"})
_EXPECTED_KEYS = frozenset(
    {
        "width",
        "height",
        "sample_rate",
        "channels",
        "min_duration_seconds",
        "max_duration_seconds",
    }
)
_COMPANION_KEYS = frozenset({"path", "role"})

_MANUAL_CHECK_NAMES = (
    ("visual_quality", "visual quality needs a human viewer"),
    ("audio_quality", "audio quality needs human listening"),
    (
        "subtitle_or_transcript_sync",
        "subtitle/transcript meaning and timing sync need a human reviewer",
    ),
    (
        "editable_project_integrity",
        "editable project completeness needs a human reviewer",
    ),
)

_DELIVERY_PASSTHROUGH = frozenset(
    {
        "INVALID_PROFILE",
        "ROOT_INVALID",
        "PATH_INVALID",
        "PATH_SECRET",
        "PATH_UNSAFE",
        "FILE_MISSING",
        "FILE_NOT_REGULAR",
        "METADATA_MISSING",
        "METADATA_INVALID",
        "METADATA_LIMIT",
        "METADATA_ACCEPTANCE_CLAIM",
        "IO_ERROR",
    }
)


class MediaError(Exception):
    """Fixed-code failure raised by media inspection.

    ``code`` is one of the stable identifiers documented in
    ``docs/media-delivery.md``.  Human-readable messages are deliberately
    generic: paths, metadata text, probe output, and file content are never
    echoed to the caller or the CLI error stream.
    """

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_PROFILE = "INVALID_PROFILE"
    INVALID_TIMEOUT = "INVALID_TIMEOUT"
    INVALID_FFPROBE = "INVALID_FFPROBE"

    ROOT_INVALID = "ROOT_INVALID"
    METADATA_MISSING = "METADATA_MISSING"
    METADATA_INVALID = "METADATA_INVALID"
    METADATA_LIMIT = "METADATA_LIMIT"
    METADATA_ACCEPTANCE_CLAIM = "METADATA_ACCEPTANCE_CLAIM"

    MEDIA_MISSING = "MEDIA_MISSING"
    MEDIA_INVALID = "MEDIA_INVALID"
    ASSET_INVALID = "ASSET_INVALID"
    ASSET_DUPLICATE = "ASSET_DUPLICATE"
    EXPECTED_INVALID = "EXPECTED_INVALID"
    COMPANION_INVALID = "COMPANION_INVALID"

    PATH_INVALID = "PATH_INVALID"
    PATH_SECRET = "PATH_SECRET"
    PATH_UNSAFE = "PATH_UNSAFE"
    FILE_MISSING = "FILE_MISSING"
    FILE_NOT_REGULAR = "FILE_NOT_REGULAR"
    FILE_LIMIT = "FILE_LIMIT"
    TOTAL_LIMIT = "TOTAL_LIMIT"
    SOURCE_CHANGED = "SOURCE_CHANGED"

    PROBE_LAUNCH = "PROBE_LAUNCH"
    PROBE_TIMEOUT = "PROBE_TIMEOUT"
    PROBE_OUTPUT_LIMIT = "PROBE_OUTPUT_LIMIT"
    PROBE_FAILED = "PROBE_FAILED"
    PROBE_OUTPUT_INVALID = "PROBE_OUTPUT_INVALID"

    OUTPUT_INVALID = "OUTPUT_INVALID"
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    OUTPUT_PARENT_MISSING = "OUTPUT_PARENT_MISSING"
    OUTPUT_UNSAFE = "OUTPUT_UNSAFE"
    OUTPUT_COLLISION = "OUTPUT_COLLISION"
    IO_ERROR = "IO_ERROR"

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


# ---------------------------------------------------------------------------
# Trusted-helper adapters
# ---------------------------------------------------------------------------


_MEDIA_CODES = frozenset(
    value
    for name, value in vars(MediaError).items()
    if name.isupper() and isinstance(value, str)
)


def _map_delivery_error(exc: DeliveryError) -> MediaError:
    code = getattr(exc, "code", "")
    if code in _DELIVERY_PASSTHROUGH or code in _MEDIA_CODES:
        return MediaError(code)
    if code == "FILES_INVALID":
        return MediaError(MediaError.METADATA_INVALID)
    if code == "INVALID_LIMITS":
        return MediaError(MediaError.INVALID_PROFILE)
    return MediaError(MediaError.MEDIA_INVALID)


def _call(function, *args, **kwargs):
    """Call a trusted helper and translate ``DeliveryError`` to ``MediaError``."""
    try:
        return function(*args, **kwargs)
    except DeliveryError as exc:
        raise _map_delivery_error(exc) from None


def _validate_path(path: str) -> None:
    try:
        _validate_rel_path(path)
    except DeliveryError as exc:
        raise _map_delivery_error(exc) from None


# ---------------------------------------------------------------------------
# Declaration validation
# ---------------------------------------------------------------------------


def _normalise_media(meta: dict):
    if "media" not in meta:
        raise MediaError(MediaError.MEDIA_MISSING, "media declaration is missing")
    media = meta["media"]
    if not isinstance(media, dict):
        raise MediaError(MediaError.MEDIA_INVALID, "media must be an object")
    if set(media) != {"schema_version", "assets"}:
        raise MediaError(MediaError.MEDIA_INVALID, "unexpected media field")
    if media.get("schema_version") != MEDIA_SCHEMA:
        raise MediaError(MediaError.MEDIA_INVALID, "bad media schema_version")

    raw_assets = media.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise MediaError(MediaError.MEDIA_INVALID, "assets must be a non-empty list")
    if len(raw_assets) > MAX_ASSETS:
        raise MediaError(MediaError.MEDIA_INVALID, "too many assets")

    files = set(meta["files"])
    assets = []
    seen = set()
    for raw in raw_assets:
        asset = _normalise_asset(raw, files, seen)
        assets.append(asset)
    return assets


def _normalise_asset(raw, files, seen):
    if not isinstance(raw, dict):
        raise MediaError(MediaError.ASSET_INVALID, "asset must be an object")
    if set(raw) - _ASSET_KEYS:
        raise MediaError(MediaError.ASSET_INVALID, "unexpected asset field")

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise MediaError(MediaError.ASSET_INVALID, "asset path is required")
    _validate_path(path)
    if path not in files:
        raise MediaError(MediaError.ASSET_INVALID, "asset path is not in files")

    key = _path_key(path)
    if key in seen:
        raise MediaError(MediaError.ASSET_DUPLICATE, "duplicate asset path")
    seen.add(key)

    kind = raw.get("kind")
    if kind not in ("video", "audio"):
        raise MediaError(MediaError.ASSET_INVALID, "asset kind must be video or audio")

    require_audio = False
    if "require_audio" in raw:
        if kind != "video":
            raise MediaError(MediaError.ASSET_INVALID, "require_audio is video-only")
        if type(raw["require_audio"]) is not bool:
            raise MediaError(MediaError.ASSET_INVALID, "require_audio must be a bool")
        require_audio = raw["require_audio"]

    expected = _normalise_expected(
        raw["expected"] if "expected" in raw else {}, kind
    )
    companions = _normalise_companions(raw.get("companions", []), files)
    return {
        "path": path,
        "kind": kind,
        "require_audio": require_audio,
        "expected": expected,
        "companions": companions,
        "source": None,
    }


def _normalise_expected(raw, kind):
    if not isinstance(raw, dict):
        raise MediaError(MediaError.EXPECTED_INVALID, "expected must be an object")
    if set(raw) - _EXPECTED_KEYS:
        raise MediaError(MediaError.EXPECTED_INVALID, "unexpected expected field")

    expected = {}
    for key in ("width", "height"):
        if key in raw:
            if kind != "video":
                raise MediaError(
                    MediaError.EXPECTED_INVALID, "width/height are video-only"
                )
            value = raw[key]
            if type(value) is not int or value <= 0:
                raise MediaError(
                    MediaError.EXPECTED_INVALID, "width/height must be positive ints"
                )
            expected[key] = value

    for key in ("sample_rate", "channels"):
        if key in raw:
            value = raw[key]
            if type(value) is not int or value <= 0:
                raise MediaError(
                    MediaError.EXPECTED_INVALID, "sample_rate/channels must be positive ints"
                )
            expected[key] = value

    for key in ("min_duration_seconds", "max_duration_seconds"):
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MediaError(
                    MediaError.EXPECTED_INVALID, "duration bound must be a number"
                )
            try:
                number = float(value)
            except (OverflowError, ValueError):
                raise MediaError(
                    MediaError.EXPECTED_INVALID, "duration bound is not representable"
                ) from None
            if not math.isfinite(number) or number <= 0:
                raise MediaError(
                    MediaError.EXPECTED_INVALID, "duration bound must be finite and positive"
                )
            expected[key] = number

    if (
        "min_duration_seconds" in expected
        and "max_duration_seconds" in expected
        and expected["min_duration_seconds"] > expected["max_duration_seconds"]
    ):
        raise MediaError(MediaError.EXPECTED_INVALID, "min duration exceeds max duration")
    return expected


def _normalise_companions(raw, files):
    if raw is None:
        raise MediaError(MediaError.COMPANION_INVALID, "companions must be a list")
    if not isinstance(raw, list):
        raise MediaError(MediaError.COMPANION_INVALID, "companions must be a list")
    companions = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != _COMPANION_KEYS:
            raise MediaError(MediaError.COMPANION_INVALID, "bad companion object")
        path = item.get("path")
        role = item.get("role")
        if not isinstance(path, str) or not path:
            raise MediaError(MediaError.COMPANION_INVALID, "companion path is required")
        _validate_path(path)
        if path not in files:
            raise MediaError(
                MediaError.COMPANION_INVALID, "companion path is not in files"
            )
        if role not in COMPANION_ROLES:
            raise MediaError(MediaError.COMPANION_INVALID, "bad companion role")
        key = _path_key(path)
        if key in seen:
            raise MediaError(
                MediaError.COMPANION_INVALID, "duplicate companion path"
            )
        seen.add(key)
        companions.append({"path": path, "role": role, "source": None})
    return companions


# ---------------------------------------------------------------------------
# Root / metadata / declared-path preparation
# ---------------------------------------------------------------------------


def _coerce_timeout(timeout):
    if isinstance(timeout, bool):
        raise MediaError(MediaError.INVALID_TIMEOUT, "timeout must be a number")
    if isinstance(timeout, str):
        try:
            number = float(timeout.strip())
        except (TypeError, ValueError):
            raise MediaError(MediaError.INVALID_TIMEOUT, "timeout must be a number") from None
    elif isinstance(timeout, (int, float)):
        number = float(timeout)
    else:
        raise MediaError(MediaError.INVALID_TIMEOUT, "timeout must be a number")
    if not math.isfinite(number) or not (1 <= number <= 60):
        raise MediaError(
            MediaError.INVALID_TIMEOUT, "timeout must be finite and within 1..60"
        )
    return number


def _prepare(root, *, profile="standard"):
    limits = _call(get_limits, profile)
    root_path = _call(_validate_root, _call(_coerce_path, root, MediaError.ROOT_INVALID))
    metadata_bytes = _call(
        _read_metadata_file, root_path, limits["max_metadata_bytes"]
    )
    meta = _call(_loads_json, metadata_bytes, MediaError.METADATA_INVALID)
    _call(_validate_metadata, meta)

    assets = _normalise_media(meta)
    for asset in assets:
        asset["source"] = _resolve_declared(
            root_path, asset["path"], MediaError.ASSET_INVALID
        )
        for companion in asset["companions"]:
            companion["source"] = _resolve_declared(
                root_path, companion["path"], MediaError.COMPANION_INVALID
            )
    return {
        "root": root_path,
        "profile": profile,
        "limits": limits,
        "meta": meta,
        "assets": assets,
    }


def _resolve_declared(root, path, invalid_code):
    """Resolve a declared path with trusted rules.

    A missing file returns ``None`` so the asset can be reported as failed.
    A link, junction, secret path, or traversal aborts the run.  A declared
    directory is a declaration error, because media and companions name files.
    """
    try:
        segments = _validate_rel_path(path)
    except DeliveryError as exc:
        raise _map_delivery_error(exc) from None
    try:
        return _ensure_regular_file(root, segments)
    except DeliveryError as exc:
        if exc.code == DeliveryError.FILE_MISSING:
            return None
        if exc.code == DeliveryError.FILE_NOT_REGULAR:
            raise MediaError(invalid_code, "declared path is not a regular file") from None
        raise _map_delivery_error(exc) from None


# ---------------------------------------------------------------------------
# Bounded child process runner
# ---------------------------------------------------------------------------


def _kill_process(process) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _run_bounded(argv, timeout, *, stdout_cap=STDOUT_CAP, stderr_cap=STDERR_CAP):
    """Run one child with hard time and output caps.

    Two reader threads collect stdout/stderr in fixed-size chunks.  A cap
    violation kills the child immediately; ``communicate()`` is deliberately
    not used because it would buffer unbounded output before the cap could be
    enforced.  ``shell`` is always ``False`` and no environment is modified.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise MediaError(MediaError.INVALID_TIMEOUT, "timeout must be positive")

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            creationflags=creationflags,
        )
    except OSError:
        return {"status": "launch_error", "returncode": None, "stdout": b"", "stderr": b""}

    state = {"limit": False}
    lock = threading.Lock()
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()

    def _drain(stream, cap, sink):
        try:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                overflow = False
                with lock:
                    if not state["limit"]:
                        if len(sink) + len(chunk) > cap:
                            state["limit"] = True
                            overflow = True
                        else:
                            sink.extend(chunk)
                if overflow:
                    _kill_process(process)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    threads = [
        threading.Thread(
            target=_drain, args=(process.stdout, stdout_cap, stdout_buffer), daemon=True
        ),
        threading.Thread(
            target=_drain, args=(process.stderr, stderr_cap, stderr_buffer), daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    for thread in threads:
        thread.join(timeout=5)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    if timed_out:
        status = "timeout"
    elif state["limit"]:
        status = "output_limit"
    elif process.returncode == 0:
        status = "ok"
    else:
        status = "nonzero"
    return {
        "status": status,
        "returncode": process.returncode,
        "stdout": bytes(stdout_buffer),
        "stderr": bytes(stderr_buffer),
    }


def _tool_version(stdout: bytes):
    text = stdout.decode("utf-8", "replace")
    for line in text.splitlines():
        line = "".join(ch for ch in line.strip() if ch.isprintable())
        if line:
            return line[:200]
    return None


_TOOL_UNTRUSTED_REASON = (
    "auto-discovered ffprobe resolves inside the inspected root; "
    "pass an explicit trusted local tool"
)
_AUTO_TOOL_UNTRUSTED = object()


def _is_within(parent, child):
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def _inside_root(root, candidate):
    """Return True when ``candidate`` is inside ``root`` in either path form.

    Automatic discovery must never start package content.  Windows
    ``shutil.which`` can return a name in the current directory, and a
    symlink/junction may point back into the inspected root, so both the
    literal path and the canonical (realpath, case-normalised) path are
    checked.  An unresolvable location fails closed.
    """
    root_literal = os.path.normcase(os.path.abspath(root))
    candidate_literal = os.path.normcase(os.path.abspath(candidate))
    try:
        root_canonical = os.path.normcase(os.path.realpath(root_literal))
        candidate_canonical = os.path.normcase(
            os.path.realpath(candidate_literal)
        )
    except (OSError, ValueError):
        return True
    for parent in (root_literal, root_canonical):
        for path in (candidate_literal, candidate_canonical):
            if _is_within(parent, path):
                return True
    return False


def _resolve_tool(ffprobe, root=None):
    if ffprobe is None:
        candidate = shutil.which("ffprobe")
        if candidate is None:
            return None
        if root is not None and _inside_root(root, candidate):
            return _AUTO_TOOL_UNTRUSTED
        return candidate
    path = _call(_coerce_path, ffprobe, MediaError.INVALID_FFPROBE)
    if not os.path.isabs(path):
        raise MediaError(MediaError.INVALID_FFPROBE, "ffprobe path must be absolute")
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return path


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _success(size, digest, identity):
    return {
        "status": "passed",
        "code": None,
        "bytes": size,
        "sha256": digest,
        "identity": identity,
    }


def _failure(code, size=None):
    return {
        "status": "failed",
        "code": code,
        "bytes": size,
        "sha256": None,
        "identity": None,
    }


class _FileCache:
    """Chunked SHA-256 with per-file and unique-total profile limits."""

    def __init__(self, limits):
        self._limits = limits
        self._results = {}
        self._total = 0

    def get(self, path):
        key = os.path.normcase(os.path.abspath(path))
        if key not in self._results:
            self._results[key] = self._read(path)
        return self._results[key]

    def _read(self, path):
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            return _failure(MediaError.FILE_MISSING)
        except OSError:
            return _failure(MediaError.IO_ERROR)

        try:
            st = os.fstat(descriptor)
            if not stat.S_ISREG(st.st_mode):
                return _failure(MediaError.FILE_NOT_REGULAR)
            size = st.st_size
            identity = _identity(st)
            if size > self._limits["max_file_bytes"]:
                return _failure(MediaError.FILE_LIMIT, size)
            if self._total + size > self._limits["max_total_bytes"]:
                return _failure(MediaError.TOTAL_LIMIT, size)

            digest = hashlib.sha256()
            count = 0
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                while True:
                    chunk = handle.read(_HASH_CHUNK)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > self._limits["max_file_bytes"]:
                        return _failure(MediaError.FILE_LIMIT, size)
                    if self._total + count > self._limits["max_total_bytes"]:
                        return _failure(MediaError.TOTAL_LIMIT, size)
                    digest.update(chunk)
                after = os.fstat(handle.fileno())

            if count != size or _identity(after) != identity:
                return _failure(MediaError.SOURCE_CHANGED, count)
            self._total += count
            return _success(count, digest.hexdigest(), identity)
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Probe output normalization
# ---------------------------------------------------------------------------


def _parse_number(value, *, integer=False, positive=False):
    if value is None:
        return None
    if isinstance(value, bool):
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad number")
    if isinstance(value, str):
        text = value.strip()
        if text.upper() in ("N/A", "NA"):
            return None
        try:
            number = float(text)
        except (OverflowError, ValueError):
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad number") from None
    elif isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad number") from None
    else:
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad number")

    if not math.isfinite(number):
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "non-finite number")
    if integer:
        if number != int(number):
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "non-integral number")
        number = int(number)
    if positive and number <= 0:
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "non-positive number")
    return number


def _parse_flag(stream):
    value = stream.get("attached_pic")
    if value is None:
        disposition = stream.get("disposition")
        if disposition is not None:
            if not isinstance(disposition, dict):
                raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad disposition")
            value = disposition.get("attached_pic")
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return value.strip() == "1"
    raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad attached_pic")


def _clean_token(value):
    """Keep one short printable technical token; never a path or metadata blob."""
    if value is None:
        return None
    text = "".join(ch for ch in value if ch.isprintable())
    return text[:128]


def _normalise_probe(obj):
    if not isinstance(obj, dict):
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "probe output must be an object")

    format_raw = obj.get("format", {})
    if format_raw is None:
        format_raw = {}
    if not isinstance(format_raw, dict):
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad format object")

    format_name = format_raw.get("format_name")
    if format_name is not None and not isinstance(format_name, str):
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad format_name")
    format_name = _clean_token(format_name)
    format_duration = _parse_number(format_raw.get("duration"))
    if format_duration is not None and format_duration < 0:
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "negative duration")
    # A zero duration is not a usable, known length: ffprobe reports 0 for
    # timed media it could not measure.  Treat it as unknown (``None``) so the
    # primary-stream fallback and DURATION_UNKNOWN semantics still apply.
    if format_duration == 0:
        format_duration = None

    raw_streams = obj.get("streams", [])
    if raw_streams is None:
        raw_streams = []
    if not isinstance(raw_streams, list):
        raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad streams list")

    streams = []
    seen_index = set()
    for raw in raw_streams:
        if not isinstance(raw, dict):
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad stream object")
        index = _parse_number(raw.get("index"), integer=True)
        if index is None or index < 0 or index in seen_index:
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad stream index")
        seen_index.add(index)
        codec_type = raw.get("codec_type")
        if not isinstance(codec_type, str) or not codec_type:
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad codec_type")
        if codec_type not in ("video", "audio"):
            continue

        codec_name = raw.get("codec_name")
        if codec_name is not None and not isinstance(codec_name, str):
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "bad codec_name")
        stream_duration = _parse_number(raw.get("duration"))
        if stream_duration is not None and stream_duration < 0:
            raise MediaError(MediaError.PROBE_OUTPUT_INVALID, "negative duration")
        if stream_duration == 0:
            stream_duration = None
        streams.append(
            {
                "index": index,
                "codec_type": codec_type,
                "codec_name": _clean_token(codec_name),
                "width": _parse_number(raw.get("width"), integer=True, positive=True),
                "height": _parse_number(raw.get("height"), integer=True, positive=True),
                "sample_rate": _parse_number(
                    raw.get("sample_rate"), integer=True, positive=True
                ),
                "channels": _parse_number(
                    raw.get("channels"), integer=True, positive=True
                ),
                "duration": stream_duration,
                "attached_pic": _parse_flag(raw),
            }
        )

    streams.sort(key=lambda item: item["index"])
    return {
        "format_name": format_name,
        "format_duration": format_duration,
        "streams": streams,
    }


def _probe_media(tool_path, source, timeout):
    argv = [
        tool_path,
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        FORMAT_WHITELIST,
        "-probesize",
        str(PROBESIZE),
        "-analyzeduration",
        str(ANALYZEDURATION),
        "-show_entries",
        _SHOW_ENTRIES,
        "-of",
        "json",
        "-i",
        os.path.abspath(source),
    ]
    result = _run_bounded(argv, timeout)
    status = result["status"]
    if status == "timeout":
        return {"status": "failed", "code": MediaError.PROBE_TIMEOUT}
    if status == "output_limit":
        return {"status": "failed", "code": MediaError.PROBE_OUTPUT_LIMIT}
    if status == "launch_error":
        return {"status": "failed", "code": MediaError.PROBE_LAUNCH}
    if status != "ok" or result["returncode"] != 0:
        return {"status": "failed", "code": MediaError.PROBE_FAILED}

    try:
        parsed = _loads_json(result["stdout"], MediaError.PROBE_OUTPUT_INVALID)
    except DeliveryError:
        return {"status": "failed", "code": MediaError.PROBE_OUTPUT_INVALID}
    try:
        return {"status": "ok", "data": _normalise_probe(parsed)}
    except MediaError as exc:
        return {"status": "failed", "code": exc.code}


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def _check(name, status, code=None, reason=None, **extra):
    item = {"name": name, "status": status}
    if code is not None:
        item["code"] = code
    if reason is not None:
        item["reason"] = reason
    item.update(extra)
    return item


def _status_from_checks(checks):
    if any(item["status"] == "failed" for item in checks):
        return "failed"
    if any(item["status"] == "not_run" for item in checks):
        return "not_run"
    return "passed"


def _primary_video(streams):
    for stream in streams:
        if stream["codec_type"] == "video" and not stream["attached_pic"]:
            return stream
    return None


def _primary_audio(streams):
    for stream in streams:
        if stream["codec_type"] == "audio":
            return stream
    return None


def _expected_checks(expected, kind, video_stream, audio_stream, duration):
    if not expected:
        return []
    checks = []
    for key in (
        "width",
        "height",
        "sample_rate",
        "channels",
        "min_duration_seconds",
        "max_duration_seconds",
    ):
        if key not in expected:
            continue
        value = expected[key]
        name = "expected:" + key
        if key == "width":
            actual = None if video_stream is None else video_stream["width"]
        elif key == "height":
            actual = None if video_stream is None else video_stream["height"]
        elif key == "sample_rate":
            actual = None if audio_stream is None else audio_stream["sample_rate"]
        elif key == "channels":
            actual = None if audio_stream is None else audio_stream["channels"]
        else:
            actual = duration

        if actual is None:
            checks.append(
                _check(
                    name,
                    "not_run",
                    "EXPECTED_UNKNOWN",
                    "actual value is unavailable",
                    expected=value,
                )
            )
            continue
        if key == "min_duration_seconds":
            matched = actual >= value
        elif key == "max_duration_seconds":
            matched = actual <= value
        else:
            matched = actual == value
        checks.append(
            _check(
                name,
                "passed" if matched else "failed",
                None if matched else "EXPECTED_MISMATCH",
                expected=value,
                actual=actual,
            )
        )
    return checks


def _safe_identity(path):
    try:
        return _identity(os.stat(path))
    except OSError:
        return None


def _inspect_asset(asset, cache, tool_path, timeout, *, tool_error=None):
    checks = []
    source = asset["source"]

    file_result = (
        cache.get(source) if source is not None else _failure(MediaError.FILE_MISSING)
    )
    checks.append(
        _check(
            "file",
            "passed" if file_result["status"] == "passed" else "failed",
            None if file_result["status"] == "passed" else file_result["code"],
        )
    )

    companions = []
    companion_failed = False
    for companion in asset["companions"]:
        if companion["source"] is None:
            result = _failure(MediaError.FILE_MISSING)
        else:
            result = cache.get(companion["source"])
        if result["status"] != "passed":
            companion_failed = True
        report = {
            "path": companion["path"],
            "role": companion["role"],
            "bytes": result["bytes"],
            "sha256": result["sha256"],
            "status": result["status"],
        }
        if result["status"] != "passed":
            report["code"] = result["code"]
        companions.append(report)
    if asset["companions"]:
        checks.append(
            _check(
                "companions",
                "failed" if companion_failed else "passed",
                "COMPANION_FAILED" if companion_failed else None,
            )
        )

    streams = []
    format_name = None
    duration = None
    video_stream = None
    audio_stream = None

    if source is None:
        checks.append(
            _check("probe", "not_run", "SOURCE_UNAVAILABLE", "media file is missing")
        )
        checks.append(_check("streams", "not_run", "SOURCE_UNAVAILABLE"))
        checks.append(_check("duration", "not_run", "DURATION_UNKNOWN"))
        checks.extend(_expected_checks(asset["expected"], asset["kind"], None, None, None))
    elif file_result["status"] != "passed":
        checks.append(
            _check("probe", "not_run", "SOURCE_UNAVAILABLE", "media file is unusable")
        )
        checks.append(_check("streams", "not_run", "SOURCE_UNAVAILABLE"))
        checks.append(_check("duration", "not_run", "DURATION_UNKNOWN"))
        checks.extend(_expected_checks(asset["expected"], asset["kind"], None, None, None))
    elif tool_path is None:
        tool_code, tool_reason = (
            tool_error
            if tool_error is not None
            else ("TOOL_MISSING", "ffprobe is not available")
        )
        checks.append(_check("probe", "not_run", tool_code, tool_reason))
        checks.append(_check("streams", "not_run", tool_code))
        checks.append(_check("duration", "not_run", "DURATION_UNKNOWN"))
        checks.extend(_expected_checks(asset["expected"], asset["kind"], None, None, None))
    else:
        before = _safe_identity(source)
        if before is None:
            checks.append(_check("source_unchanged", "failed", MediaError.SOURCE_CHANGED))
            checks.append(
                _check("probe", "not_run", MediaError.SOURCE_CHANGED, "source changed")
            )
            checks.append(_check("streams", "not_run", MediaError.SOURCE_CHANGED))
            checks.append(_check("duration", "not_run", "DURATION_UNKNOWN"))
            checks.extend(
                _expected_checks(asset["expected"], asset["kind"], None, None, None)
            )
        else:
            probe = _probe_media(tool_path, source, timeout)
            after = _safe_identity(source)
            unchanged = (
                after is not None
                and before == after
                and file_result.get("identity") == before
            )
            checks.append(
                _check(
                    "source_unchanged",
                    "passed" if unchanged else "failed",
                    None if unchanged else MediaError.SOURCE_CHANGED,
                )
            )
            if probe["status"] == "ok":
                parsed = probe["data"]
                checks.append(_check("probe", "passed"))
                all_streams = parsed["streams"]
                if len(all_streams) > MAX_STREAMS:
                    checks.append(
                        _check(
                            "stream_limit",
                            "not_run",
                            "STREAM_LIMIT",
                            "only the first 64 audio/video streams are reported",
                        )
                    )
                streams = all_streams[:MAX_STREAMS]
                format_name = parsed["format_name"]
                video_stream = _primary_video(all_streams)
                audio_stream = _primary_audio(all_streams)

                if asset["kind"] == "video":
                    if video_stream is None:
                        checks.append(
                            _check(
                                "streams",
                                "failed",
                                "NO_VIDEO_STREAM",
                                "no non-cover video stream",
                            )
                        )
                    else:
                        checks.append(_check("streams", "passed"))
                    if asset["require_audio"]:
                        checks.append(
                            _check(
                                "audio_required",
                                "passed" if audio_stream is not None else "failed",
                                None
                                if audio_stream is not None
                                else "AUDIO_REQUIRED_MISSING",
                            )
                        )
                else:
                    if audio_stream is None:
                        checks.append(
                            _check("streams", "failed", "NO_AUDIO_STREAM")
                        )
                    else:
                        checks.append(_check("streams", "passed"))

                primary = video_stream if asset["kind"] == "video" else audio_stream
                duration = parsed["format_duration"]
                if duration is None and primary is not None:
                    duration = primary["duration"]
                checks.append(
                    _check(
                        "duration",
                        "passed" if duration is not None else "not_run",
                        None if duration is not None else "DURATION_UNKNOWN",
                    )
                )
                checks.extend(
                    _expected_checks(
                        asset["expected"],
                        asset["kind"],
                        video_stream,
                        audio_stream,
                        duration,
                    )
                )
            else:
                checks.append(_check("probe", "failed", probe["code"]))
                checks.append(_check("streams", "not_run", probe["code"]))
                checks.append(_check("duration", "not_run", "DURATION_UNKNOWN"))
                checks.extend(
                    _expected_checks(asset["expected"], asset["kind"], None, None, None)
                )

    return {
        "path": asset["path"],
        "kind": asset["kind"],
        "bytes": file_result["bytes"],
        "sha256": file_result["sha256"],
        "format_name": format_name,
        "duration_seconds": duration,
        "streams": streams,
        "checks": checks,
        "companions": companions,
        "technical_status": _status_from_checks(checks),
    }


def _manual_checks():
    return [
        {"name": name, "status": "not_run", "reason": reason}
        for name, reason in _MANUAL_CHECK_NAMES
    ]


def _inspect_prepared(prepared, *, ffprobe=None, timeout=15):
    timeout = _coerce_timeout(timeout)
    resolved = _resolve_tool(ffprobe, prepared["root"])
    tool_error = None
    if resolved is _AUTO_TOOL_UNTRUSTED:
        tool_path = None
        tool_error = (TOOL_UNTRUSTED, _TOOL_UNTRUSTED_REASON)
    else:
        tool_path = resolved
    tool = {"name": "ffprobe", "available": False, "version": None}
    if tool_path is not None:
        version = _run_bounded([tool_path, "-version"], timeout)
        if version["status"] == "launch_error":
            tool_path = None
        else:
            tool["available"] = True
            if version["status"] == "ok":
                tool["version"] = _tool_version(version["stdout"])

    cache = _FileCache(prepared["limits"])
    assets = [
        _inspect_asset(asset, cache, tool_path, timeout, tool_error=tool_error)
        for asset in prepared["assets"]
    ]
    statuses = [asset["technical_status"] for asset in assets]
    if "failed" in statuses:
        technical_status = "failed"
    elif "not_run" in statuses:
        technical_status = "not_run"
    else:
        technical_status = "passed"

    return {
        "schema_version": REPORT_SCHEMA,
        "independent_acceptance": "UNKNOWN",
        "profile": prepared["profile"],
        "technical_status": technical_status,
        "tool": tool,
        "assets": assets,
        "manual_checks": _manual_checks(),
    }


def inspect_media(root, *, profile="standard", ffprobe=None, timeout=15):
    """Inspect the explicit media declaration in ``root`` and return a report.

    The function only reads ``DELIVERY.json``, declared media files, and their
    companions.  It never scans the project tree, never edits metadata, and
    never writes a report; the CLI wrapper owns report output.
    """
    prepared = _prepare(root, profile=profile)
    return _inspect_prepared(prepared, ffprobe=ffprobe, timeout=timeout)


# ---------------------------------------------------------------------------
# CLI output handling
# ---------------------------------------------------------------------------


def _validate_output_target(out):
    out = _call(_coerce_path, out, MediaError.OUTPUT_INVALID)
    if os.path.lexists(out):
        raise MediaError(MediaError.OUTPUT_EXISTS, "output already exists")
    parent = os.path.dirname(os.path.abspath(out))
    if not parent:
        parent = os.curdir
    try:
        st = os.lstat(parent)
    except OSError:
        raise MediaError(
            MediaError.OUTPUT_PARENT_MISSING, "output parent does not exist"
        ) from None
    if _is_link_like(parent, st):
        raise MediaError(MediaError.OUTPUT_UNSAFE, "output parent is a link")
    if not stat.S_ISDIR(st.st_mode):
        raise MediaError(
            MediaError.OUTPUT_PARENT_MISSING, "output parent is not a directory"
        )
    if not os.access(parent, os.W_OK):
        raise MediaError(MediaError.OUTPUT_UNSAFE, "output parent is not writable")
    return out


def _declared_input_paths(prepared):
    paths = [os.path.join(prepared["root"], "DELIVERY.json")]
    for asset in prepared["assets"]:
        paths.append(os.path.join(prepared["root"], *asset["path"].split("/")))
        for companion in asset["companions"]:
            paths.append(
                os.path.join(prepared["root"], *companion["path"].split("/"))
            )
    return {os.path.normcase(os.path.abspath(path)) for path in paths}


def _reject_output_collision(out, prepared):
    target = os.path.normcase(os.path.abspath(out))
    if target in _declared_input_paths(prepared):
        raise MediaError(
            MediaError.OUTPUT_COLLISION,
            "output may not replace an inspected input file",
        )


def _discard_new_output(out, identity):
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


def _write_report(out, report):
    payload = json.dumps(
        report, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(out, flags, 0o666)
    except FileExistsError:
        raise MediaError(MediaError.OUTPUT_EXISTS, "output already exists") from None
    except OSError:
        raise MediaError(MediaError.IO_ERROR, "could not create report") from None

    identity = None
    try:
        st = os.fstat(descriptor)
        identity = (st.st_dev, st.st_ino)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _discard_new_output(out, identity)
        raise MediaError(MediaError.IO_ERROR, "could not write report") from None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise MediaError(MediaError.INVALID_ARGUMENT, "invalid command line")


def main(argv=None) -> int:
    parser = _ArgumentParser(
        prog="media.py",
        description="Inspect an explicit media declaration (stdlib only).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="write an inspection report")
    inspect_parser.add_argument("--root", required=True, help="staging directory")
    inspect_parser.add_argument(
        "--out", required=True, help="new report path (must not exist)"
    )
    inspect_parser.add_argument(
        "--profile", default="standard", help="standard or large"
    )
    inspect_parser.add_argument(
        "--ffprobe", default=None, help="absolute path to a trusted local ffprobe"
    )
    inspect_parser.add_argument("--timeout", default="15", help="1..60 seconds")

    try:
        args = parser.parse_args(argv)
        timeout = _coerce_timeout(args.timeout)
        out = _validate_output_target(args.out)
        prepared = _prepare(args.root, profile=args.profile)
        _reject_output_collision(out, prepared)
        report = _inspect_prepared(
            prepared, ffprobe=args.ffprobe, timeout=timeout
        )
        _write_report(out, report)
    except MediaError as exc:
        print(
            json.dumps({"error": exc.code}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1
    except OSError:
        print(
            json.dumps({"error": MediaError.IO_ERROR}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "technical_status": report["technical_status"],
                "assets": len(report["assets"]),
            },
            separators=(",", ":"),
        )
    )
    return {"passed": 0, "failed": 1, "not_run": 2}[report["technical_status"]]


if __name__ == "__main__":
    sys.exit(main())
