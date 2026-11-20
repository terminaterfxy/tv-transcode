import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeFlask:
    def __init__(self, _name):
        pass

    @staticmethod
    def _route(*_args, **_kwargs):
        return lambda func: func

    get = _route
    post = _route

    @staticmethod
    def before_request(func):
        return func

    def run(self, **_kwargs):
        pass


fake_flask = types.ModuleType("flask")
fake_flask.Flask = _FakeFlask
fake_flask.jsonify = lambda value: value
fake_flask.request = types.SimpleNamespace()
sys.modules.setdefault("flask", fake_flask)

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("mediapanel_app", APP_PATH)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def probe(video_codec="h264", *, height=1080, dv=0, audio="eac3", subs=None):
    return {
        "video": {
            "codec": video_codec,
            "dv_profile": dv,
            "hdr": False,
            "width": 1920 if height == 1080 else 3840,
            "height": height,
            "pix_fmt": "yuv420p",
            "r_frame_rate": "24/1",
        },
        "video_count": 1,
        "audio": [{"codec": audio, "channels": 6, "lang": "jpn"}],
        "subs": list(subs or []),
        "attachments": 0,
        "duration": 6000,
    }


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.inbox = root / "download"
        self.work = self.inbox / "dvwork"
        self.movies = root / "Movies"
        self.inbox.mkdir()
        self.work.mkdir()
        self.globals = patch.multiple(
            app,
            SHARE=root,
            INBOX=self.inbox,
            WORK=self.work,
            MOVIES=self.movies,
            STATE_FILE=self.work / ".panel_state.json",
            ITEMS={},
            SIZE_HISTORY={},
            PROBE_FAILURES={},
            JOB_QUEUE=[],
            CURRENT_JOB=None,
            PROCESSED={},
            OUTPUT_INFO={},
            DISC_PREVIEW_CACHE_FILE=self.work / ".disc_preview_cache.json",
            DISC_PREVIEW_CACHE={},
            DISC_PREVIEW_QUEUE=[],
            DISC_PREVIEW_PENDING=set(),
            DISC_PREVIEW_ACTIVE=None,
            JELLYFIN_SUBTITLE_INDEX={},
            UPGRADE_STATE={"status": "idle"},
            ACTIVE_OPERATIONS=set(),
            PIPELINE_HALT="",
            # 测试不依赖本机是否装了 Tesseract；需要时由用例自行指定已装模型。
            installed_tesseract_languages=lambda: None,
        )
        self.globals.start()

    def tearDown(self):
        self.globals.stop()
        self.tempdir.cleanup()

    def write_srt(self, name, body):
        path = self.work / name
        path.write_text(body, "utf-8")
        return path

    def test_sparse_chinese_srt_is_kept(self):
        path = self.write_srt(
            "forced.srt",
            "1\n00:00:10,000 --> 00:00:12,000\n危险\n",
        )
        self.assertEqual(app._classify_srt(path, "und"), "zho")
        self.assertEqual(app._inspect_srt(path)["cue_count"], 1)

    def test_sparse_kanji_uses_japanese_track_language(self):
        path = self.write_srt(
            "sign.srt",
            "1\n00:10:10,000 --> 00:10:12,000\n入口\n",
        )
        self.assertEqual(app._classify_srt(path, "jpn"), "jpn")

    def test_timecode_only_srt_is_rejected(self):
        path = self.write_srt(
            "empty.srt",
            "1\n00:00:10,000 --> 00:00:12,000\n\n",
        )
        self.assertIsNone(app._classify_srt(path, "zho"))

    def test_pgsrip_returns_diagnostics_instead_of_swallowing_them(self):
        class FakePopen:
            def __init__(self, *_args, **_kwargs):
                self.stdout = io.BytesIO(
                    b"1 PGS subtitle collected\rRipping subtitles  50%\r"
                    b"tesseract language error\n"
                )
                self.returncode = 2

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def wait(self):
                return self.returncode

        job = {"percent": 0}
        with patch.object(app.subprocess, "Popen", FakePopen):
            code, diagnostic = app._run_pgsrip_progress(
                job, self.work / "track.mkv", 0, 100
            )

        self.assertEqual(code, 2)
        self.assertIn("tesseract language error", diagnostic)
        self.assertEqual(job["percent"], 50)

    def test_mkvmerge_ocr_extraction_failure_is_persistently_explained(self):
        source = self.inbox / "movie.mkv"
        source.write_bytes(b"x")
        job = {"log": []}
        failed = subprocess.CompletedProcess(
            args=["mkvmerge"], returncode=2, stdout=b"", stderr=b"bad track id"
        )
        with patch.object(app, "_pgs_tracks", return_value=[{"id": 7, "lang": "zho"}]), \
                patch.object(app.subprocess, "run", return_value=failed):
            result = app.ocr_subs(job, source, self.work)

        self.assertEqual(result, [])
        self.assertIn("抽取失败", job["warnings"][0])
        self.assertIn("bad track id", job["warnings"][0])

    def test_pgs_is_extracted_to_sup_before_pgsrip(self):
        source = self.inbox / "movie.mkv"
        source.write_bytes(b"x")
        seen = {}

        def extract(command, **_kwargs):
            seen["extract"] = command
            sup = Path(command[-1].split(":", 1)[1])
            sup.write_bytes(b"PG")
            return subprocess.CompletedProcess(command, 0, b"ok", b"")

        def rip(_job, target, _base, _span):
            seen["rip_target"] = target
            target.with_suffix(".srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", "utf-8"
            )
            return 0, "done"

        with patch.object(app, "_pgs_tracks", return_value=[{"id": 7, "lang": "jpn"}]), \
                patch.object(app.subprocess, "run", side_effect=extract), \
                patch.object(app, "_run_pgsrip_progress", side_effect=rip):
            result = app.ocr_subs({"log": []}, source, self.work)

        self.assertEqual(seen["extract"][:3], ["mkvextract", str(source), "tracks"])
        self.assertTrue(str(seen["extract"][-1]).startswith("7:"))
        self.assertEqual(seen["rip_target"].suffix, ".sup")
        self.assertEqual(result[0][1], "jpn")
        self.assertFalse(seen["rip_target"].exists())

    def test_m2ts_pgs_is_losslessly_normalized_before_sup_extraction(self):
        source = self.inbox / "movie.m2ts"
        source.write_bytes(b"transport stream")
        commands = []

        def run(command, **_kwargs):
            commands.append(command)
            if command[0] == "mkvmerge":
                Path(command[2]).write_bytes(b"matroska")
            elif command[0] == "mkvextract":
                Path(command[-1].split(":", 1)[1]).write_bytes(b"PG")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def rip(_job, target, _base, _span):
            target.with_suffix(".srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n日本語\n", "utf-8"
            )
            return 0, ""

        with patch.object(
            app, "_pgs_tracks", return_value=[{"id": 2, "lang": "jpn"}]
        ) as tracks, patch.object(app.subprocess, "run", side_effect=run), \
                patch.object(app, "_run_pgsrip_progress", side_effect=rip):
            result = app.ocr_subs({"log": []}, source, self.work)

        normalized = self.work / "pgs_source.mkv"
        tracks.assert_called_once_with(normalized)
        self.assertEqual(commands[0], [
            "mkvmerge", "-o", str(normalized), str(source)
        ])
        self.assertEqual(commands[1][0:2], ["mkvextract", str(normalized)])
        self.assertEqual(result[0][1], "jpn")

    def test_unprobed_iso_preview_says_it_will_pre_read(self):
        preview = app.format_preview(
            "iso", None, "/share/download/movie.iso", "analyzing"
        )
        self.assertEqual(preview["source"], "ISO 光盘镜像")
        self.assertEqual(preview["target"], "正在预读转换后规格")
        self.assertIn("分辨率、帧率、杜比/HDR", preview["target_detail"])
        self.assertTrue(preview["provisional"])
        self.assertTrue(preview["disc_pending"])

    def test_makemkv_info_previews_longest_main_title_before_rip(self):
        robot = """
TINFO:0,2,0,"映前短片"
TINFO:0,9,0,"0:10:00"
TINFO:0,10,0,"3000000000"
SINFO:0,0,1,6201,"Video"
SINFO:0,0,5,0,"V_MPEG4/ISO/AVC"
SINFO:0,0,6,0,"Mpeg4 AVC High@L4.1"
SINFO:0,0,19,0,"1920x1080"
SINFO:0,0,21,0,"24 (24/1)"
TINFO:1,2,0,"Main Feature"
TINFO:1,9,0,"1:46:00"
TINFO:1,10,0,"33473424661"
SINFO:1,0,1,6201,"Video"
SINFO:1,0,5,0,"V_MPEG4/ISO/AVC"
SINFO:1,0,6,0,"Mpeg4 AVC High@L4.1"
SINFO:1,0,19,0,"1920x1080"
SINFO:1,0,21,0,"24 (24/1)"
SINFO:1,1,1,6202,"Audio"
SINFO:1,1,3,0,"jpn"
SINFO:1,1,6,0,"LPCM"
SINFO:1,1,22,0,"5.1"
SINFO:1,2,1,6203,"Subtitles"
SINFO:1,2,3,0,"jpn"
SINFO:1,2,6,0,"PGS"
"""
        media = app.parse_makemkv_info(robot)
        self.assertEqual(media["disc_preview"]["title"], 1)
        self.assertEqual(media["disc_preview"]["duration_h"], "1小时46分")
        self.assertEqual(media["video"]["codec"], "h264")
        self.assertEqual(media["video"]["width"], 1920)
        self.assertEqual(media["video"]["height"], 1080)
        self.assertTrue(media["video"]["dynamic_range_known"])
        self.assertEqual(media["audio"][0]["codec"], "pcm_s16le")
        self.assertEqual(media["audio"][0]["channels"], 6)
        self.assertEqual(media["subs"][0]["codec"], "hdmv_pgs_subtitle")
        preview = app.format_preview("iso", media, "/share/download/movie.iso")
        self.assertEqual(preview["target"], "1920×1080 · 24 fps · SDR")
        self.assertIn("不会点亮杜比视界", preview["target_detail"])
        self.assertIn("所选音轨保留原编码", preview["target_detail"])        # LPCM 电视实测可直放
        self.assertIn("未选字幕", preview["target_detail"])
        self.assertTrue(preview["pre_read"])

    def test_disc_pre_read_uses_info_command_and_never_rips(self):
        iso = self.inbox / "movie.iso"
        iso.write_bytes(b"disc")
        seen = {}

        class Container:
            def exec_run(self, command, demux, environment=None):
                seen["command"] = command
                seen["demux"] = demux
                return types.SimpleNamespace(
                    exit_code=0,
                    output=(b'TINFO:0,9,0,"1:40:00"\n', b""),
                )

        client = types.SimpleNamespace(
            containers=types.SimpleNamespace(get=lambda name: Container()),
            close=lambda: None,
        )
        fake_docker = types.SimpleNamespace(from_env=lambda: client)
        with patch.dict(sys.modules, {"docker": fake_docker}):
            text = app.makemkv_info(iso)

        self.assertIn("TINFO", text)
        self.assertIn("info", seen["command"])
        self.assertNotIn("mkv", seen["command"])
        self.assertNotIn("backup", seen["command"])
        self.assertEqual(seen["command"][-1], f"iso:{iso}")
        self.assertTrue(seen["demux"])

    def test_unknown_uhd_dynamic_range_is_not_mislabeled_sdr_or_dolby(self):
        robot = """
TINFO:0,9,0,"2:00:00"
SINFO:0,0,1,6201,"Video"
SINFO:0,0,5,0,"V_MPEGH/ISO/HEVC"
SINFO:0,0,6,0,"MpegH HEVC Main10@L5.1"
SINFO:0,0,19,0,"3840x2160"
SINFO:0,0,21,0,"23.976 (24000/1001)"
"""
        media = app.parse_makemkv_info(robot)
        preview = app.format_preview("iso", media)
        self.assertIn("HDR/杜比待复核", preview["source"])
        self.assertIn("杜比视界待处理时复核", preview["target_detail"])
        self.assertNotIn("不会点亮杜比视界", preview["target_detail"])

    def test_probed_disc_preview_is_explicit_and_lossless(self):
        media = probe(
            "h264",
            subs=[{"codec": "hdmv_pgs_subtitle", "lang": "jpn", "title": ""}],
        )
        preview = app.format_preview("iso", media, "/tmp/rip.mkv")
        self.assertIn("1920×1080", preview["source"])
        self.assertIn("24 fps", preview["source"])
        self.assertIn("SDR", preview["source"])
        self.assertEqual(preview["target"], "1920×1080 · 24 fps · SDR")
        self.assertIn("不会点亮杜比视界", preview["target_detail"])
        self.assertIn("画面直通", preview["target_detail"])
        self.assertIn("未选字幕", preview["target_detail"])

    def test_h264_mp4_mux_uses_avc1_and_video_copy(self):
        source = self.inbox / "movie.mkv"
        source.write_bytes(b"x")
        with patch.object(app, "run_ffmpeg_progress") as run:
            app.ffmpeg_mp4_mux(
                {"log": []}, "封装MP4(G5直放)", source, probe("h264"),
                self.work / "movie.mp4", [], 6000,
            )
        command = run.call_args.args[1]
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-tag:v") + 1], "avc1")

    def test_hevc_mp4_mux_uses_hvc1(self):
        source = self.inbox / "movie.mkv"
        source.write_bytes(b"x")
        with patch.object(app, "run_ffmpeg_progress") as run:
            app.ffmpeg_mp4_mux(
                {"log": []}, "封装MP4(G5直放)", source, probe("hevc"),
                self.work / "movie.mp4", [], 6000,
            )
        command = run.call_args.args[1]
        self.assertEqual(command[command.index("-tag:v") + 1], "hvc1")

    def test_clean_mp4_keeps_its_extension(self):
        source = self.inbox / "clean.mp4"
        source.write_bytes(b"source")
        item = {
            "name": source.name,
            "kind": "file",
            "payload": str(source),
            "size": source.stat().st_size,
        }
        ffprobe_data = {
            "format": {"duration": "6000"},
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264",
                    "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
                    "r_frame_rate": "24/1",
                },
                {"codec_type": "audio", "codec_name": "eac3", "channels": 6},
            ],
        }
        # 不依赖运行测试的机器真实剩余空间（低于 25GB 时复制会被中止）。
        with patch.object(app, "ffprobe", return_value=ffprobe_data), \
                patch.object(app, "disk_free", return_value=10**15):
            job = {"log": []}
            output = app.process_item(job, item)
        self.assertEqual(output.suffix, ".mp4")
        self.assertEqual(output.read_bytes(), b"source")
        self.assertTrue(source.exists())
        self.assertEqual(job["format_preview"]["target"], "1920×1080 · 24 fps · SDR")

    def test_verification_detects_resolution_change(self):
        source = probe("h264")
        changed = probe("h264", height=2160)
        raw = {
            "format": {"duration": "6000"},
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264",
                    "width": changed["video"]["width"],
                    "height": changed["video"]["height"],
                    "pix_fmt": "yuv420p", "r_frame_rate": "24/1",
                },
                {"codec_type": "audio", "codec_name": "eac3", "channels": 6},
            ],
        }
        with patch.object(app, "ffprobe", return_value=raw):
            with self.assertRaisesRegex(RuntimeError, "宽度|高度"):
                app.verify_media_preserved(self.work / "output.mp4", source)

    def test_verification_detects_missing_text_subtitle(self):
        source = probe("h264")
        raw = {
            "format": {"duration": "6000"},
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264",
                    "width": 1920, "height": 1080,
                    "pix_fmt": "yuv420p", "r_frame_rate": "24/1",
                },
                {"codec_type": "audio", "codec_name": "eac3", "channels": 6},
            ],
        }
        with patch.object(app, "ffprobe", return_value=raw):
            with self.assertRaisesRegex(RuntimeError, "内嵌字幕"):
                app.verify_media_preserved(
                    self.work / "output.mp4", source,
                    expected_text_subtitles=1,
                )

    def test_iso_scans_without_ffprobe(self):
        iso = self.inbox / "Movie.ISO"
        iso.write_bytes(b"image")
        with patch.object(app, "ffprobe", side_effect=AssertionError("no ffprobe")):
            app.scan_once()
            app.scan_once()
        item = app.ITEMS[iso.name]
        self.assertEqual(item["kind"], "iso")
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["preview_status"], "queued")
        self.assertEqual(len(app.DISC_PREVIEW_QUEUE), 1)

    def test_disc_preview_cache_avoids_re_reading_unchanged_iso(self):
        iso = self.inbox / "Movie.ISO"
        iso.write_bytes(b"image")
        media = probe("h264")
        media["disc_preview"] = {
            "title": 0, "duration_h": "1小时40分", "title_name": "Movie"
        }
        signature = app.disc_preview_signature("iso", iso, iso.stat().st_size)
        app.DISC_PREVIEW_CACHE[signature] = {
            "probe": media, "updated_epoch": 1
        }
        app.scan_once()
        app.scan_once()
        self.assertEqual(app.ITEMS[iso.name]["preview_status"], "ready")
        self.assertEqual(app.ITEMS[iso.name]["probe"], media)
        self.assertEqual(app.DISC_PREVIEW_QUEUE, [])

    def test_disc_cannot_be_queued_while_preview_is_running(self):
        name = "movie.iso"
        app.ITEMS[name] = {
            "name": name, "kind": "iso", "status": "ready",
            "preview_status": "analyzing", "size": 100,
        }
        with patch.object(
            app.request, "get_json", return_value={"names": [name]}, create=True
        ):
            response = app.api_process()
        self.assertEqual(response["queued"], [])
        self.assertIn("正在预读", response["warnings"][0])
        self.assertEqual(app.JOB_QUEUE, [])

    def test_retry_keeps_old_output_and_restores_source_to_ready(self):
        source = self.inbox / "movie.iso"
        source.write_bytes(b"source")
        self.movies.mkdir()
        output = self.movies / "movie.mkv"
        output.write_bytes(b"old output")
        app.ITEMS[source.name] = {
            "name": source.name, "status": "done", "error": None
        }
        app.PROCESSED[source.name] = {
            "output": str(output), "ts": "07-25 20:00", "warnings": ["OCR失败"]
        }
        with patch.object(
            app.request, "get_json", return_value={"name": source.name}, create=True
        ):
            response = app.api_retry()
        self.assertTrue(response["ok"])
        self.assertEqual(app.ITEMS[source.name]["status"], "ready")
        self.assertNotIn(source.name, app.PROCESSED)
        self.assertTrue(output.exists())
        self.assertTrue(source.exists())

    def test_rebuild_deletes_only_tracked_movie_output_and_keeps_source(self):
        source = self.inbox / "movie.iso"
        source.write_bytes(b"source")
        self.movies.mkdir()
        output = self.movies / "movie.mp4"
        output.write_bytes(b"discarded output")
        sidecar = app.subtitle_sidecar_path(output)
        sidecar.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n中文字幕\n", "utf-8"
        )
        app.ITEMS[source.name] = {
            "name": source.name, "status": "done", "error": None
        }
        app.PROCESSED[source.name] = {
            "output": str(output), "subtitle_sidecar": str(sidecar),
            "ts": "07-25 20:00",
        }
        with patch.object(
            app.request, "get_json", return_value={"name": source.name}, create=True
        ):
            response = app.api_rebuild()
        self.assertTrue(response["ok"])
        self.assertFalse(output.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(source.exists())
        self.assertEqual(app.ITEMS[source.name]["status"], "ready")
        self.assertNotIn(source.name, app.PROCESSED)

    def test_rebuild_refuses_output_outside_movies(self):
        source = self.inbox / "movie.iso"
        source.write_bytes(b"source")
        outside = self.inbox / "must-keep.mp4"
        outside.write_bytes(b"keep")
        self.movies.mkdir()
        app.ITEMS[source.name] = {
            "name": source.name, "status": "done", "error": None
        }
        app.PROCESSED[source.name] = {"output": str(outside)}
        with patch.object(
            app.request, "get_json", return_value={"name": source.name}, create=True
        ):
            response, status = app.api_rebuild()
        self.assertEqual(status, 409)
        self.assertIn("不在 Movies", response["error"])
        self.assertTrue(outside.exists())
        self.assertTrue(source.exists())

    def test_rebuild_keeps_saved_subtitle_candidate(self):
        source = self.inbox / "movie.iso"
        source.write_bytes(b"source")
        self.movies.mkdir()
        output = self.movies / "movie.mp4"
        output.write_bytes(b"finished")
        app.ITEMS[source.name] = {
            "name": source.name, "status": "done", "error": None
        }
        app.PROCESSED[source.name] = {"output": str(output)}
        candidate = app.subtitle_candidate_path(source.name)
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n保留字幕\n", "utf-8"
        )
        with patch.object(
            app.request, "get_json", return_value={"name": source.name}, create=True
        ):
            response = app.api_rebuild()
        self.assertTrue(response["ok"])
        self.assertFalse(output.exists())
        self.assertTrue(candidate.exists())

    def test_human_chinese_subtitle_is_retimed_and_only_gaps_use_ai(self):
        name = "movie.iso"
        candidate = app.subtitle_candidate_path(name)
        candidate.parent.mkdir(parents=True)
        candidate_cues = [
            {"start": 10_000 + i * 10_000, "end": 12_000 + i * 10_000,
             "text": f"人工译文{i}"}
            for i in range(8)
        ]
        app.write_srt(candidate, candidate_cues)
        japanese = self.work / "japanese.srt"
        anchor_cues = [
            {"start": 610_000 + i * 10_000, "end": 612_000 + i * 10_000,
             "text": f"日本語{i}"}
            for i in range(8)
        ]
        anchor_cues.append(
            {"start": 705_000, "end": 707_000, "text": "追加の台詞"}
        )
        app.write_srt(japanese, anchor_cues)

        def translate(cues, _config):
            self.assertEqual([cue["text"] for cue in cues], ["追加の台詞"])
            return [{**cues[0], "text": "AI补译缺段", "origin": "ai"}]

        job = {"log": []}
        with patch.object(app, "subtitle_ai_config", return_value={"configured": True}), \
                patch.object(app, "ai_translate_missing", side_effect=translate):
            output = app.build_hybrid_chinese_subtitle(
                job, name, [(japanese, "jpn")], self.work
            )

        cues = app.parse_srt_text(output.read_text("utf-8"))
        self.assertEqual(cues[0]["start"], 610_000)
        self.assertEqual(cues[0]["text"], "人工译文0")
        self.assertEqual(cues[-1]["text"], "AI补译缺段")
        self.assertTrue(any("匹配度" in line for line in job["log"]))

    def test_subtitle_candidate_api_normalizes_uploaded_srt(self):
        name = "movie.iso"
        app.ITEMS[name] = {"name": name, "status": "ready"}
        text = "\n\n".join(
            f"{i + 1}\n00:00:{i:02d},000 --> 00:00:{i:02d},800\n人工译文{i}"
            for i in range(5)
        ).encode("utf-8")
        upload = types.SimpleNamespace(
            filename="release.zh-CN.srt",
            read=lambda _limit: text,
        )
        with patch.object(app.request, "form", {"name": name}, create=True), \
                patch.object(
                    app.request, "files", {"subtitle": upload}, create=True
                ):
            response = app.api_subtitle_candidate()
        self.assertTrue(response["ok"])
        self.assertEqual(response["cue_count"], 5)
        saved = app.load_subtitle_candidate(name)
        self.assertEqual(saved[1][0]["text"], "人工译文0")
        self.assertEqual(
            saved[0].with_suffix(".name").read_text("utf-8"),
            "release.zh-CN.srt",
        )

    def test_jellyfin_subtitle_listing_treats_numeric_suffix_as_order_only(self):
        raw = "\n".join([
            "1721980000.0\t116736\t"
            "/config/metadata/library/ab/item/Movie.zho.0.srt",
            "1721970000.0\t109568\t"
            "/config/metadata/library/ab/item/Movie.zho.srt",
            "bad listing",
            "1721990000.0\t12\t/etc/not-allowed.srt",
        ]).encode()
        options = app._parse_jellyfin_subtitle_listing(raw)
        self.assertEqual(
            [item["filename"] for item in options],
            ["Movie.zho.0.srt", "Movie.zho.srt"],
        )
        rendered = json.dumps(options, ensure_ascii=False)
        self.assertNotIn("人工", rendered)
        self.assertNotIn("AI", rendered)

    def test_jellyfin_selected_subtitle_is_saved_as_persistent_candidate(self):
        name = "movie.iso"
        app.ITEMS[name] = {"name": name, "status": "ready"}
        text = "\n\n".join(
            f"{i + 1}\n00:00:{i:02d},000 --> 00:00:{i:02d},800\n下载译文{i}"
            for i in range(5)
        ).encode("utf-8")
        with patch.object(
            app.request, "get_json",
            return_value={"name": name, "subtitle_id": "safe-id"},
            create=True,
        ), patch.object(
            app, "jellyfin_subtitle_bytes",
            return_value=("Movie.zho.1.srt", text),
        ):
            response = app.api_jellyfin_subtitle_candidate()
        self.assertTrue(response["ok"])
        saved = app.load_subtitle_candidate(name)
        self.assertEqual(saved[1][0]["text"], "下载译文0")
        self.assertEqual(
            saved[0].with_suffix(".name").read_text("utf-8"),
            "Movie.zho.1.srt",
        )

    def test_finished_movie_gets_aligned_sidecar_without_rewriting_video(self):
        name = "movie.iso"
        self.movies.mkdir()
        output = self.movies / "movie.mp4"
        original_video = b"finished-video-must-not-change"
        output.write_bytes(original_video)
        app.ITEMS[name] = {"name": name, "status": "done"}
        app.PROCESSED[name] = {"output": str(output)}
        # 参照是转换时存下的“影片自带字幕的时间点”：只有时间，没有文字
        app.write_settings_json(app.subtitle_timing_path(output), {"tracks": [{
            "codec": "hdmv_pgs_subtitle", "lang": "jpn",
            "points": [[610_000 + i * 10_000, 612_000 + i * 10_000] for i in range(8)]}]})
        candidate_path = self.work / "candidate.srt"
        app.write_srt(candidate_path, [
            {
                "start": 10_000 + i * 10_000,
                "end": 12_000 + i * 10_000,
                "text": f"人工译文{i}",
            }
            for i in range(8)
        ])
        with patch.object(app, "subtitle_ai_config", return_value=None):
            # 打开窗口：只显示原样，不自动算，但告诉页面“可以自动校准”
            opened = app.store_candidate_for_item(
                name, "release.zho.0.srt", candidate_path.read_bytes()
            )
            self.assertTrue(opened["confirmation_required"])
            self.assertFalse(opened["automatic_available"])
            self.assertTrue(opened["can_auto"])
            self.assertEqual(opened["automatic_first_start_ms"], 10_000)
            # 用户点了“自动校准”：给出结果，仍不写文件；点应用才生效
            preview = app.store_candidate_for_item(
                name, "release.zho.0.srt", candidate_path.read_bytes(), auto=True
            )
            self.assertTrue(preview["confirmation_required"])
            self.assertTrue(preview["automatic_available"])
            self.assertEqual(preview["automatic_first_start_ms"], 610_000)
            self.assertFalse(app.subtitle_sidecar_path(output).exists())
            result = app.store_candidate_for_item(
                name, "release.zho.0.srt", candidate_path.read_bytes(),
                confirm_audio=True, manual_first_start_ms=610_000,
            )
        sidecar = app.subtitle_sidecar_path(output)
        self.assertTrue(result["applied"])
        self.assertTrue(sidecar.is_file())
        self.assertEqual(output.read_bytes(), original_video)
        cues = app.parse_srt_text(sidecar.read_text("utf-8"))
        self.assertEqual(cues[0]["start"], 610_000)
        self.assertEqual(cues[0]["text"], "人工译文0")
        self.assertEqual(result["scale"], 1.0)

    def test_movie_without_timing_reference_asks_for_the_first_line_time(self):
        name = "movie.iso"
        self.movies.mkdir()
        output = self.movies / "movie.mp4"
        original_video = b"finished-video-must-not-change"
        output.write_bytes(original_video)
        app.ITEMS[name] = {"name": name, "status": "done"}
        app.PROCESSED[name] = {"output": str(output)}
        candidate_path = self.work / "candidate.srt"
        app.write_srt(candidate_path, [
            {"start": 10_000 + i * 10_000, "end": 12_000 + i * 10_000, "text": f"人工译文{i}"}
            for i in range(8)
        ])
        with patch.object(app, "align_chinese_subtitle_by_audio",
                          side_effect=AssertionError("不应再靠声音估时间")):
            opened = app.store_candidate_for_item(name, "release.zho.srt", candidate_path.read_bytes())
            self.assertFalse(opened["can_auto"])                       # 没有参照：页面上“自动校准”不可点
            self.assertEqual(opened["automatic_error"], "")
            preview = app.store_candidate_for_item(name, "release.zho.srt", candidate_path.read_bytes(), auto=True)
            self.assertTrue(preview["confirmation_required"])
            self.assertFalse(preview["automatic_available"])
            self.assertIn("无可参照的字幕时间点", preview["automatic_error"])
            self.assertEqual(preview["preview_cues"][0]["start_ms"], 10_000)       # 预览就是原样，没乱挪
            self.assertFalse(app.subtitle_sidecar_path(output).exists())
            self.assertIsNone(app.load_subtitle_candidate(name))
            applied = app.store_candidate_for_item(
                name, "release.zho.srt", candidate_path.read_bytes(),
                confirm_audio=True, manual_first_start_ms=625_500)
        sidecar = app.subtitle_sidecar_path(output)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["alignment_method"], "manual_first_cue")
        self.assertEqual(applied["offset_seconds"], 615.5)
        self.assertEqual(output.read_bytes(), original_video)
        self.assertEqual(app.parse_srt_text(sidecar.read_text("utf-8"))[0]["start"], 625_500)

    def test_timing_reference_that_does_not_match_falls_back_to_manual(self):
        # 参照和字幕对不上（不同剪辑版）：不硬套，改让用户手动填
        name = "movie.iso"
        self.movies.mkdir()
        output = self.movies / "movie.mp4"; output.write_bytes(b"v")
        app.ITEMS[name] = {"name": name, "status": "done"}
        app.PROCESSED[name] = {"output": str(output)}
        app.write_settings_json(app.subtitle_timing_path(output), {"tracks": [{
            "points": [[t, t + 1500] for t in (3_000, 9_500, 61_000, 64_200, 190_000, 191_900, 400_000, 733_000)]}]})
        candidate_path = self.work / "candidate.srt"
        app.write_srt(candidate_path, [
            {"start": 10_000 + i * 10_000, "end": 12_000 + i * 10_000, "text": f"人工译文{i}"} for i in range(8)])
        preview = app.store_candidate_for_item(name, "release.zho.srt", candidate_path.read_bytes(), auto=True)
        self.assertTrue(preview["confirmation_required"])
        self.assertIn("不匹配", preview["automatic_error"])
        self.assertFalse(app.subtitle_sidecar_path(output).exists())

    def test_first_cue_calibration_ignores_suspicious_scale_and_keeps_all_cues(self):
        name = "featurette-before-movie.iso"
        self.movies.mkdir()
        output = self.movies / "movie.mp4"
        output.write_bytes(b"video-must-stay-identical")
        app.ITEMS[name] = {"name": name, "status": "done"}
        app.PROCESSED[name] = {"output": str(output)}
        candidate_path = self.work / "candidate.srt"
        source_cues = [
            {
                "start": 1_094_719 + i * 8_000,
                "end": 1_098_775 + i * 8_000,
                "text": f"人工译文{i}",
            }
            for i in range(8)
        ]
        app.write_srt(candidate_path, source_cues)

        with patch.object(app, "ffprobe", return_value=None):
            preview = app.store_candidate_for_item(
                name, "release.zho.srt", candidate_path.read_bytes()
            )
            self.assertFalse(preview["automatic_suspicious"])
            self.assertEqual(preview["source_first_start_ms"], 1_094_719)
            self.assertEqual(preview["automatic_first_start_ms"], 1_094_719)     # 没有自动结果时预览保持原样

            applied = app.store_candidate_for_item(
                name, "release.zho.srt", candidate_path.read_bytes(),
                confirm_audio=True,
                manual_first_start_ms=652_499,
            )

        cues = app.parse_srt_text(
            app.subtitle_sidecar_path(output).read_text("utf-8")
        )
        self.assertEqual(len(cues), len(source_cues))
        self.assertEqual(cues[0]["start"], 652_499)
        self.assertEqual(cues[1]["start"] - cues[0]["start"], 8_000)
        self.assertEqual(applied["scale"], 1.0)
        self.assertEqual(applied["alignment_method"], "manual_first_cue")

    def test_manual_subtitle_offset_validation_and_negative_shift(self):
        self.assertIsNone(app.normalize_manual_subtitle_offset(None))
        self.assertEqual(app.normalize_manual_subtitle_offset("-2.3456"), -2.346)
        for invalid in ("hello", float("inf"), 2701):
            with self.assertRaises(ValueError):
                app.normalize_manual_subtitle_offset(invalid)

        source = self.work / "source.srt"
        target = self.work / "shifted.srt"
        app.write_srt(source, [
            {
                "start": 10_000 + i * 10_000,
                "end": 12_000 + i * 10_000,
                "text": f"字幕{i}",
            }
            for i in range(8)
        ])
        app.shift_aligned_subtitle(source, -5.25, target)
        shifted = app.parse_srt_text(target.read_text("utf-8"))
        self.assertEqual(shifted[0]["start"], 4_750)
        self.assertEqual(shifted[0]["end"], 6_750)

        self.assertEqual(
            app.normalize_manual_first_start_ms("625500", 7_000_000), 625_500
        )
        for invalid in ("not-a-time", -1, 7_200_001):
            with self.assertRaises(ValueError):
                app.normalize_manual_first_start_ms(invalid, 7_080_000)

    def test_audio_alignment_uses_large_offset_guard_and_multi_segment_mode(self):
        video = self.work / "movie.mp4"
        video.write_bytes(b"video")
        output = self.work / "aligned.srt"
        cues = [
            {"start": i * 10_000, "end": i * 10_000 + 2_000, "text": f"字幕{i}"}
            for i in range(8)
        ]
        seen = {}

        def run(arguments):
            seen["arguments"] = arguments
            app.write_srt(output, [
                {**cue, "start": cue["start"] + 600_000,
                 "end": cue["end"] + 600_000}
                for cue in cues
            ])
            return {
                "sync_was_successful": True,
                "offset_seconds": 600.0,
                "framerate_scale_factor": 1.0,
            }

        with patch.object(app, "_run_ffsubsync", side_effect=run), \
                patch.object(app, "ffprobe", return_value=None):
            aligned, meta = app.align_chinese_subtitle_by_audio(
                {"log": []}, video, cues, output
            )
        self.assertEqual(aligned, output)
        self.assertEqual(meta["offset_seconds"], 600.0)
        self.assertIn("--multi-segment-sync", seen["arguments"])
        self.assertEqual(
            seen["arguments"][seen["arguments"].index("--max-offset-seconds") + 1],
            "1200",
        )

    def test_low_quality_audio_alignment_removes_unsynced_output(self):
        video = self.work / "movie.mp4"
        video.write_bytes(b"video")
        output = self.work / "aligned.srt"
        cues = [
            {"start": i * 10_000, "end": i * 10_000 + 2_000, "text": f"字幕{i}"}
            for i in range(8)
        ]

        def low_quality(_arguments):
            app.write_srt(output, cues)
            return {
                "sync_was_successful": False,
                "offset_seconds": 0.0,
                "framerate_scale_factor": 1.0,
            }

        with patch.object(app, "_run_ffsubsync", side_effect=low_quality):
            with self.assertRaisesRegex(ValueError, "可信度"):
                app.align_chinese_subtitle_by_audio(
                    {"log": []}, video, cues, output
                )
        self.assertFalse(output.exists())

    def test_remove_candidate_deletes_sidecar_but_keeps_video_and_anchor(self):
        name = "movie.iso"
        self.movies.mkdir()
        output = self.movies / "movie.mp4"
        output.write_bytes(b"video")
        app.ITEMS[name] = {"name": name, "status": "done"}
        sidecar = app.subtitle_sidecar_path(output)
        sidecar.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n中文字幕\n", "utf-8"
        )
        app.PROCESSED[name] = {
            "output": str(output), "subtitle_sidecar": str(sidecar)
        }
        candidate = app.subtitle_candidate_path(name)
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n候选字幕\n", "utf-8"
        )
        candidate.with_suffix(".name").write_text("wrong.srt", "utf-8")
        anchor = app.subtitle_anchor_path(name)
        anchor.parent.mkdir(parents=True)
        anchor.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n日本語\n", "utf-8"
        )
        removed = app.remove_candidate_and_sidecar(name)
        self.assertTrue(removed)
        self.assertFalse(candidate.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(output.exists())
        self.assertTrue(anchor.exists())

    def test_ai_translator_sends_only_requested_missing_cues(self):
        seen = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return (
                    b'{"choices":[{"message":{"content":'
                    b'"[{\\"id\\":0,\\"text\\":\\"AI translated\\"}]"}}]}'
                )

        def urlopen(request, timeout):
            seen["payload"] = json.loads(request.data.decode("utf-8"))
            seen["timeout"] = timeout
            return Response()

        missing = [{"start": 1_000, "end": 2_000, "text": "不足部分"}]
        with patch.object(app.urlrequest, "urlopen", side_effect=urlopen):
            translated = app.ai_translate_missing(
                missing,
                {
                    "endpoint": "https://example.invalid/v1/chat/completions",
                    "api_key": "secret",
                    "model": "subtitle-model",
                },
            )
        user_prompt = seen["payload"]["messages"][1]["content"]
        self.assertIn("不足部分", user_prompt)
        self.assertNotIn("人工译文", user_prompt)
        self.assertEqual(translated[0]["text"], "AI translated")
        self.assertEqual(seen["timeout"], 90)

    def test_low_confidence_candidate_is_not_forced_onto_disc(self):
        anchor = [
            {"start": i * 1000, "end": i * 1000 + 300, "text": "日"}
            for i in range(8)
        ]
        candidate = [
            {"start": i * 73_000, "end": i * 73_000 + 300, "text": "中"}
            for i in range(8)
        ]
        self.assertIsNone(app.estimate_subtitle_transform(anchor, candidate))

    def test_upgrade_files_are_syntax_checked_and_context_is_minimal(self):
        app_bytes = b"print('ok')\n"
        docker_bytes = (
            b"FROM python:3.11-slim\nCOPY app.py /app/app.py\n"
            b'CMD [\"python\", \"/app/app.py\"]\n'
        )
        app.validate_upgrade_files(app_bytes, docker_bytes)
        with tarfile.open(fileobj=app.docker_build_context(app_bytes, docker_bytes)) as archive:
            self.assertEqual(sorted(archive.getnames()), ["Dockerfile", "app.py"])
            self.assertEqual(archive.extractfile("app.py").read(), app_bytes)
        with self.assertRaisesRegex(ValueError, "语法检查失败"):
            app.validate_upgrade_files(b"def broken(:\n", docker_bytes)

    def test_upgrade_api_rejects_public_network_and_busy_pipeline(self):
        valid_app = io.BytesIO(b"print('ok')\n")
        valid_docker = io.BytesIO(
            b"FROM python:3.11-slim\nCOPY app.py /app/app.py\n"
            b'CMD [\"python\", \"/app/app.py\"]\n'
        )
        files = {"app_py": valid_app, "dockerfile": valid_docker}
        with patch.object(app.request, "remote_addr", "8.8.8.8", create=True), \
                patch.object(app.request, "files", files, create=True):
            response, status = app.api_upgrade()
        self.assertEqual(status, 403)
        self.assertIn("局域网", response["error"])

        files["app_py"].seek(0)
        files["dockerfile"].seek(0)
        app.CURRENT_JOB = {"name": "busy"}
        try:
            with patch.object(app.request, "remote_addr", "192.168.1.21", create=True), \
                    patch.object(app.request, "files", files, create=True), \
                    patch.object(app.threading, "Thread") as thread:
                response, status = app.api_upgrade()
        finally:
            app.CURRENT_JOB = None
        self.assertEqual(status, 409)
        self.assertIn("任务正在运行", response["error"])
        thread.assert_not_called()

    def test_failed_upgrade_preparation_removes_candidate_image(self):
        class Images:
            def __init__(self):
                self.removed = []

            def build(self, **_kwargs):
                return object(), []

            def remove(self, image, force=False):
                self.removed.append((image, force))

        current = types.SimpleNamespace(
            id="old-id",
            attrs={"Mounts": [{
                "Type": "bind", "Destination": "/share",
                "Source": "/volume2/Share",
            }]},
            reload=lambda: None,
        )

        class Containers:
            def __init__(self):
                self.lookups = []

            def get(self, _ref):
                self.lookups.append(_ref)
                return current

            def run(self, *_args, **_kwargs):
                raise RuntimeError("helper launch failed")

        containers = Containers()
        client = types.SimpleNamespace(images=Images(), containers=containers)
        fake_docker = types.SimpleNamespace(from_env=lambda: client)
        with patch.dict(sys.modules, {"docker": fake_docker}):
            app.build_and_schedule_upgrade(
                b"print('ok')\n",
                b"FROM python:3.11-slim\nCOPY app.py /app/app.py\nCMD [\"python\",\"/app/app.py\"]\n",
                "test-id",
            )
        self.assertEqual(
            client.images.removed, [("mediapanel:update-test-id", True)]
        )
        self.assertEqual(containers.lookups, ["mediapanel"])
        self.assertEqual(app.read_upgrade_status()["status"], "failed")

    def test_upgrade_helper_tracks_partial_switch_for_safe_rollback(self):
        compile(app.UPGRADE_HELPER, "<upgrade-helper>", "exec")
        self.assertIn("old_stopped = False", app.UPGRADE_HELPER)
        self.assertIn("old_renamed = False", app.UPGRADE_HELPER)
        self.assertIn("if old_renamed:", app.UPGRADE_HELPER)
        self.assertIn("if old_stopped:", app.UPGRADE_HELPER)


class InterfaceRegressionTests(unittest.TestCase):
    def test_dashboard_keeps_safety_controls(self):
        for marker in (
            'aria-label="媒体状态筛选"',
            'id="rows" class="cards"',
            "data-go=", "data-asis=", "data-cancel=", "data-audio=", "data-sub=", "data-all=",
            'role="dialog"',
            'id="deleteConfirm"',
            'id="rebuildConfirm"',
            'id="upgradeConfirm"',
            "/api/rebuild",
            "/api/upgrade",
            "/api/subtitle_candidate",
            "/api/subtitles/list", "/api/subtitles/search", "/api/subtitles/download", "/api/subtitles/delete",
            "/api/subtitle_candidate/jellyfin",
            'id="subtitlePickerModal"',
            'id="subtitleSyncModal"',
            'id="subtitleSyncConfirm"',
            'id="subtitleSyncManual"',
            'data-sync-step="-10"',
            "从电脑上传",
            "校准字幕时间",
            "首句出现时间",
        ):
            self.assertIn(marker, app.PAGE)
        self.assertNotIn("alert(", app.PAGE)
        self.assertNotIn("confirm(", app.PAGE)
        # 页面做减法：品牌名、图标、概览卡片、字幕 AI 入口都不再出现（后台接口保留）
        for gone in ("蓝光媒体处理台</h1>", "brandmark\" aria", "运行概览", "subtitleAi", "dialog-mark\" aria",
                     "封装MKV", "探测流信息"):
            self.assertNotIn(gone, app.PAGE.split("<script>")[0])
        self.assertIn("plainPhase(j.phase)", app.PAGE)

    def test_docker_image_exposes_chinese_model_name_expected_by_pgsrip(self):
        dockerfile = (APP_PATH.parent / "Dockerfile").read_text()
        self.assertIn("zho.traineddata", dockerfile)
        self.assertIn("pgsrip==0.1.12", dockerfile)
        self.assertIn("ffsubsync==0.5.0", dockerfile)


if __name__ == "__main__":
    unittest.main()
