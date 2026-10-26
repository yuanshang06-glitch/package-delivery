"""Focused tests for the optional media-delivery extension.

Run directly with either of these forms::

    python tests/test_media.py --tool src/media.py
    python tests/test_media.py --tool package-delivery/scripts/media.py

The module also defaults to ``src/media.py`` relative to this file, so it can
be imported by a test runner that does not understand ``--tool``.  Probe JSON
is mocked; the bounded subprocess runner is exercised with real small Python
child processes and does not require ffmpeg to be installed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


def _extract_tool_arg():
    argv = sys.argv[:]
    tool = None
    cleaned = [argv[0]]
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--tool":
            if index + 1 >= len(argv):
                raise SystemExit("--tool requires a path")
            tool = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--tool="):
            tool = arg.split("=", 1)[1]
            index += 1
            continue
        cleaned.append(arg)
        index += 1
    sys.argv[:] = cleaned
    return tool


TOOL_PATH = Path(
    _extract_tool_arg()
    or (Path(__file__).resolve().parents[1] / "src" / "media.py")
).resolve()


def _load_media(path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("media_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MEDIA = _load_media(TOOL_PATH)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_json(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


def _base_meta(media=None, files=None):
    meta = {
        "schema_version": "collaborator-delivery/1",
        "task_id": "media-test",
        "attempt_id": "media-delivery-013-a3",
        "title": "media test",
        "summary": "focused media test package",
        "status": "completed",
        "independent_acceptance": "UNKNOWN",
        "files": files or ["START-HERE.md"],
        "checks": [],
        "unfinished": [],
        "requirements": [],
        "next_steps": [],
    }
    if media is not None:
        meta["media"] = media
    return meta


def _asset(path, kind, **kwargs):
    item = {"path": path, "kind": kind}
    item.update(kwargs)
    return item


def _media(assets):
    return {"schema_version": "collaborator-media/1", "assets": assets}


def _make_package(tmp, media, contents=None, declared_extra=None):
    root = Path(tmp) / "staging"
    root.mkdir(parents=True)
    files = ["START-HERE.md"]
    (root / "START-HERE.md").write_bytes(b"start here")
    contents = contents or {}
    for relative, data in contents.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(str(data), encoding="utf-8")
        if relative not in files:
            files.append(relative)
    for relative in declared_extra or []:
        if relative not in files:
            files.append(relative)
    _write_json(root / "DELIVERY.json", _base_meta(media, files))
    return root


def _video_stream(
    index=0, width=320, height=180, duration="3.0", codec="h264", attached=False
):
    return {
        "index": index,
        "codec_type": "video",
        "codec_name": codec,
        "width": width,
        "height": height,
        "duration": duration,
        "disposition": {"attached_pic": 1 if attached else 0},
    }


def _audio_stream(index=1, sample_rate="44100", channels=2, duration="3.0", codec="aac"):
    return {
        "index": index,
        "codec_type": "audio",
        "codec_name": codec,
        "sample_rate": sample_rate,
        "channels": channels,
        "duration": duration,
        "disposition": {"attached_pic": 0},
    }


def _probe_result(streams, format_name="mov,mp4", duration="3.0", extra=None):
    obj = {
        "format": {"format_name": format_name, "duration": duration},
        "streams": streams,
    }
    if extra:
        obj.update(extra)
    return {
        "status": "ok",
        "returncode": 0,
        "stdout": json.dumps(obj).encode("utf-8"),
        "stderr": b"",
    }


def _failure_result(status, returncode=None):
    return {
        "status": status,
        "returncode": returncode,
        "stdout": b"",
        "stderr": b"",
    }


class MediaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)

    # -- small path/probe helpers ------------------------------------------

    def key(self, path):
        return os.path.normcase(os.path.abspath(str(path)))

    def fake_tool(self):
        tool = self.tmp / "fake-ffprobe"
        tool.write_bytes(b"not a real probe")
        return str(tool)

    @contextmanager
    def fake_probe(self, mapping):
        original = MEDIA._run_bounded

        def fake(argv, timeout, **kwargs):
            if "-version" in argv:
                return {
                    "status": "ok",
                    "returncode": 0,
                    "stdout": b"ffprobe version 9.9-test\n",
                    "stderr": b"",
                }
            try:
                source = argv[argv.index("-i") + 1]
            except (ValueError, IndexError):
                return _failure_result("nonzero", 1)
            handler = mapping.get(self.key(source), mapping.get("default"))
            if handler is None:
                return _failure_result("nonzero", 1)
            return handler(argv) if callable(handler) else handler

        MEDIA._run_bounded = fake
        try:
            yield
        finally:
            MEDIA._run_bounded = original

    def expect_error(self, root, code, *, ffprobe=None):
        with self.assertRaises(MEDIA.MediaError) as context:
            MEDIA.inspect_media(
                str(root),
                ffprobe=ffprobe or str(self.tmp / "missing-ffprobe"),
            )
        self.assertEqual(context.exception.code, code)

    # -- declaration and reference validation ------------------------------

    def test_missing_media_declaration(self):
        root = _make_package(self.tmp, None)
        self.expect_error(root, MEDIA.MediaError.MEDIA_MISSING)

    def test_invalid_declarations(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        good_asset = _asset("artifacts/final.mp4", "video")
        cases = [
            ("media_not_object", "collaborator-media/1"),
            ("unknown_media_field", {"schema_version": "collaborator-media/1", "assets": [good_asset], "extra": 1}),
            ("bad_schema", {"schema_version": "other/1", "assets": [good_asset]}),
            ("empty_assets", {"schema_version": "collaborator-media/1", "assets": []}),
            (
                "too_many_assets",
                {
                    "schema_version": "collaborator-media/1",
                    "assets": [good_asset for _ in range(33)],
                },
            ),
            ("asset_not_object", _media([1])),
            ("unknown_asset_field", _media([_asset("artifacts/final.mp4", "video", extra=1)])),
            ("asset_not_in_files", _media([_asset("artifacts/other.mp4", "video")])),
            (
                "duplicate_asset",
                _media([good_asset, good_asset]),
            ),
            ("bad_kind", _media([_asset("artifacts/final.mp4", "image")])),
            (
                "audio_require_audio",
                _media([_asset("artifacts/final.mp4", "audio", require_audio=True)]),
            ),
            (
                "require_audio_not_bool",
                _media([_asset("artifacts/final.mp4", "video", require_audio=1)]),
            ),
            ("url_path", _media([_asset("https://example.invalid/a.mp4", "video")])),
            ("traversal_path", _media([_asset("../outside.mp4", "video")])),
            ("secret_path", _media([_asset(".env", "video")])),
            (
                "expected_not_object",
                _media([_asset("artifacts/final.mp4", "video", expected=[])]),
            ),
            (
                "unknown_expected",
                _media([_asset("artifacts/final.mp4", "video", expected={"fps": 30})]),
            ),
            (
                "zero_width",
                _media([_asset("artifacts/final.mp4", "video", expected={"width": 0})]),
            ),
            (
                "bool_width",
                _media([_asset("artifacts/final.mp4", "video", expected={"width": True})]),
            ),
            (
                "audio_width",
                _media([_asset("artifacts/final.mp4", "audio", expected={"width": 320})]),
            ),
            (
                "min_over_max",
                _media(
                    [
                        _asset(
                            "artifacts/final.mp4",
                            "video",
                            expected={
                                "min_duration_seconds": 5,
                                "max_duration_seconds": 4,
                            },
                        )
                    ]
                ),
            ),
            (
                "bad_companion_role",
                _media(
                    [
                        _asset(
                            "artifacts/final.mp4",
                            "video",
                            companions=[{"path": "artifacts/final.srt", "role": "notes"}],
                        )
                    ]
                ),
            ),
            (
                "unknown_companion_field",
                _media(
                    [
                        _asset(
                            "artifacts/final.mp4",
                            "video",
                            companions=[
                                {"path": "artifacts/final.srt", "role": "subtitle", "x": 1}
                            ],
                        )
                    ]
                ),
            ),
            (
                "companion_not_in_files",
                _media(
                    [
                        _asset(
                            "artifacts/final.mp4",
                            "video",
                            companions=[
                                {"path": "artifacts/other.srt", "role": "subtitle"}
                            ],
                        )
                    ]
                ),
            ),
            (
                "duplicate_companion",
                _media(
                    [
                        _asset(
                            "artifacts/final.mp4",
                            "video",
                            companions=[
                                {"path": "artifacts/final.srt", "role": "subtitle"},
                                {"path": "artifacts/final.srt", "role": "transcript"},
                            ],
                        )
                    ]
                ),
            ),
        ]
        for name, media in cases:
            with self.subTest(name=name):
                _write_json(
                    root / "DELIVERY.json",
                    _base_meta(
                        media,
                        [
                            "START-HERE.md",
                            "artifacts/final.mp4",
                            "artifacts/final.srt",
                        ],
                    ),
                )
                expected = {
                    "media_not_object": MEDIA.MediaError.MEDIA_INVALID,
                    "unknown_media_field": MEDIA.MediaError.MEDIA_INVALID,
                    "bad_schema": MEDIA.MediaError.MEDIA_INVALID,
                    "empty_assets": MEDIA.MediaError.MEDIA_INVALID,
                    "too_many_assets": MEDIA.MediaError.MEDIA_INVALID,
                    "asset_not_object": MEDIA.MediaError.ASSET_INVALID,
                    "unknown_asset_field": MEDIA.MediaError.ASSET_INVALID,
                    "asset_not_in_files": MEDIA.MediaError.ASSET_INVALID,
                    "duplicate_asset": MEDIA.MediaError.ASSET_DUPLICATE,
                    "bad_kind": MEDIA.MediaError.ASSET_INVALID,
                    "audio_require_audio": MEDIA.MediaError.ASSET_INVALID,
                    "require_audio_not_bool": MEDIA.MediaError.ASSET_INVALID,
                    "url_path": MEDIA.MediaError.PATH_INVALID,
                    "traversal_path": MEDIA.MediaError.PATH_INVALID,
                    "secret_path": MEDIA.MediaError.PATH_SECRET,
                    "expected_not_object": MEDIA.MediaError.EXPECTED_INVALID,
                    "unknown_expected": MEDIA.MediaError.EXPECTED_INVALID,
                    "zero_width": MEDIA.MediaError.EXPECTED_INVALID,
                    "bool_width": MEDIA.MediaError.EXPECTED_INVALID,
                    "audio_width": MEDIA.MediaError.EXPECTED_INVALID,
                    "min_over_max": MEDIA.MediaError.EXPECTED_INVALID,
                    "bad_companion_role": MEDIA.MediaError.COMPANION_INVALID,
                    "unknown_companion_field": MEDIA.MediaError.COMPANION_INVALID,
                    "companion_not_in_files": MEDIA.MediaError.COMPANION_INVALID,
                    "duplicate_companion": MEDIA.MediaError.COMPANION_INVALID,
                }[name]
                self.expect_error(root, expected)

    def test_declared_directory_is_not_a_media_file(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/clip.mp4", "video")]),
            declared_extra=["artifacts/clip.mp4"],
        )
        (root / "artifacts" / "clip.mp4").mkdir(parents=True)
        self.expect_error(root, MEDIA.MediaError.ASSET_INVALID)

    def test_symlinked_media_is_rejected_when_supported(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/link.mp4", "video")]),
            declared_extra=["artifacts/link.mp4"],
        )
        outside = self.tmp / "outside.bin"
        outside.write_bytes(b"outside")
        (root / "artifacts").mkdir()
        try:
            os.symlink(outside, root / "artifacts" / "link.mp4")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available in this environment")
        self.expect_error(root, MEDIA.MediaError.PATH_UNSAFE)

    # -- successful technical inspection -----------------------------------

    def test_video_and_audio_success(self):
        contents = {
            "artifacts/final.mp4": b"fake-video-bytes",
            "artifacts/final.srt": b"1\n00:00:00,000 --> 00:00:03,000\nhi\n",
            "artifacts/voice.wav": b"fake-audio-bytes",
        }
        media = _media(
            [
                _asset(
                    "artifacts/final.mp4",
                    "video",
                    require_audio=True,
                    expected={
                        "width": 320,
                        "height": 180,
                        "min_duration_seconds": 2.5,
                        "max_duration_seconds": 3.5,
                    },
                    companions=[{"path": "artifacts/final.srt", "role": "subtitle"}],
                ),
                _asset(
                    "artifacts/voice.wav",
                    "audio",
                    expected={"sample_rate": 16000, "channels": 1},
                ),
            ]
        )
        root = _make_package(self.tmp, media, contents=contents)
        video_probe = _probe_result(
            [_video_stream(0), _audio_stream(1)],
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration="3.0",
        )
        audio_probe = _probe_result(
            [_audio_stream(0, sample_rate="16000", channels=1, duration="2.0")],
            format_name="wav",
            duration="2.0",
        )
        with self.fake_probe(
            {
                self.key(root / "artifacts" / "final.mp4"): video_probe,
                self.key(root / "artifacts" / "voice.wav"): audio_probe,
            }
        ):
            report = MEDIA.inspect_media(
                str(root), ffprobe=self.fake_tool(), timeout=5
            )

        self.assertEqual(report["schema_version"], "media-inspection/1")
        self.assertEqual(report["independent_acceptance"], "UNKNOWN")
        self.assertEqual(report["profile"], "standard")
        self.assertEqual(report["technical_status"], "passed")
        self.assertEqual(report["tool"]["name"], "ffprobe")
        self.assertTrue(report["tool"]["available"])
        self.assertEqual(report["tool"]["version"], "ffprobe version 9.9-test")
        self.assertNotIn("path", report["tool"])
        self.assertEqual(len(report["assets"]), 2)
        self.assertTrue(all(item["status"] == "not_run" for item in report["manual_checks"]))

        video = report["assets"][0]
        self.assertEqual(video["path"], "artifacts/final.mp4")
        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["bytes"], len(contents["artifacts/final.mp4"]))
        self.assertEqual(
            video["sha256"],
            hashlib.sha256(contents["artifacts/final.mp4"]).hexdigest(),
        )
        self.assertEqual(video["duration_seconds"], 3.0)
        self.assertEqual(video["format_name"], "mov,mp4,m4a,3gp,3g2,mj2")
        self.assertEqual([item["index"] for item in video["streams"]], [0, 1])
        self.assertEqual(video["companions"][0]["status"], "passed")
        self.assertEqual(video["companions"][0]["role"], "subtitle")
        self.assertEqual(video["technical_status"], "passed")

        audio = report["assets"][1]
        self.assertEqual(audio["duration_seconds"], 2.0)
        self.assertEqual(audio["streams"][0]["sample_rate"], 16000)
        self.assertEqual(audio["streams"][0]["channels"], 1)
        self.assertEqual(audio["technical_status"], "passed")

        # Technical fields only: no filenames, tags, artist, or absolute paths.
        serialized = json.dumps(report)
        self.assertNotIn(str(self.tmp), serialized)
        self.assertNotIn("filename", serialized)
        self.assertNotIn("tags", serialized)
        self.assertNotIn("artist", serialized)
        for stream in video["streams"]:
            self.assertEqual(
                set(stream),
                {
                    "index",
                    "codec_type",
                    "codec_name",
                    "width",
                    "height",
                    "sample_rate",
                    "channels",
                    "duration",
                    "attached_pic",
                },
            )

    def test_video_require_audio_missing_fails(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video", require_audio=True)]),
            contents={"artifacts/final.mp4": b"video"},
        )
        probe = _probe_result([_video_stream(0)])
        with self.fake_probe({self.key(root / "artifacts" / "final.mp4"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        self.assertEqual(report["technical_status"], "failed")
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["audio_required"]["status"], "failed")
        self.assertEqual(checks["audio_required"]["code"], "AUDIO_REQUIRED_MISSING")

    def test_audio_with_cover_is_not_a_video_delivery(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/cover.mp3", "video")]),
            contents={"artifacts/cover.mp3": b"audio with cover"},
        )
        probe = _probe_result(
            [_audio_stream(0), _video_stream(1, width=32, height=32, attached=True)],
            format_name="mp3",
        )
        with self.fake_probe({self.key(root / "artifacts" / "cover.mp3"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        self.assertEqual(report["technical_status"], "failed")
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["streams"]["status"], "failed")
        self.assertEqual(checks["streams"]["code"], "NO_VIDEO_STREAM")

    def test_expected_mismatch_and_unknown(self):
        media = _media(
            [
                _asset(
                    "artifacts/final.mp4",
                    "video",
                    expected={"width": 640, "sample_rate": 48000},
                )
            ]
        )
        root = _make_package(
            self.tmp, media, contents={"artifacts/final.mp4": b"video"}
        )
        stream = _video_stream(0, width=320)
        probe = _probe_result([stream], duration="3.0")
        with self.fake_probe({self.key(root / "artifacts" / "final.mp4"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["expected:width"]["status"], "failed")
        self.assertEqual(checks["expected:width"]["code"], "EXPECTED_MISMATCH")
        self.assertEqual(checks["expected:sample_rate"]["status"], "not_run")
        self.assertEqual(checks["expected:sample_rate"]["code"], "EXPECTED_UNKNOWN")
        self.assertEqual(report["technical_status"], "failed")

    def test_unknown_expected_actual_is_not_run(self):
        media = _media(
            [_asset("artifacts/final.mp4", "video", expected={"width": 320})]
        )
        root = _make_package(
            self.tmp, media, contents={"artifacts/final.mp4": b"video"}
        )
        probe = _probe_result([_video_stream(0, width=None)], duration="3.0")
        with self.fake_probe({self.key(root / "artifacts" / "final.mp4"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["expected:width"]["status"], "not_run")
        self.assertEqual(checks["duration"]["status"], "passed")
        self.assertEqual(report["technical_status"], "not_run")

    def test_missing_duration_is_not_run(self):
        media = _media([_asset("artifacts/voice.wav", "audio")])
        root = _make_package(
            self.tmp, media, contents={"artifacts/voice.wav": b"audio"}
        )
        probe = _probe_result(
            [_audio_stream(0, duration=None)], format_name="wav", duration=None
        )
        with self.fake_probe({self.key(root / "artifacts" / "voice.wav"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["duration"]["status"], "not_run")
        self.assertEqual(checks["duration"]["code"], "DURATION_UNKNOWN")
        self.assertIsNone(report["assets"][0]["duration_seconds"])
        self.assertEqual(report["technical_status"], "not_run")

    def test_zero_format_and_zero_stream_duration_is_not_passed(self):
        media = _media([_asset("artifacts/final.mp4", "video")])
        root = _make_package(
            self.tmp, media, contents={"artifacts/final.mp4": b"video"}
        )
        probe = _probe_result([_video_stream(0, duration="0.0")], duration="0.0")
        with self.fake_probe({self.key(root / "artifacts" / "final.mp4"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["duration"]["status"], "not_run")
        self.assertEqual(checks["duration"]["code"], "DURATION_UNKNOWN")
        self.assertIsNone(report["assets"][0]["duration_seconds"])
        self.assertIsNone(report["assets"][0]["streams"][0]["duration"])
        self.assertNotEqual(report["technical_status"], "passed")
        self.assertEqual(report["technical_status"], "not_run")

    def test_zero_format_duration_falls_back_to_stream_duration(self):
        media = _media(
            [
                _asset(
                    "artifacts/final.mp4",
                    "video",
                    expected={"min_duration_seconds": 2.5},
                )
            ]
        )
        root = _make_package(
            self.tmp, media, contents={"artifacts/final.mp4": b"video"}
        )
        probe = _probe_result([_video_stream(0, duration="3.0")], duration="0")
        with self.fake_probe({self.key(root / "artifacts" / "final.mp4"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["duration"]["status"], "passed")
        self.assertEqual(checks["expected:min_duration_seconds"]["status"], "passed")
        self.assertEqual(report["assets"][0]["duration_seconds"], 3.0)
        self.assertEqual(report["technical_status"], "passed")

    def test_valid_format_duration_wins_over_zero_stream_duration(self):
        media = _media([_asset("artifacts/final.mp4", "video")])
        root = _make_package(
            self.tmp, media, contents={"artifacts/final.mp4": b"video"}
        )
        probe = _probe_result([_video_stream(0, duration="0")], duration="3.0")
        with self.fake_probe({self.key(root / "artifacts" / "final.mp4"): probe}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["duration"]["status"], "passed")
        self.assertIsNone(report["assets"][0]["streams"][0]["duration"])
        self.assertEqual(report["assets"][0]["duration_seconds"], 3.0)
        self.assertEqual(report["technical_status"], "passed")

    # -- missing tool / probe failures -------------------------------------

    def test_missing_tool_is_not_run(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        report = MEDIA.inspect_media(
            str(root), ffprobe=str(self.tmp / "missing-ffprobe")
        )
        self.assertEqual(report["technical_status"], "not_run")
        self.assertFalse(report["tool"]["available"])
        self.assertIsNone(report["tool"]["version"])
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["file"]["status"], "passed")
        self.assertEqual(checks["probe"]["status"], "not_run")
        self.assertEqual(checks["probe"]["code"], "TOOL_MISSING")

    def test_auto_discovered_tool_inside_root_is_rejected(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        inside_tool = root / "ffprobe.exe"
        inside_tool.write_bytes(b"package tool")
        candidates = [str(inside_tool)]
        if os.name == "nt":
            candidates.append(str(inside_tool).upper())
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                calls = []

                def forbidden(argv, timeout, **kwargs):
                    calls.append(list(argv))
                    return _failure_result("launch_error")

                with mock.patch.object(
                    MEDIA.shutil, "which", return_value=candidate
                ):
                    with mock.patch.object(MEDIA, "_run_bounded", forbidden):
                        report = MEDIA.inspect_media(str(root))
                self.assertEqual(calls, [])
                self.assertEqual(report["technical_status"], "not_run")
                self.assertFalse(report["tool"]["available"])
                self.assertIsNone(report["tool"]["version"])
                self.assertEqual(
                    set(report["tool"]), {"name", "available", "version"}
                )
                self.assertEqual(
                    report["assets"][0]["technical_status"], "not_run"
                )
                checks = {
                    item["name"]: item for item in report["assets"][0]["checks"]
                }
                self.assertEqual(checks["probe"]["status"], "not_run")
                self.assertEqual(checks["probe"]["code"], "TOOL_UNTRUSTED")
                self.assertEqual(checks["streams"]["code"], "TOOL_UNTRUSTED")
                self.assertEqual(checks["duration"]["code"], "DURATION_UNKNOWN")
                reason = checks["probe"]["reason"]
                self.assertIn("trusted", reason.lower())
                self.assertNotIn(str(root), reason)
                self.assertNotIn("ffprobe.exe", reason)

    def test_auto_discovered_tool_outside_root_is_used(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        tools = self.tmp / "trusted-tools"
        tools.mkdir()
        outside_tool = tools / "ffprobe.exe"
        outside_tool.write_bytes(b"external tool")
        probe = _probe_result([_video_stream(0)], duration="3.0")
        with mock.patch.object(
            MEDIA.shutil, "which", return_value=str(outside_tool)
        ):
            with self.fake_probe(
                {self.key(root / "artifacts" / "final.mp4"): probe}
            ):
                report = MEDIA.inspect_media(str(root))
        self.assertEqual(report["technical_status"], "passed")
        self.assertTrue(report["tool"]["available"])
        self.assertEqual(report["tool"]["version"], "ffprobe version 9.9-test")
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["probe"]["status"], "passed")
        self.assertEqual(checks["probe"].get("code"), None)

    def test_auto_discovered_tool_alias_into_root_is_rejected(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        (root / "ffprobe.exe").write_bytes(b"package tool")
        alias = self.tmp / "alias-dir"
        if not self._directory_alias(alias, root):
            self.skipTest("directory symlink/junction is not available")
        candidate = alias / "ffprobe.exe"
        self.assertTrue(candidate.exists())
        # Literally outside the root, but canonically inside it.
        self.assertFalse(
            self.key(candidate).startswith(self.key(root) + os.sep)
        )
        self.assertTrue(
            os.path.normcase(os.path.realpath(candidate)).startswith(
                os.path.normcase(os.path.realpath(root)) + os.sep
            )
        )
        calls = []

        def forbidden(argv, timeout, **kwargs):
            calls.append(list(argv))
            return _failure_result("launch_error")

        with mock.patch.object(
            MEDIA.shutil, "which", return_value=str(candidate)
        ):
            with mock.patch.object(MEDIA, "_run_bounded", forbidden):
                report = MEDIA.inspect_media(str(root))
        self.assertEqual(calls, [])
        self.assertEqual(report["technical_status"], "not_run")
        self.assertFalse(report["tool"]["available"])
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["probe"]["status"], "not_run")
        self.assertEqual(checks["probe"]["code"], "TOOL_UNTRUSTED")
        self.assertIn("trusted", checks["probe"]["reason"].lower())

    def test_probe_failure_modes(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        source_key = self.key(root / "artifacts" / "final.mp4")
        cases = [
            ("timeout", _failure_result("timeout"), "PROBE_TIMEOUT"),
            ("output_limit", _failure_result("output_limit"), "PROBE_OUTPUT_LIMIT"),
            ("nonzero", _failure_result("nonzero", 2), "PROBE_FAILED"),
            ("launch_error", _failure_result("launch_error"), "PROBE_LAUNCH"),
        ]
        for name, result, code in cases:
            with self.subTest(name=name):
                with self.fake_probe({source_key: result}):
                    report = MEDIA.inspect_media(
                        str(root), ffprobe=self.fake_tool()
                    )
                checks = {item["name"]: item for item in report["assets"][0]["checks"]}
                self.assertEqual(checks["probe"]["status"], "failed")
                self.assertEqual(checks["probe"]["code"], code)
                self.assertEqual(report["technical_status"], "failed")

    def test_invalid_probe_json(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        source_key = self.key(root / "artifacts" / "final.mp4")
        bad_payloads = {
            "not_json": b"not json",
            "duplicate_keys": b'{"streams":[],"streams":[]}',
            "nan": b'{"format":{"duration":NaN},"streams":[]}',
            "not_object": b"[]",
            "bad_number": b'{"format":{"duration":"abc"},"streams":[]}',
        }
        for name, payload in bad_payloads.items():
            with self.subTest(name=name):
                result = {
                    "status": "ok",
                    "returncode": 0,
                    "stdout": payload,
                    "stderr": b"",
                }
                with self.fake_probe({source_key: result}):
                    report = MEDIA.inspect_media(
                        str(root), ffprobe=self.fake_tool()
                    )
                checks = {item["name"]: item for item in report["assets"][0]["checks"]}
                self.assertEqual(checks["probe"]["status"], "failed")
                self.assertEqual(
                    checks["probe"]["code"], "PROBE_OUTPUT_INVALID"
                )
                self.assertEqual(report["technical_status"], "failed")

    def test_source_change_during_probe(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"original"},
        )
        source = root / "artifacts" / "final.mp4"
        probe = _probe_result([_video_stream(0)], duration="3.0")

        def mutate(argv):
            source.write_bytes(source.read_bytes() + b"changed")
            return probe

        with self.fake_probe({self.key(source): mutate}):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["source_unchanged"]["status"], "failed")
        self.assertEqual(checks["source_unchanged"]["code"], "SOURCE_CHANGED")
        self.assertEqual(report["technical_status"], "failed")

    # -- hashing / profile limits ------------------------------------------

    def test_hash_limits_and_shared_companion(self):
        limits = {
            "max_entries": 512,
            "max_file_bytes": 10,
            "max_total_bytes": 25,
            "max_metadata_bytes": 64 * 1024,
            "max_manifest_bytes": 512 * 1024,
        }
        media = _media(
            [
                _asset(
                    "artifacts/a.mp4",
                    "video",
                    companions=[{"path": "artifacts/shared.srt", "role": "subtitle"}],
                ),
                _asset(
                    "artifacts/b.mp4",
                    "video",
                    companions=[{"path": "artifacts/shared.srt", "role": "subtitle"}],
                ),
            ]
        )
        self._remove_staging()
        root = _make_package(
            self.tmp,
            media,
            contents={
                "artifacts/a.mp4": b"a" * 10,
                "artifacts/b.mp4": b"b" * 10,
                "artifacts/shared.srt": b"s" * 5,
            },
        )
        probes = {
            self.key(root / "artifacts" / "a.mp4"): _probe_result(
                [_video_stream(0)], duration="1.0"
            ),
            self.key(root / "artifacts" / "b.mp4"): _probe_result(
                [_video_stream(0)], duration="1.0"
            ),
        }
        with mock.patch.object(MEDIA, "get_limits", return_value=limits):
            with self.fake_probe(probes):
                report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        self.assertEqual(report["technical_status"], "passed")
        self.assertEqual(report["assets"][0]["sha256"], hashlib.sha256(b"a" * 10).hexdigest())
        self.assertEqual(report["assets"][1]["sha256"], hashlib.sha256(b"b" * 10).hexdigest())
        self.assertEqual(
            report["assets"][0]["companions"][0]["sha256"],
            hashlib.sha256(b"s" * 5).hexdigest(),
        )

    def test_per_file_limit_fails_without_reading(self):
        limits = {
            "max_entries": 512,
            "max_file_bytes": 4,
            "max_total_bytes": 100,
            "max_metadata_bytes": 64 * 1024,
            "max_manifest_bytes": 512 * 1024,
        }
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"12345"},
        )
        with mock.patch.object(MEDIA, "get_limits", return_value=limits):
            report = MEDIA.inspect_media(
                str(root), ffprobe=str(self.tmp / "missing-ffprobe")
            )
        checks = {item["name"]: item for item in report["assets"][0]["checks"]}
        self.assertEqual(checks["file"]["status"], "failed")
        self.assertEqual(checks["file"]["code"], "FILE_LIMIT")
        self.assertIsNone(report["assets"][0]["sha256"])
        self.assertEqual(report["technical_status"], "failed")

    def test_total_limit_fails_after_budget(self):
        limits = {
            "max_entries": 512,
            "max_file_bytes": 10,
            "max_total_bytes": 10,
            "max_metadata_bytes": 64 * 1024,
            "max_manifest_bytes": 512 * 1024,
        }
        media = _media(
            [
                _asset("artifacts/a.mp4", "video"),
                _asset("artifacts/b.mp4", "video"),
            ]
        )
        root = _make_package(
            self.tmp,
            media,
            contents={
                "artifacts/a.mp4": b"a" * 10,
                "artifacts/b.mp4": b"b" * 10,
            },
        )
        probes = {
            self.key(root / "artifacts" / "a.mp4"): _probe_result(
                [_video_stream(0)], duration="1.0"
            ),
            self.key(root / "artifacts" / "b.mp4"): _probe_result(
                [_video_stream(0)], duration="1.0"
            ),
        }
        with mock.patch.object(MEDIA, "get_limits", return_value=limits):
            with self.fake_probe(probes):
                report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        self.assertEqual(report["assets"][0]["technical_status"], "passed")
        second_checks = {item["name"]: item for item in report["assets"][1]["checks"]}
        self.assertEqual(second_checks["file"]["status"], "failed")
        self.assertEqual(second_checks["file"]["code"], "TOTAL_LIMIT")
        self.assertEqual(report["technical_status"], "failed")

    # -- companions --------------------------------------------------------

    def test_missing_companion_fails_asset(self):
        media = _media(
            [
                _asset(
                    "artifacts/final.mp4",
                    "video",
                    companions=[{"path": "artifacts/final.srt", "role": "subtitle"}],
                )
            ]
        )
        root = _make_package(
            self.tmp,
            media,
            contents={"artifacts/final.mp4": b"video"},
            declared_extra=["artifacts/final.srt"],
        )
        with self.fake_probe(
            {
                self.key(root / "artifacts" / "final.mp4"): _probe_result(
                    [_video_stream(0)], duration="3.0"
                )
            }
        ):
            report = MEDIA.inspect_media(str(root), ffprobe=self.fake_tool())
        companion = report["assets"][0]["companions"][0]
        self.assertEqual(companion["status"], "failed")
        self.assertEqual(companion["code"], "FILE_MISSING")
        self.assertEqual(report["technical_status"], "failed")

    # -- bounded subprocess runner -----------------------------------------

    def test_bounded_runner_ok_and_nonzero(self):
        ok = MEDIA._run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('hi')"], 10
        )
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["stdout"], b"hi")
        failed = MEDIA._run_bounded(
            [sys.executable, "-c", "raise SystemExit(3)"], 10
        )
        self.assertEqual(failed["status"], "nonzero")
        self.assertEqual(failed["returncode"], 3)

    def test_bounded_runner_timeout(self):
        started = time.monotonic()
        result = MEDIA._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"], 0.5
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "timeout")
        self.assertLess(elapsed, 10)

    def test_bounded_runner_stdout_and_stderr_caps(self):
        stdout_result = MEDIA._run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*200000)"],
            10,
            stdout_cap=1000,
            stderr_cap=1000,
        )
        self.assertEqual(stdout_result["status"], "output_limit")
        stderr_result = MEDIA._run_bounded(
            [sys.executable, "-c", "import sys; sys.stderr.write('e'*200000)"],
            10,
            stdout_cap=1000,
            stderr_cap=1000,
        )
        self.assertEqual(stderr_result["status"], "output_limit")

    # -- CLI ---------------------------------------------------------------

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(TOOL_PATH), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _remove_staging(self):
        staging = self.tmp / "staging"
        if staging.exists():
            shutil.rmtree(staging)

    def _directory_alias(self, link, target):
        """Create a directory symlink/junction; return whether it worked."""
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            pass
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return result.returncode == 0 and link.exists()
        return False

    def test_cli_existing_output_rejected(self):
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        out = self.tmp / "report.json"
        out.write_text("keep me", encoding="utf-8")
        result = self.run_cli(
            "inspect", "--root", str(root), "--out", str(out)
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr), {"error": "OUTPUT_EXISTS"})
        self.assertEqual(out.read_text(encoding="utf-8"), "keep me")

    def test_cli_missing_tool_writes_not_run_report(self):
        self._remove_staging()
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        out = self.tmp / "evidence" / "media-report.json"
        out.parent.mkdir()
        result = self.run_cli(
            "inspect",
            "--root",
            str(root),
            "--out",
            str(out),
            "--ffprobe",
            str(self.tmp / "missing-ffprobe"),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        stdout = json.loads(result.stdout)
        self.assertEqual(set(stdout), {"technical_status", "assets"})
        self.assertEqual(stdout["technical_status"], "not_run")
        self.assertEqual(stdout["assets"], 1)
        self.assertTrue(out.exists())
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["technical_status"], "not_run")
        self.assertEqual(report["independent_acceptance"], "UNKNOWN")
        self.assertTrue(all(item["status"] == "not_run" for item in report["manual_checks"]))

    def test_cli_failed_report_is_written(self):
        self._remove_staging()
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/missing.mp4", "video")]),
            declared_extra=["artifacts/missing.mp4"],
        )
        (root / "artifacts").mkdir()
        out = self.tmp / "evidence" / "failed-report.json"
        out.parent.mkdir()
        result = self.run_cli(
            "inspect",
            "--root",
            str(root),
            "--out",
            str(out),
            "--ffprobe",
            str(self.tmp / "missing-ffprobe"),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["technical_status"], "failed")
        self.assertEqual(report["assets"][0]["checks"][0]["code"], "FILE_MISSING")

    def test_cli_invalid_declaration_does_not_write_report(self):
        self._remove_staging()
        root = _make_package(self.tmp, None)
        out = self.tmp / "evidence" / "invalid-report.json"
        out.parent.mkdir()
        result = self.run_cli(
            "inspect", "--root", str(root), "--out", str(out)
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr), {"error": "MEDIA_MISSING"})
        self.assertFalse(out.exists())

    def test_cli_output_collision_with_missing_media_path(self):
        self._remove_staging()
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/missing.mp4", "video")]),
            declared_extra=["artifacts/missing.mp4"],
        )
        (root / "artifacts").mkdir()
        out = root / "artifacts" / "missing.mp4"
        result = self.run_cli(
            "inspect", "--root", str(root), "--out", str(out)
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr), {"error": "OUTPUT_COLLISION"})
        self.assertFalse(out.exists())

    def test_cli_rejects_relative_ffprobe_path(self):
        self._remove_staging()
        root = _make_package(
            self.tmp,
            _media([_asset("artifacts/final.mp4", "video")]),
            contents={"artifacts/final.mp4": b"video"},
        )
        out = self.tmp / "evidence" / "report.json"
        out.parent.mkdir()
        result = self.run_cli(
            "inspect",
            "--root",
            str(root),
            "--out",
            str(out),
            "--ffprobe",
            "relative-ffprobe",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr), {"error": "INVALID_FFPROBE"})


if __name__ == "__main__":
    unittest.main()
