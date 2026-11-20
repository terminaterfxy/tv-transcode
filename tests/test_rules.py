"""2026-11-17 起的转换规则：以 LG G5 实测支持表为依据，只留一条音轨和中文字幕。"""
import shutil
import time
import subprocess
import sys
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_media_pipeline as fixtures

app = fixtures.app


def audio(codec, lang="jpn", channels=6, title="", default=False, profile=""):
    return {"codec": codec, "lang": lang, "channels": channels, "title": title,
            "default": default, "profile": profile}


def sub(codec, lang="chi", title="", forced=False):
    return {"codec": codec, "lang": lang, "title": title, "forced": forced, "default": False}


def probe(video="h264", dv=0, audio_tracks=None, subs=None):
    media = fixtures.probe(video, dv=dv)
    media["audio"] = audio_tracks if audio_tracks is not None else [audio("eac3", default=True)]
    media["subs"] = subs or []
    return media


EVA = probe(audio_tracks=[
    audio("dts", "jpn", 6, "DTS-HD MA 5.1", default=True, profile="DTS-HD MA"),
    audio("dts", "jpn", 7, "DTS-ES 6.1", profile="DTS-ES"),
    audio("flac", "jpn", 2, "FLAC 2.0"),
    audio("dts", "eng", 6, "DTS-HD MA 5.1 (ADV)", profile="DTS-HD MA"),
    audio("ac3", "eng", 2, "Commentary by Director"),
], subs=[
    sub("hdmv_pgs_subtitle", "eng", "English"),
    sub("subrip", "eng", "English"),
    sub("hdmv_pgs_subtitle", "chi", "Chinese (Traditional)"),
    sub("hdmv_pgs_subtitle", "jpn", "Japanese (SDH)"),
])


class RuleTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    tearDown = fixtures.PipelineTests.tearDown

    def request(self, data):
        return patch.object(app, "request", types.SimpleNamespace(get_json=lambda **kw: data))

    # ---- 实测支持表 ----
    def test_which_audio_the_tv_plays_directly(self):
        for codec in ("eac3", "ac3", "aac", "pcm_s24le"):
            self.assertTrue(app.audio_tv_direct(codec, "mkv"), codec)
        for codec in ("dts", "truehd", "flac", "mlp", "opus"):
            self.assertFalse(app.audio_tv_direct(codec, "mkv"), codec)
        self.assertFalse(app.audio_tv_direct("pcm_s24le", "mp4"))       # LPCM 只验证过 MKV
        self.assertEqual(app.target_container("iso", probe("hevc")), "mkv")
        self.assertEqual(app.target_container("file", probe("hevc", dv=8)), "mp4")
        self.assertFalse(app.OCR_SUBS)

    # ---- 自动预选 ----
    def test_default_pick_is_best_original_language_track_and_chinese_subtitle(self):
        picked = app.default_selection(EVA)
        self.assertEqual(picked, {"audio": [0], "subs": [2]})
        # 同语言里无损优先于有损，即使有损那条标了默认
        media = probe(audio_tracks=[audio("ac3", "eng", 6, default=True),
                                    audio("truehd", "eng", 8, "TrueHD Atmos 7.1")])
        self.assertEqual(app.default_selection(media)["audio"], [1])
        # 评论音轨即使排第一也不会被选中
        media = probe(audio_tracks=[audio("ac3", "eng", 2, "Director Commentary"), audio("eac3", "eng", 6)])
        self.assertEqual(app.default_selection(media)["audio"], [1])

    def test_default_pick_follows_the_default_flag_even_if_it_is_a_dub(self):
        # 判断“原声”只能靠线索；配音排第一的版本会选到配音，所以页面必须能改
        media = probe(audio_tracks=[audio("ac3", "chi", 6, "国语", default=True), audio("truehd", "eng", 8)])
        self.assertEqual(app.default_selection(media)["audio"], [0])

    def test_chinese_subtitle_preference(self):
        media = probe(subs=[sub("hdmv_pgs_subtitle", "chi", "Chinese (Traditional)"),
                            sub("subrip", "chi", "简体中文"),
                            sub("subrip", "chi", "简体中文", forced=True),
                            sub("dvd_subtitle", "chi", "简体")])
        self.assertEqual(app.default_selection(media)["subs"], [1])
        self.assertEqual(app.default_selection(probe(subs=[sub("subrip", "eng")]))["subs"], [])
        titled = probe(subs=[sub("subrip", "und", "中文字幕")])
        self.assertEqual(app.default_selection(titled)["subs"], [0])

    def test_user_selection_is_validated(self):
        self.assertEqual(app.normalize_selection(EVA, {"audio": [3], "subs": []}), {"audio": [3], "subs": []})
        self.assertEqual(app.normalize_selection(EVA, {"audio": [99, "x", True], "subs": [7]}),
                         {"audio": [0], "subs": []})                 # 越界的丢掉，音轨不能一条不留
        self.assertEqual(app.normalize_selection(EVA, None), app.default_selection(EVA))
        everything = app.normalize_selection(EVA, {"all": True})
        self.assertEqual(everything["audio"], [0, 1, 2, 3, 4])
        self.assertEqual(everything["subs"], [0, 1, 2, 3])
        dvd = probe(subs=[sub("dvd_subtitle", "chi")])
        self.assertEqual(app.normalize_selection(dvd, {"subs": [0]})["subs"], [])

    def test_disc_selection_is_remapped_after_extraction(self):
        before = probe(audio_tracks=[audio("truehd", "eng"), audio("ac3", "eng"), audio("ac3", "chi")],
                       subs=[sub("hdmv_pgs_subtitle", "eng"), sub("hdmv_pgs_subtitle", "chi")])
        after = probe(audio_tracks=[audio("ac3", "chi"), audio("truehd", "eng"), audio("ac3", "eng")],
                      subs=[sub("hdmv_pgs_subtitle", "chi"), sub("hdmv_pgs_subtitle", "eng")])
        self.assertEqual(app.remap_selection(before, after, {"audio": [0], "subs": [1]}),
                         {"audio": [1], "subs": [0]})
        self.assertIsNone(app.remap_selection(before, probe(audio_tracks=[audio("ac3", "eng")]), {"audio": [0]}))
        self.assertEqual(app.remap_selection(before, after, {"all": True}), {"all": True})

    # ---- 要不要转 ----
    def test_verdicts(self):
        def needs(kind, media, name):
            return app.make_plan(kind, media, name)["needs"]
        self.assertEqual(needs("iso", probe(), "movie.iso"), "must")
        self.assertEqual(needs("file", probe("hevc", dv=8), "movie.mkv"), "must")         # 杜比视界必须进 MP4
        self.assertEqual(needs("file", probe("hevc", dv=7), "movie.mp4"), "must")         # P7 电视不认
        self.assertEqual(needs("file", probe("hevc", dv=8), "movie.mp4"), "none")
        self.assertEqual(needs("file", EVA, "movie.mkv"), "should")                       # DTS 电视解不了
        self.assertEqual(needs("file", probe(audio_tracks=[audio("pcm_s24le")]), "movie.mkv"), "none")
        with_pgs = probe(subs=[sub("hdmv_pgs_subtitle", "chi")])
        self.assertEqual(needs("file", with_pgs, "movie.mkv"), "should")                  # 字幕要拿出来做成外挂
        self.assertEqual(app.make_plan("file", with_pgs, "movie.mkv", {"audio": [0], "subs": []})["should"], ["selection"])
        self.assertEqual(needs("file", probe(), "movie.m2ts"), "should")
        # 音轨可以复制，但只保留选中轨道仍需重新封装
        self.assertEqual(app.make_plan("file", EVA, "movie.mkv", {"audio": [4], "subs": []})["should"], ["selection"])

    def test_plan_details_and_card(self):
        plan = app.make_plan("file", EVA, "movie.mkv")
        self.assertEqual(plan["container"], "mkv")
        self.assertEqual(plan["audio"], [{"src": 0, "compat": True, "keep": True, "lossy": False}])
        self.assertEqual(plan["subs"], [{"src": 2, "how": "sidecar"}])
        self.assertIn("字幕内嵌在影片中，电视加载需数分钟", plan["now"])
        self.assertIn("字幕即时显示", plan["gains"])
        dv = probe("hevc", dv=8, audio_tracks=[audio("truehd", "eng", 8), audio("pcm_s24le", "eng")],
                   subs=[sub("hdmv_pgs_subtitle", "chi"), sub("subrip", "chi", "简体")])
        plan = app.make_plan("file", dv, "movie.mkv", {"audio": [0, 1], "subs": [0, 1]})
        self.assertEqual(plan["container"], "mp4")
        self.assertEqual([e["how"] for e in plan["subs"]], ["sidecar", "sidecar"])
        self.assertEqual([(e["keep"], e["lossy"]) for e in plan["audio"]], [(True, False), (False, True)])
        self.assertIn("部分音轨仅保留压缩版本", plan["costs"])

        card = app.conversion_card("file", EVA, "/x/movie.mkv", 25 * 1024**3)
        self.assertEqual((card["verdict"], card["verdict_label"]), ("should", "建议转换"))
        self.assertTrue(card["can_import"])
        self.assertEqual(card["selected"], {"audio": [0], "subs": [2]})
        # 下拉选项用人话：语言、声音效果、音质档次；推荐的标出来；不出现 DTS、FLAC 这类名字
        self.assertEqual([a["label"] for a in card["audio"]], [
            "日语 · 环绕声 · 最高音质（推荐）", "日语 · 环绕声 · 普通音质", "日语 · 立体声 · 最高音质",
            "英语 · 环绕声 · 最高音质", "英语 · 评论解说（不是正片声音）"])
        self.assertEqual([x["label"] for x in card["subs"]],
                         ["英语（第 1 条）", "英语（第 2 条）", "繁体中文（推荐）", "日语 · 含音效说明"])
        dubs = probe(audio_tracks=[audio("dts", "eng", 6, "DTS-HD MA 5.1 (ADV)", profile="DTS-HD MA"),
                                   audio("dts", "eng", 6, "DTS-HD MA 5.1 (Netflix)", profile="DTS-HD MA")])
        self.assertEqual([a["label"] for a in app.conversion_card("file", dubs, "/x/m.mkv", 1)["audio"]],
                         ["英语 · 环绕声 · 最高音质（ADV）（推荐）", "英语 · 环绕声 · 最高音质（Netflix）"])
        # 卡片先说“现在”直接播是什么效果，再说转换后；只讲看到听到的效果，不出现术语
        self.assertEqual(len(card["now"]), 2)                # 声音一条、字幕一条
        self.assertIn("电视加载需数分钟", card["now"][1])
        self.assertIn("电视不支持此音轨", card["now"][0])
        self.assertIn("音质下降", card["now"][0])
        self.assertIn("音质提升，即点即播", card["gains"])
        everything = " ".join(card["now"] + card["gains"] + card["costs"])
        for jargon in ("640k", "1536k", "EAC3", "转码", "边播边转", "兼容轨", "MKV", "MP4"):
            self.assertNotIn(jargon, everything)
        self.assertTrue(card["costs"][0].startswith("预计耗时 "))
        self.assertTrue(any("仅含所选的 1 条音轨和 1 条字幕" in cost for cost in card["costs"]))
        self.assertEqual(card["by_audio"]["0"]["verdict"], "should")
        self.assertEqual(card["by_audio"]["4"]["verdict"], "should")      # 改选 AC3 那条：声音不用动，但字幕还要拿出来
        self.assertEqual(card["all"]["verdict"], "should")
        disc = app.conversion_card("bdmv", probe("hevc", dv=7), "/x/disc", 60 * 1024**3)
        self.assertEqual(disc["verdict"], "must")
        self.assertIn("蓝光原盘，电视无法直接播放", disc["now"])
        self.assertTrue(any("无法点亮" in line for line in disc["now"]))
        self.assertIn("杜比视界可以点亮", disc["gains"])
        self.assertFalse(disc["can_import"])
        fine = app.conversion_card("file", probe(), "/x/movie.mkv", 1024**3)
        self.assertEqual(fine["verdict"], "none")
        self.assertEqual(fine["gains"], ["电视可直接播放，画面和声音保持原样",
                                         "无中文字幕，可在入库后添加"])
        self.assertEqual(fine["now"], [])
        with_chinese = app.conversion_card("file", probe(subs=[sub("subrip", "chi")]), "/x/movie.mkv", 1024**3)
        self.assertEqual(with_chinese["verdict"], "should")
        self.assertEqual(with_chinese["gains"], ["字幕即时显示"])
        self.assertIsNone(app.conversion_card("iso", None, "/x/movie.iso", 1))

    # ---- 封装命令 ----
    def test_mkv_arguments_keep_only_the_selection(self):
        args, audio_count, sub_count = app.audio_sub_args(EVA)
        text = " ".join(args)
        self.assertEqual((audio_count, sub_count), (2, 0))                       # 字幕不装进影片文件
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "-map" and ":a:" in args[i + 1]],
                         ["0:a:0", "0:a:0"])
        self.assertIn("-c:a:0 eac3 -b:a:0 1536k", text)
        self.assertIn("title=电视用", args)
        self.assertIn("title=原始音质（电视播不了，留给功放或回音壁）", args)
        self.assertIn("-disposition:a:0 default", text)
        self.assertIn("-c:a:1 copy -disposition:a:1 0", text)
        self.assertNotIn("0:s:", text)
        # 字幕各存成同名外挂文件：图形的 .sup，文本的 .srt，特效字幕 .ass；DVD 字幕不带
        media = probe(subs=[sub("hdmv_pgs_subtitle", "chi"), sub("subrip", "chi"), sub("ass", "jpn"),
                            sub("dvd_subtitle", "eng"), sub("mov_text", "")])
        extra, paths = app.subtitle_sidecar_outputs(self.work / "Movie.mkv", media, [0, 1, 2, 3, 4])
        self.assertEqual([p.name for p in paths], ["Movie.chi.sup", "Movie.chi.srt", "Movie.jpn.ass", "Movie.und.srt"])
        self.assertEqual(" ".join(extra[:7]), f"-map 0:s:0 -c:s copy -f sup {self.work / 'Movie.chi.sup'}")
        self.assertIn(f"-map 0:s:1 -c:s srt -f srt {self.work / 'Movie.chi.srt'}", " ".join(extra))
        self.assertNotIn("0:a:4", text)
        self.assertNotIn("0:s:0", text)
        # 7.1 的原轨给 EAC3 降到 5.1；LPCM 直接复制
        args, count, _ = app.audio_sub_args(probe(audio_tracks=[audio("truehd", "eng", 8), audio("pcm_s24le", "eng")]),
                                            {"audio": [0, 1], "subs": []})
        text = " ".join(args)
        self.assertEqual(count, 3)
        self.assertIn("-ac:a:0 6", text)
        self.assertIn("-map 0:a:1 -c:a:2 copy -disposition:a:2 0", text)

    def test_dolby_vision_mp4_command_embeds_text_and_exports_graphic_subtitles(self):
        source = self.inbox / "movie.mkv"; source.write_bytes(b"x")
        media = probe("hevc", dv=8, audio_tracks=[audio("truehd", "eng", 8, "Atmos")],
                      subs=[sub("hdmv_pgs_subtitle", "chi"), sub("subrip", "chi", "简体"),
                            sub("hdmv_pgs_subtitle", "chi", "繁體")])
        out = self.work / "Movie.mp4"
        with patch.object(app, "run_ffmpeg_progress") as run:
            counts = app.ffmpeg_mp4_mux({"log": []}, "封装MP4(保留DV)", source, media, out,
                                        {"audio": [0], "subs": [0, 1, 2]}, 60)
        command = run.call_args.args[1]
        text = " ".join(command)
        self.assertEqual(counts, (2, 0, [self.work / "Movie.chi.sup", self.work / "Movie.chi.srt",
                                         self.work / "Movie.chi.2.sup"]))
        self.assertIn("-tag:v hvc1", text)
        self.assertIn("-c:a:0 eac3 -b:a:0 1536k -ac:a:0 6", text)
        self.assertIn("-c:a:1 copy -disposition:a:1 0", text)
        self.assertEqual(command[command.index("-strict") + 1], "experimental")     # MP4 里放 TrueHD
        main_end = command.index(str(out))
        self.assertNotIn("0:s:", " ".join(command[:main_end]))                      # 影片文件里不装字幕
        tail = " ".join(command[main_end + 1:])
        self.assertEqual(tail, f"-map 0:s:0 -c:s copy -f sup {self.work / 'Movie.chi.sup'} "
                               f"-map 0:s:1 -c:s srt -f srt {self.work / 'Movie.chi.srt'} "
                               f"-map 0:s:2 -c:s copy -f sup {self.work / 'Movie.chi.2.sup'}")
        with patch.object(app, "run_ffmpeg_progress") as run:
            with self.assertRaisesRegex(ValueError, "重建时间戳"):
                app.ffmpeg_mp4_mux({"log": []}, "x", source, media, out, None, 60,
                                   bl_video=self.work / "BL_P8.hevc")
            run.assert_not_called()

    # ---- 流水线 ----
    def test_p7_pipeline_converts_rebuilds_timestamps_then_muxes_and_moves_sidecars(self):
        source = self.inbox / "movie.mkv"; source.write_bytes(b"x")
        media = probe("hevc", dv=7, audio_tracks=[audio("truehd", "eng", 8, default=True)],
                      subs=[sub("hdmv_pgs_subtitle", "chi")])
        media["video"].update(avg_frame_rate="24/1", start_time=0)
        calls = []
        def convert(job, src, jobdir):
            bl = jobdir / "BL_P8.hevc"; bl.write_bytes(b"bl"); return bl
        def rebuild(job, bl, fps, jobdir):
            calls.append(("rebuild", bl.name, fps)); return jobdir / "BL_P8.mkv"
        def mux(job, phase, src, media_, out, selection, dur, bl_video=None):
            calls.append(("mux", bl_video.name, selection))
            out.write_bytes(b"mp4")
            sidecar = out.with_name(out.stem + ".chi.sup"); sidecar.write_bytes(b"PG")
            return 2, 0, [sidecar]
        job = {"log": []}
        with patch.object(app, "ffprobe", return_value={}), \
                patch.object(app, "summarize_probe", return_value=media), \
                patch.object(app, "dovi_convert", side_effect=convert), \
                patch.object(app, "rebuild_bl_timestamps", side_effect=rebuild), \
                patch.object(app, "ffmpeg_mp4_mux", side_effect=mux), \
                patch.object(app, "verify_dovi_p8") as p8, \
                patch.object(app, "verify_media_preserved") as preserved:
            output = app.process_item(job, {"name": source.name, "kind": "file", "payload": str(source)})
        self.assertEqual(calls, [("rebuild", "BL_P8.hevc", "24/1"),
                                 ("mux", "BL_P8.mkv", {"audio": [0], "subs": [0]})])
        p8.assert_called_once()
        self.assertEqual(preserved.call_args.kwargs, {"expected_audio": 2})
        self.assertEqual(output.name, "movie.mp4")
        self.assertEqual(job["sidecars"], [str(self.movies / "movie.chi.sup")])
        self.assertTrue((self.movies / "movie.chi.sup").is_file())
        self.assertEqual(job["verdict"], "must")

    def test_no_conversion_needed_means_plain_copy_and_as_is_cannot_bypass_must(self):
        source = self.inbox / "fine.mkv"; source.write_bytes(b"original bytes")
        with patch.object(app, "ffprobe", return_value={}), \
                patch.object(app, "summarize_probe", return_value=probe()), \
                patch.object(app, "disk_free", return_value=10**15), \
                patch.object(app, "run_ffmpeg_progress", side_effect=AssertionError("不应转码")):
            job = {"log": []}
            output = app.process_item(job, {"name": source.name, "kind": "file", "payload": str(source)})
        self.assertEqual(output.read_bytes(), b"original bytes")
        self.assertEqual(job["verdict"], "none")
        dv = self.inbox / "dv.mkv"; dv.write_bytes(b"x")
        with patch.object(app, "ffprobe", return_value={}), \
                patch.object(app, "summarize_probe", return_value=probe("hevc", dv=8)):
            with self.assertRaisesRegex(RuntimeError, "不能原样入库"):
                app.process_item({"log": []}, {"name": dv.name, "kind": "file",
                                               "payload": str(dv), "as_is": True})

    def test_queueing_carries_the_users_selection(self):
        source = self.inbox / "movie.mkv"; source.write_bytes(b"x")
        with patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=EVA):
            app.scan_once(); app.scan_once()
        shown = next(i for i in app.api_state()["items"] if i["name"] == source.name)
        self.assertEqual(shown["card"]["verdict"], "should")
        self.assertTrue(shown["optional"])
        with self.request({"names": [source.name], "selection": {source.name: {"audio": [3], "subs": []}}}):
            self.assertEqual(app.api_process()["queued"], [source.name])
        item = app.ITEMS[source.name]
        self.assertEqual(item["selection"], {"audio": [3], "subs": []})
        job = {"log": [], "warnings": []}
        self.assertEqual(app.resolve_selection(job, item, EVA), {"audio": [3], "subs": []})
        queued = next(i for i in app.api_state()["items"] if i["name"] == source.name)
        self.assertEqual(queued["card"]["selected"], {"audio": [3], "subs": []})
        with self.request({"name": source.name}):
            app.api_cancel()
        self.assertNotIn("selection", app.ITEMS[source.name])

    # ---- 多盘目录 ----
    def make_set(self):
        top = self.inbox / "[BDMV] Movie Set"
        for name in ("UHD", "Main Disc", "Special Disc"):
            (top / name / "BDMV" / "STREAM").mkdir(parents=True)
            (top / name / "BDMV" / "STREAM" / "00001.m2ts").write_bytes(name.encode() * 10)
        return top

    def test_each_disc_in_a_multi_disc_folder_becomes_its_own_item(self):
        top = self.make_set()
        app.scan_once(); app.scan_once()
        names = sorted(app.ITEMS)
        self.assertEqual(names, [f"{top.name}/Main Disc", f"{top.name}/Special Disc", f"{top.name}/UHD"])
        uhd = app.ITEMS[f"{top.name}/UHD"]
        self.assertEqual((uhd["kind"], uhd["status"], uhd["preview_status"]), ("bdmv", "ready", "queued"))
        self.assertEqual(Path(uhd["root"]), top / "UHD")
        self.assertEqual(app.item_source_root(uhd), top / "UHD")
        self.assertEqual(app.output_stem(uhd["name"], "bdmv"), "Movie Set - UHD")     # 方括号标签不带进成品名
        self.assertEqual(len(app.DISC_PREVIEW_QUEUE), 3)

    def test_deleting_one_disc_leaves_the_others(self):
        top = self.make_set()
        app.scan_once(); app.scan_once()
        name = f"{top.name}/UHD"
        self.movies.mkdir()
        output = self.movies / "out.mp4"; output.write_bytes(b"done")
        app.PROCESSED[name] = {"output": str(output), "source_snapshot": app.source_snapshot(top / "UHD")}
        app.ITEMS[name]["status"] = "done"
        with self.request({"name": name}):
            self.assertEqual(app.api_delete_source(), {"ok": True})
        self.assertFalse((top / "UHD").exists())
        self.assertTrue((top / "Main Disc" / "BDMV").is_dir())
        self.assertTrue((top / "Special Disc" / "BDMV").is_dir())

    def test_source_root_must_stay_inside_download(self):
        for bad in (self.inbox, self.work, self.work / "job_x", self.inbox.parent / "Movies", Path("/etc")):
            with self.assertRaises(ValueError, msg=str(bad)):
                app.item_source_root({"name": "x", "root": str(bad)})
        with self.assertRaises(ValueError):
            app.item_source_root({"name": "../Movies"})

    def test_disc_without_a_feature_is_marked_and_not_processable(self):
        top = self.make_set()
        app.scan_once(); app.scan_once()
        name = f"{top.name}/Special Disc"
        def fail(_src):
            raise ValueError("MakeMKV 没有报告可用的视频标题")
        task = next(t for t in app.DISC_PREVIEW_QUEUE if t[0] == name)
        app.DISC_PREVIEW_QUEUE[:] = [task]
        class Stop(Exception):
            pass
        sleeps = []
        def sleep(_seconds):
            sleeps.append(1)
            raise Stop()
        with patch.object(app, "makemkv_info", side_effect=fail), patch.object(app.time, "sleep", side_effect=sleep):
            with self.assertRaises(Stop):
                app.disc_preview_loop()
        item = app.ITEMS[name]
        self.assertEqual(item["preview_status"], "no_feature")
        self.assertEqual(item["preview_error"], app.NO_FEATURE_REASON)
        app.scan_once()                                  # 不会再反复排队预读
        self.assertEqual(app.ITEMS[name]["preview_status"], "no_feature")
        self.assertEqual(app.DISC_PREVIEW_QUEUE, [])
        shown = next(i for i in app.api_state()["items"] if i["name"] == name)
        self.assertFalse(shown["selectable"])
        with self.request({"names": [name]}):
            self.assertEqual(app.api_process()["queued"], [])

    def test_rebuild_removes_only_our_own_sidecars(self):
        source = self.inbox / "movie.mkv"; source.write_bytes(b"x")
        self.movies.mkdir()
        output = self.movies / "movie.mp4"; output.write_bytes(b"out")
        ours = self.movies / "movie.chi.sup"; ours.write_bytes(b"PG")
        stranger = self.movies / "other.chi.sup"; stranger.write_bytes(b"PG")
        unlisted = self.movies / "movie.eng.sup"; unlisted.write_bytes(b"PG")
        app.ITEMS[source.name] = {"name": source.name, "status": "done", "kind": "file", "root": str(source)}
        app.PROCESSED[source.name] = {"output": str(output), "sidecars": [str(ours), str(stranger)]}
        with self.request({"name": source.name}):
            result = app.api_rebuild()
        self.assertTrue(result["ok"])
        self.assertFalse(output.exists())
        self.assertFalse(ours.exists())
        self.assertTrue(stranger.exists())      # 登记了但不是以成品名开头的，不删
        self.assertTrue(unlisted.exists())      # 没登记的，不删

    def test_failed_disc_preview_says_why_and_can_be_retried(self):
        output = (b'MSG:1005,0,1,"MakeMKV v1.17.7 linux(x64-release) started","%1 started","x"\n'
                  b'MSG:5021,0,0,"This application version is too old.","fmt"\n', b"")
        container = types.SimpleNamespace(exec_run=lambda command, demux, environment=None: types.SimpleNamespace(exit_code=253, output=output))
        client = types.SimpleNamespace(containers=types.SimpleNamespace(get=lambda name: container), close=lambda: None)
        with patch.dict(sys.modules, {"docker": types.SimpleNamespace(from_env=lambda: client)}):
            with self.assertRaises(RuntimeError) as outdated:
                app.makemkv_info(self.inbox)
            self.assertEqual(str(outdated.exception), app.MAKEMKV_OUTDATED)       # 英文报错翻成大白话
            container.exec_run = lambda command, demux, environment=None: types.SimpleNamespace(exit_code=253, output=(b"", b""))
            with self.assertRaisesRegex(RuntimeError, "代码 253.*更新读盘工具"):
                app.makemkv_info(self.inbox)
            other = (b'MSG:5010,0,0,"Failed to open disc","fmt"\n', b"")
            container.exec_run = lambda command, demux, environment=None: types.SimpleNamespace(exit_code=1, output=other)
            with self.assertRaisesRegex(RuntimeError, "代码 1.*Failed to open disc"):
                app.makemkv_info(self.inbox)
            # 读盘工具自己崩溃（段错误）时，不把无关的英文提示贴给用户
            noise = (b'MSG:5050,0,2,"Evaluation version, 30 day(s) out of 30 remaining","fmt","30","30"\n', b"")
            container.exec_run = lambda command, demux, environment=None: types.SimpleNamespace(exit_code=139, output=noise)
            with self.assertRaises(RuntimeError) as crashed:
                app.makemkv_info(self.inbox)
            self.assertEqual(str(crashed.exception), app.MAKEMKV_CRASHED)
            seen = {}
            def capture(command, demux, environment=None):
                seen["environment"] = environment
                return types.SimpleNamespace(exit_code=0, output=(b"ok", b""))
            container.exec_run = capture
            app.makemkv_info(self.inbox)
            self.assertEqual(seen["environment"], {"HOME": "/config"})     # 用容器里已有的注册信息
        top = self.make_set()
        app.scan_once(); app.scan_once()
        name = f"{top.name}/UHD"
        app.ITEMS[name].update(preview_status="failed", preview_error="x")
        app.DISC_PREVIEW_QUEUE.clear(); app.DISC_PREVIEW_PENDING.clear()
        app.scan_once()
        self.assertEqual(app.DISC_PREVIEW_QUEUE, [])                # 失败后不会自己反复重试
        with self.request({"name": name}):
            self.assertEqual(app.api_repreview(), {"ok": True})
        app.scan_once()
        self.assertEqual([task[0] for task in app.DISC_PREVIEW_QUEUE], [name])
        self.assertEqual(app.ITEMS[name]["preview_status"], "queued")
        with self.request({"name": "nope"}):
            self.assertEqual(app.api_repreview()[1], 404)

    # ---- 未加密原盘：不经 MakeMKV 直接读 ----
    def plain_disc(self, name="Movie", aacs=False):
        disc = self.inbox / name
        for sub_dir in ("PLAYLIST", "STREAM"):
            (disc / "BDMV" / sub_dir).mkdir(parents=True)
        (disc / "BDMV" / "PLAYLIST" / "00001.mpls").write_bytes(b"main")
        (disc / "BDMV" / "PLAYLIST" / "00002.mpls").write_bytes(b"short")
        (disc / "BDMV" / "STREAM" / "00005.m2ts").write_bytes(b"x")
        (disc / "BDMV" / "STREAM" / "00006.m2ts").write_bytes(b"x" * 50)
        if aacs:
            (disc / "AACS").mkdir()
        return disc

    def identify_for(self, disc, tracks=None):
        stream = disc / "BDMV" / "STREAM"
        tracks = tracks or [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {}},
            {"id": 1, "type": "audio", "codec": "PCM", "properties": {"language": "jpn", "audio_channels": 6}},
            {"id": 2, "type": "audio", "codec": "DTS-HD Master Audio", "properties": {"language": "eng", "audio_channels": 8}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "chi"}},
        ]
        def identify(path):
            if Path(path).name == "00001.mpls":
                return {"container": {"recognized": True, "properties": {
                    "playlist_duration": 9302 * 10**9, "playlist_size": 81 * 10**9,
                    "playlist_file": [str(stream / "00005.m2ts"), str(stream / "00006.m2ts")]}}, "tracks": tracks}
            if Path(path).name == "00002.mpls":
                return {"container": {"recognized": True, "properties": {
                    "playlist_duration": 40 * 10**9, "playlist_size": 10, "playlist_file": []}}, "tracks": tracks[:1]}
            return None
        return identify

    def test_unencrypted_disc_folder_is_read_without_makemkv(self):
        disc = self.plain_disc()
        self.assertTrue(app.disc_is_plain_folder(disc))
        self.assertFalse(app.disc_is_plain_folder(self.plain_disc("Encrypted", aacs=True)))
        iso = self.inbox / "x.iso"; iso.write_bytes(b"i")
        self.assertFalse(app.disc_is_plain_folder(iso))
        clip = probe("hevc")
        with patch.object(app, "_mkvmerge_identify", side_effect=self.identify_for(disc)), \
                patch.object(app, "ffprobe", return_value={}) as ff, \
                patch.object(app, "summarize_probe", return_value=clip), \
                patch.object(app, "makemkv_info", side_effect=AssertionError("不应调用 MakeMKV")):
            info = app.read_disc_info(disc)
        self.assertEqual(Path(ff.call_args.args[0]).name, "00006.m2ts")        # 用最大的那个片段看画面规格
        preview = info["disc_preview"]
        self.assertEqual((preview["backend"], preview["title"], preview["similar_titles"]), ("direct", "00001.mpls", 1))
        self.assertEqual(info["duration"], 9302)
        self.assertEqual([(a["codec"], a["lang"], a["channels"], a["mkv_id"]) for a in info["audio"]],
                         [("pcm_bluray", "jpn", 6, 1), ("dts", "eng", 8, 2)])
        self.assertEqual(info["audio"][1]["profile"], "DTS-HD MA")
        self.assertEqual([(x["codec"], x["lang"], x["mkv_id"]) for x in info["subs"]], [("hdmv_pgs_subtitle", "chi", 3)])
        # 一遍出成品：占一份空间；需要再封装时占两份；读不了直读信息的盘按 MakeMKV 估
        item = {"name": "Movie", "kind": "bdmv", "size": 100, "probe": info, "payload": str(disc)}
        self.assertEqual(app.estimate_peak(item), 210)                                   # 带字幕：要第二遍做外挂
        self.assertEqual(app.estimate_peak({**item, "selection": {"audio": [0], "subs": []}}), 105)
        self.assertEqual(app.estimate_peak({**item, "selection": {"audio": [1], "subs": []}}), 210)
        self.assertEqual(app.estimate_peak({**item, "probe": None}), 260)

    def test_direct_read_falls_back_to_makemkv_when_it_cannot_apply(self):
        disc = self.plain_disc()
        two_layers = probe("hevc"); two_layers["video_count"] = 2
        with patch.object(app, "_mkvmerge_identify", side_effect=self.identify_for(disc)), \
                patch.object(app, "ffprobe", return_value={}), \
                patch.object(app, "summarize_probe", return_value=two_layers), \
                patch.object(app, "makemkv_info", return_value="robot") as makemkv, \
                patch.object(app, "parse_makemkv_info", return_value={"disc_preview": {}}), patch("builtins.print"):
            app.read_disc_info(disc)                         # 双层杜比视界 → MakeMKV
            makemkv.assert_called_once()
        with patch.object(app, "_mkvmerge_identify", return_value=None):
            with self.assertRaisesRegex(ValueError, app.NO_FEATURE_MARK):
                app.direct_disc_info(disc)                   # 没有能用的正片

    def test_direct_rip_command_selects_tracks(self):
        disc = self.plain_disc()
        with patch.object(app, "_mkvmerge_identify", side_effect=self.identify_for(disc)), \
                patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=probe("hevc")):
            info = app.direct_disc_info(disc)
        seen = {}
        class FakePopen:
            returncode = 0
            def __init__(self, cmd, **_kwargs): seen["cmd"] = cmd
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        ok = probe("hevc"); ok["duration"] = 9302.5
        with patch.object(app.subprocess, "Popen", FakePopen), \
                patch.object(app, "read_process_progress", return_value=""), patch.object(app, "stop_local_process"), \
                patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=ok):
            job = {"log": []}
            app.direct_disc_rip(job, info, self.work / "out.mkv", {"audio": [1, 0], "subs": [0]})
            self.assertEqual(seen["cmd"], [
                "mkvmerge", "--gui-mode", "-o", str(self.work / "out.mkv"),
                "--audio-tracks", "2,1", "--subtitle-tracks", "3",
                "--default-track-flag", "2:yes", "--default-track-flag", "1:no", "--default-track-flag", "3:yes",
                str(disc / "BDMV" / "PLAYLIST" / "00001.mpls")])
            app.direct_disc_rip(job, info, self.work / "out.mkv", {"audio": [0], "subs": []})
            self.assertIn("--no-subtitles", seen["cmd"])
            app.direct_disc_rip(job, info, self.work / "out.mkv")          # 不给选择：全部轨道
            self.assertNotIn("--audio-tracks", seen["cmd"])
            short = probe("hevc"); short["duration"] = 600
            with patch.object(app, "summarize_probe", return_value=short):
                with self.assertRaisesRegex(RuntimeError, "时长"):
                    app.direct_disc_rip(job, info, self.work / "out.mkv")

    def test_plain_disc_with_playable_audio_is_done_in_a_single_pass(self):
        disc = self.plain_disc()
        result = probe("hevc", audio_tracks=[audio("pcm_s24le", "jpn")])
        def rip(job, info, out, selection=None):
            self.assertEqual(selection, {"audio": [0], "subs": []})
            out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(b"mkv"); return out
        job = {"log": []}
        item = {"name": "Movie", "kind": "bdmv", "payload": str(disc), "root": str(disc),
                "selection": {"audio": [0], "subs": []}}                  # 不带字幕才能一遍完成
        with patch.object(app, "_mkvmerge_identify", side_effect=self.identify_for(disc)), \
                patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=result), \
                patch.object(app, "direct_disc_rip", side_effect=rip), \
                patch.object(app, "makemkv_rip", side_effect=AssertionError("不应调用 MakeMKV")), \
                patch.object(app, "run_ffmpeg_progress", side_effect=AssertionError("不应再封装一遍")):
            output = app.process_item(job, item)
        self.assertEqual(output, self.movies / "Movie.mkv")
        self.assertEqual(job["steps"], ["取出原盘正片", "成品自检", "移入 Movies"])
        self.assertEqual(job["verdict"], "must")
        self.assertTrue(disc.exists())
        # 选了电视解不了的那条（DTS-HD）：不能一遍完成，要先取出再加一条电视能播的
        calls = []
        def rip_all(job, info, out, selection=None):
            calls.append(selection); out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(b"mkv"); return out
        ripped = probe("hevc", audio_tracks=[audio("pcm_s24le", "jpn"), audio("dts", "eng", 8)], subs=[sub("hdmv_pgs_subtitle", "chi")])
        with patch.object(app, "_mkvmerge_identify", side_effect=self.identify_for(disc)), \
                patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=ripped), \
                patch.object(app, "direct_disc_rip", side_effect=rip_all), \
                patch.object(app, "run_ffmpeg_progress", side_effect=lambda job, cmd, dur, phase: [
                    Path(a).write_bytes(b"m") for a in cmd if a.endswith((".mkv", ".sup")) and "dvwork" in a and "/rip/" not in a]) as mux, \
                patch.object(app, "verify_media_preserved") as verify:
            job = {"log": []}
            app.process_item(job, {**item, "selection": {"audio": [1], "subs": [0]}})
        self.assertEqual([Path(x).name for x in job["sidecars"]], ["Movie.v1.chi.sup"])   # 字幕成了外挂文件，跟着成品改名
        self.assertEqual(calls, [None])                    # 第一遍取全部轨道
        mux.assert_called_once()
        self.assertEqual(verify.call_args.kwargs, {"expected_audio": 2})
        self.assertEqual(job.get("warnings", []), [])      # 预读和取出的轨道对得上，不该有“对不上”的警告

    def test_space_estimate_matches_what_the_pipeline_really_writes(self):
        gb = 1024**3
        def peak(kind, media, name, size_gb, **extra):
            return app.estimate_peak({"kind": kind, "size": size_gb * gb, "probe": media, "payload": name, **extra}) / gb
        self.assertAlmostEqual(peak("file", EVA, "m.mkv", 25.6), 28.16, places=1)          # 以前按 1.7 倍估成 43.5GB
        self.assertAlmostEqual(peak("file", probe(), "m.mkv", 10), 10.2, places=1)          # 不用转换：原样复制
        self.assertAlmostEqual(peak("file", EVA, "m.mkv", 10, as_is=True), 10.2, places=1)
        self.assertAlmostEqual(peak("file", probe("hevc", dv=7), "m.mkv", 10), 22, places=1)  # P7 多一份中间文件
        self.assertAlmostEqual(peak("file", None, "m.mkv", 10), 11, places=1)               # 还没探测出来
        self.assertAlmostEqual(peak("iso", probe(), "m.iso", 10), 26, places=1)             # 经 MakeMKV
        # NAS 上实际出现过的情况：剩 90.8GB，25.6GB 的片子应该能转
        item = {"kind": "file", "size": int(25.6 * gb), "probe": EVA, "payload": "m.mkv"}
        with patch.object(app, "disk_free", return_value=int(90.8 * gb)):
            self.assertEqual(app.precheck_space(item), (True, ""))
        with patch.object(app, "disk_free", return_value=int(60 * gb)):
            ok, reason = app.precheck_space(item)
            self.assertFalse(ok)
            self.assertIn("空间不够", reason)
            self.assertIn("腾出", reason)

    # ---- 一键更新读盘工具 ----
    def fake_docker(self, new_image_id="sha-new", start_status="running", run_error=None):
        log = []
        old_image = types.SimpleNamespace(id="sha-old", attrs={"Config": {"Env": ["PATH=/bin", "APP_VERSION=1.18.4"]}})
        class Old:
            image = old_image
            attrs = {"Config": {"Image": "jlesage/makemkv", "Env": ["PATH=/bin", "APP_VERSION=1.18.4", "MAKEMKV_KEY=BETA", "HOSTNAME=x"]},
                     "HostConfig": {"Binds": ["/volume2/docker/makemkv:/config:rw", "/volume2/Share:/share:rw"],
                                    "RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "bridge",
                                    "PortBindings": {"5800/tcp": [{"HostIp": "", "HostPort": "5800"}]}}}
            def reload(self): pass
            def stop(self, timeout): log.append("stop old")
            def rename(self, name): log.append("rename " + name.split("-old-")[0] + ("-old" if "-old-" in name else ""))
            def start(self): log.append("start old")
            def remove(self, force): log.append("remove old")
        class New:
            status = start_status
            def reload(self): pass
            def remove(self, force): log.append("remove new")
        def run(image, **kwargs):
            log.append(("run", image, kwargs))
            if run_error:
                raise RuntimeError(run_error)
            return New()
        client = types.SimpleNamespace(
            containers=types.SimpleNamespace(get=lambda name: Old(), run=run),
            images=types.SimpleNamespace(pull=lambda repo, tag: (log.append(f"pull {repo}:{tag}"),
                                                                 types.SimpleNamespace(id=new_image_id))[1]),
            close=lambda: None)
        return log, types.SimpleNamespace(from_env=lambda: client)

    def run_update(self, **kwargs):
        log, docker = self.fake_docker(**kwargs)
        with patch.dict(sys.modules, {"docker": docker}), patch.object(app.time, "sleep"), \
                patch.object(app, "MAKEMKV_UPDATE", {"status": "idle", "message": ""}), patch("builtins.print"):
            app.ITEMS["disc"] = {"name": "disc", "kind": "bdmv", "preview_status": "failed", "preview_error": "x"}
            app.update_makemkv_container()
            return log, dict(app.MAKEMKV_UPDATE), dict(app.ITEMS["disc"])

    def test_makemkv_update_recreates_container_with_the_same_settings(self):
        log, status, disc = self.run_update()
        self.assertEqual(status["status"], "done")
        self.assertEqual([entry for entry in log if isinstance(entry, str)],
                         ["pull jlesage/makemkv:latest", "stop old", "rename makemkv-old", "remove old"])
        _run, image, kwargs = next(entry for entry in log if isinstance(entry, tuple))
        self.assertEqual(image, "sha-new")
        self.assertEqual(kwargs["name"], "makemkv")
        self.assertEqual(kwargs["environment"], ["MAKEMKV_KEY=BETA"])      # 不把旧镜像自带的版本号带过去
        self.assertEqual(kwargs["volumes"], ["/volume2/docker/makemkv:/config:rw", "/volume2/Share:/share:rw"])
        self.assertEqual(kwargs["restart_policy"], {"Name": "unless-stopped"})
        self.assertEqual(kwargs["ports"], {"5800/tcp": [("0.0.0.0", 5800)]})
        self.assertIsNone(disc["preview_status"])                          # 失败的盘会自动重读

    def test_makemkv_update_rolls_back_when_the_new_container_fails(self):
        log, status, disc = self.run_update(run_error="boom")
        self.assertEqual(status["status"], "failed")
        self.assertIn("已恢复原样", status["message"])
        self.assertEqual([entry for entry in log if isinstance(entry, str)][-2:], ["rename makemkv", "start old"])
        self.assertEqual(disc["preview_status"], "failed")
        log, status, _ = self.run_update(start_status="exited")
        self.assertEqual(status["status"], "failed")
        self.assertIn("remove new", log)
        self.assertEqual([entry for entry in log if isinstance(entry, str)][-2:], ["rename makemkv", "start old"])

    def test_makemkv_update_stops_if_no_newer_version_exists(self):
        log, status, _ = self.run_update(new_image_id="sha-old")
        self.assertEqual(status["status"], "failed")
        self.assertIn("等官方发布新版", status["message"])
        self.assertNotIn("stop old", log)

    def test_makemkv_update_only_runs_when_idle_and_pauses_processing(self):
        fake = types.SimpleNamespace(remote_addr="192.168.1.22", get_json=lambda **kw: {})
        app.JOB_QUEUE.append("x")
        with patch.object(app, "request", fake), patch.object(app.threading, "Thread") as thread:
            self.assertEqual(app.api_makemkv_update()[1], 409)
            thread.assert_not_called()
            app.JOB_QUEUE.clear()
            with patch.object(app, "MAKEMKV_UPDATE", {"status": "idle", "message": ""}):
                self.assertEqual(app.api_makemkv_update()[1], 202)
                thread.assert_called_once()
                self.assertIn("更新读盘工具", app.processing_block_reason())   # 更新期间不开始新任务
        with patch.object(app, "request", types.SimpleNamespace(remote_addr="8.8.8.8", get_json=lambda **kw: {})):
            self.assertEqual(app.api_makemkv_update()[1], 403)

    def test_slow_deletes_do_not_hold_the_global_lock(self):
        source = self.inbox / "movie.mkv"; source.write_bytes(b"x")
        self.movies.mkdir()
        output = self.movies / "movie.mkv"; output.write_bytes(b"out")
        app.ITEMS[source.name] = {"name": source.name, "status": "done", "kind": "file", "root": str(source)}
        app.PROCESSED[source.name] = {"output": str(output), "source_snapshot": app.source_snapshot(source)}
        free = []
        def probe_lock(*_args, **_kwargs):
            import threading
            got = []
            worker = threading.Thread(target=lambda: got.append(app.LOCK.acquire(timeout=2) and (app.LOCK.release() or True)))
            worker.start(); worker.join()
            free.append(bool(got and got[0]))
        real_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            probe_lock()
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            with self.request({"name": source.name}):
                self.assertTrue(app.api_rebuild()["ok"])
            output.write_bytes(b"out again")
            app.ITEMS[source.name]["status"] = "done"
            app.PROCESSED[source.name] = {"output": str(output), "source_snapshot": app.source_snapshot(source)}
            with self.request({"name": source.name}):
                self.assertEqual(app.api_delete_source(), {"ok": True})
        self.assertEqual(free, [True, True])        # 两次删除期间，其他线程都拿得到锁
        self.assertFalse(source.exists())
        # 锁必须原样还回去
        self.assertTrue(app.LOCK.acquire(timeout=1)); app.LOCK.release()

    def test_unwanted_disc_can_be_discarded_from_the_panel(self):
        # 多盘目录里不要的那张（花絮盘）：直接在面板删，只删这一张，最后一张删完收掉空的外层目录
        outer = self.inbox / "Set"
        extras = outer / "Special Disc"; (extras / "BDMV").mkdir(parents=True); (extras / "BDMV" / "a.m2ts").write_bytes(b"x")
        main = outer / "UHD"; (main / "BDMV").mkdir(parents=True)
        (outer / ".DS_Store").write_bytes(b"j")
        app.ITEMS["Set/Special Disc"] = {"name": "Set/Special Disc", "status": "ready", "kind": "bdmv",
                                         "root": str(extras), "preview_status": "no_feature"}
        app.ITEMS["Set/UHD"] = {"name": "Set/UHD", "status": "ready", "kind": "bdmv", "root": str(main),
                                "preview_status": "ready"}
        with self.request({"name": "Set/Special Disc"}):
            self.assertEqual(app.api_discard_source(), {"ok": True})
        self.assertFalse(extras.exists())
        self.assertTrue(main.exists())                 # 另一张盘不受影响
        self.assertNotIn("Set/Special Disc", app.ITEMS)
        with self.request({"name": "Set/UHD"}):
            self.assertEqual(app.api_discard_source(), {"ok": True})
        self.assertFalse(outer.exists())               # 都删完后外层空目录也收掉
        self.assertTrue(self.inbox.exists())

    def test_discard_refuses_anything_in_use_or_already_in_the_library(self):
        source = self.inbox / "movie.mkv"; source.write_bytes(b"x")
        for status, preview in (("processing", None), ("queued", None), ("done", None), ("growing", None),
                                ("ready", "analyzing")):
            app.ITEMS[source.name] = {"name": source.name, "status": status, "kind": "file",
                                      "root": str(source), "preview_status": preview}
            with self.request({"name": source.name}):
                self.assertEqual(app.api_discard_source()[1], 409, status)
            self.assertTrue(source.exists())
        # 源路径跑到 download 之外的，拒绝
        outside = self.inbox.parent / "elsewhere.mkv"; outside.write_bytes(b"x")
        app.ITEMS["x"] = {"name": "x", "status": "ready", "kind": "file", "root": str(outside)}
        with self.request({"name": "x"}):
            self.assertEqual(app.api_discard_source()[1], 409)
        self.assertTrue(outside.exists())

    def test_item_being_discarded_cannot_be_queued(self):
        source = self.inbox / "movie.mkv"; source.write_bytes(b"x")
        app.ITEMS[source.name] = {"name": source.name, "status": "ready", "kind": "file", "root": str(source),
                                  "discarding": True, "size": 1, "probe": None, "payload": str(source)}
        with self.request({"names": [source.name]}):
            self.assertEqual(app.api_process()["queued"], [])

    def test_progress_lives_in_the_item_card_and_busy_rows_offer_queueing(self):
        page = app.PAGE
        self.assertNotIn("进度见页面上方", page)
        self.assertIn("data-jobslot", page)
        self.assertIn("slot.appendChild($('#jobcard'))", page)
        self.assertIn("排队转换", page)
        self.assertIn("/api/discard_source", page)
        self.assertIn("删除源文件", page)

    def test_dropping_a_long_subtitle_does_not_fail_the_duration_check(self):
        # NAS 实测（The End of Evangelion）：源里有条轨道比画面长 2 秒，只留一条音轨和字幕后
        # 文件总时长短了 2.002 秒，旧规则按总时长比，把好的成品判成失败并删掉了
        import copy
        source = copy.deepcopy(probe(audio_tracks=[audio("eac3", default=True)]))
        source["duration"] = 5210.247; source["video"]["duration"] = 5208.245
        result = copy.deepcopy(source); result["duration"] = 5208.245
        with patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=result):
            app.verify_media_preserved(Path("out.mkv"), source, 0, expected_audio=1)
            # 画面真的少了一截，仍然要拦住
            result["video"]["duration"] = 5100.0; result["duration"] = 5100.0
            with self.assertRaisesRegex(RuntimeError, "画面时长偏差"):
                app.verify_media_preserved(Path("out.mkv"), source, 0, expected_audio=1)
            # 读不到视频轨时长时退回总时长：只查成品是否明显变短
            source["video"]["duration"] = 0; result["video"]["duration"] = 0; result["duration"] = 5208.245
            app.verify_media_preserved(Path("out.mkv"), source, 0, expected_audio=1)
            result["duration"] = 5100.0
            with self.assertRaisesRegex(RuntimeError, "成品比源短"):
                app.verify_media_preserved(Path("out.mkv"), source, 0, expected_audio=1)

    def test_track_duration_is_read_from_mkv_tags_and_mp4_fields(self):
        self.assertAlmostEqual(app.stream_duration({"tags": {"DURATION": "01:26:48.245000000"}}), 5208.245)
        self.assertAlmostEqual(app.stream_duration({"tags": {"DURATION-eng": "00:00:10.5"}}), 10.5)
        self.assertEqual(app.stream_duration({"duration": "20.02"}), 20.02)
        self.assertEqual(app.stream_duration({}), 0.0)

    # ---- 一键找中文字幕 ----
    def fake_jellyfin(self, subtitles, plugins=(), movies=None, key="k" * 32):
        """起一个本机假 Jellyfin，走真实的 HTTP 请求。返回 (地址, 收到的请求列表)。"""
        import json as js, threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = []
        movies = movies if movies is not None else [{"Id": "abc", "Path": "/media/Movies/movie.mkv"}]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a): pass
            def reply(self, code, body=None):
                raw = js.dumps(body).encode() if body is not None else b""
                self.send_response(code); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                self.wfile.write(raw)
            def handle_any(self):
                seen.append((self.command, self.path))
                if self.headers.get("Authorization") != 'MediaBrowser Token="%s"' % key:
                    return self.reply(401)
                path = self.path.split("?")[0]
                if path == "/Items": return self.reply(200, {"Items": movies})
                if path == "/Plugins": return self.reply(200, [{"Name": n} for n in plugins])
                if self.command == "GET" and path == "/Items/abc/RemoteSearch/Subtitles/chi": return self.reply(200, subtitles)
                if self.command == "GET" and path.startswith("/Items/abc/RemoteSearch/Subtitles/"): return self.reply(200, [])
                if self.command == "POST" and path.startswith("/Items/abc/RemoteSearch/Subtitles/"): return self.reply(204)
                if self.command == "DELETE" and path.startswith("/Videos/abc/Subtitles/"): return self.reply(204)
                if self.command == "POST" and path == "/Items/abc/Refresh": return self.reply(204)
                return self.reply(404)
            do_GET = do_POST = do_DELETE = handle_any
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_port, seen

    def done_movie(self):
        self.movies.mkdir(exist_ok=True)
        output = self.movies / "movie.mkv"; output.write_bytes(b"out")
        app.ITEMS["movie.mkv"] = {"name": "movie.mkv", "status": "done", "kind": "file"}
        app.PROCESSED["movie.mkv"] = {"output": str(output)}
        return output

    def auto(self, base, body):
        req = types.SimpleNamespace(get_json=lambda **kw: body, host="nas.local:8097")
        with patch.object(app, "request", req), patch.dict(app.os.environ, {"JELLYFIN_URL": base}):
            return app.api_subtitle_auto()

    def test_one_click_subtitle_asks_jellyfin_to_download_the_best_chinese_one(self):
        self.done_movie()
        base, seen = self.fake_jellyfin([
            {"Id": "os_1", "Name": "Movie.2020.ENG-forced", "Forced": True, "DownloadCount": 99999},
            {"Id": "os_2", "Name": "Movie.cht", "Format": "srt", "DownloadCount": 5000},
            {"Id": "os/3", "Name": "Movie.简体.chs", "Format": "srt", "DownloadCount": 300},
            {"Id": "os_4", "Name": "Movie.chs.机翻", "Format": "srt", "DownloadCount": 300, "MachineTranslated": True},
        ])
        # 没有密钥：先要密钥，不发任何请求
        first = self.auto(base, {"name": "movie.mkv"})
        self.assertEqual(first[1], 428); self.assertTrue(first[0]["needs_key"]); self.assertEqual(seen, [])
        # 填了密钥：保存到 NAS，并下载简体那份（编号里的斜杠要转义）
        done = self.auto(base, {"name": "movie.mkv", "api_key": "k" * 32})
        self.assertTrue(done["ok"]); self.assertIn("Movie.简体.chs", done["message"])
        self.assertIn(("POST", "/Items/abc/RemoteSearch/Subtitles/os%2F3"), seen)
        self.assertEqual(app.jellyfin_api_key(), "k" * 32)
        self.assertNotIn("k" * 32, str(done))                    # 密钥不回显
        # 之后不用再填
        self.assertTrue(self.auto(base, {"name": "movie.mkv"})["ok"])

    def test_one_click_subtitle_explains_what_is_missing(self):
        self.done_movie()
        app.save_jellyfin_api_key("k" * 32)
        # 没搜到，且 Jellyfin 没装字幕插件
        base, _ = self.fake_jellyfin([], plugins=["Trakt"])
        res = self.auto(base, {"name": "movie.mkv"})
        self.assertEqual(res[1], 502); self.assertIn("还没装字幕下载插件", res[0]["error"])
        # 装了插件但没搜到
        base, _ = self.fake_jellyfin([], plugins=["Open Subtitles"],
                                     movies=[{"Id": "abc", "Path": "/media/Movies/movie.mkv", "ProviderIds": {"Imdb": "tt1"}}])
        self.assertIn("未找到", self.auto(base, {"name": "movie.mkv"})[0]["error"])
        # Jellyfin 里还没扫到这部片
        base, _ = self.fake_jellyfin([{"Id": "x"}], movies=[{"Id": "abc", "Path": "/media/Movies/other.mkv"}])
        self.assertIn("刷新一次媒体库", self.auto(base, {"name": "movie.mkv"})[0]["error"])
        # 密钥不对：提示重新填
        base, _ = self.fake_jellyfin([{"Id": "x"}], key="z" * 32)
        res = self.auto(base, {"name": "movie.mkv"})
        self.assertTrue(res[0]["needs_key"]); self.assertIn("不认这个密钥", res[0]["error"])
        # Jellyfin 没开
        self.assertIn("连不上", self.auto("http://127.0.0.1:9", {"name": "movie.mkv"})[0]["error"])
        # 还没入库的片不能找
        app.ITEMS["movie.mkv"]["status"] = "ready"
        self.assertEqual(self.auto(base, {"name": "movie.mkv"})[1], 409)

    def test_jellyfin_address_defaults_to_the_host_the_panel_was_opened_with(self):
        with patch.dict(app.os.environ, {"JELLYFIN_URL": ""}):
            self.assertEqual(app.jellyfin_base_url("192.168.1.10:8097"), "http://192.168.1.10:8096")
            self.assertEqual(app.jellyfin_base_url("nas.local"), "http://nas.local:8096")
        with self.assertRaises(ValueError):
            app.save_jellyfin_api_key("not a key!")

    # ---- 转换规则可以在页面上改 ----
    def test_default_rules_describe_actual_container_outputs(self):
        view = app.rules_view()
        self.assertFalse(view["customized"])
        rules = [r for r in view["rows"] if not r.get("note_only")]
        self.assertEqual({r["id"]: r["action"] for r in rules}, app.RULE_DEFAULTS)
        # 备注默认就是“为什么这么设”，每条都有
        self.assertTrue(all(r["note"] == r["reason"] and r["note"] and r["label"] and len(r["options"]) >= 2
                            for r in rules))
        self.assertEqual([r["label"] for r in view["rows"] if r["group"] == "声音"][:5],
                         ["DTS", "DTS-HD", "Dolby TrueHD", "MLP", "FLAC"])
        self.assertEqual(view["groups"], ["画面", "声音", "字幕", "默认保留"])          # 画面在前
        self.assertEqual(view["rows"][0]["group"], "画面")
        self.assertEqual(len(view["fixed"]), 3)
        # 选项要写清楚处理成什么
        dts = next(r for r in view["rows"] if r["id"] == "audio.dts")
        self.assertEqual([o["outputs"]["mkv"] for o in dts["options"]],
                         ["和片源相同", "E-AC-3 + 原音轨", "E-AC-3", "AC-3 + 原音轨", "AC-3"])
        self.assertEqual(dts["options"][3]["outputs"]["mp4"], "AC-3 + 原音轨")
        self.assertEqual(app.rule_outputs("audio.pcm", "pair_ac3"), {"mkv": "AC-3 + 原音轨", "mp4": "AC-3"})
        # 处理一栏的说法统一：不转换 / 转换：……
        self.assertTrue(all(o["label"] == "不转换" or o["label"].startswith(("转换：", "不转换：")) for r in rules
                            if r["group"] in ("声音", "字幕") for o in r["options"]))
        app.save_rules({"audio.truehd": {"action": "pair_ac3"}})
        self.assertEqual(app.rule("audio.mlp"), "pair_ac3")
        self.assertEqual(app.audio_action("mlp", "mp4"), "replace")
        self.assertEqual(app.rule_outputs("audio.mlp", "pair_ac3"), {"mkv": "AC-3 + 原音轨", "mp4": "AC-3"})
        self.assertEqual(app.rule_outputs("subs.graphic", "embed"), {"mkv": "内嵌", "mp4": "外挂 .sup"})
        # 写给人看：不出现命令行参数和工具名
        everything = " ".join(o["label"] for r in view["rows"] for o in r["options"]) + " ".join(f["action"] for f in view["fixed"])
        for jargon in ("-c:", "copy", "dovi_tool", "hvc1", "mov_text", "直通"):
            self.assertNotIn(jargon, everything)

    def test_changing_a_rule_changes_what_gets_converted(self):
        self.assertEqual(app.make_plan("file", EVA, "m.mkv", {"audio": [0], "subs": []})["needs"], "should")
        # 别人的电视能播 DTS-HD：这条声音就不用动了；普通 DTS 是另一行，不受影响
        app.save_rules({"audio.dtshd": {"action": "keep", "note": "我的电视能播"}})
        self.assertEqual(app.make_plan("file", EVA, "m.mkv", {"audio": [0], "subs": []})["should"], ["selection"])
        self.assertEqual(app.make_plan("file", EVA, "m.mkv", {"audio": [1], "subs": []})["needs"], "should")
        view = app.rules_view()
        self.assertTrue(view["customized"])
        self.assertEqual(next(r["note"] for r in view["rows"] if r["id"] == "audio.dtshd"), "我的电视能播")
        # 只留电视能播的，不留原来的
        app.save_rules({"audio.dtshd": {"action": "replace"}})
        plan = app.make_plan("file", EVA, "m.mkv", {"audio": [0], "subs": []})
        self.assertEqual(plan["audio"], [{"src": 0, "compat": True, "keep": False, "lossy": True}])
        self.assertEqual(app.audio_sub_args(EVA, {"audio": [0], "subs": []})[1], 1)
        # 改成做 AC-3：编码器和码率跟着变，原轨照样保留
        app.save_rules({"audio.dtshd": {"action": "pair_ac3"}})
        args, count, _ = app.audio_sub_args(EVA, {"audio": [0], "subs": []})
        self.assertEqual(count, 2)
        self.assertIn("-c:a:0 ac3 -b:a:0 640k", " ".join(args))
        app.save_rules({"audio.dtshd": {"action": "replace_ac3"}})
        self.assertEqual(app.audio_sub_args(EVA, {"audio": [0], "subs": []})[1], 1)
        app.save_rules(None)
        args, count, _ = app.audio_sub_args(EVA, {"audio": [0], "subs": []})
        self.assertEqual(count, 2)
        self.assertIn("-c:a:0 eac3 -b:a:0 1536k", " ".join(args))

    def test_subtitle_and_picture_rules(self):
        with_text = probe(subs=[sub("subrip", "chi"), sub("subrip", "eng")])
        # 字幕留在影片文件里：不再因为字幕建议转换，封装时内嵌、不出外挂
        app.save_rules({"subs.text": {"action": "embed"}})
        self.assertEqual(app.make_plan("file", with_text, "m.mkv")["should"], ["selection"])
        args, _, sub_count = app.audio_sub_args(with_text)
        self.assertEqual(sub_count, 1)
        self.assertIn("-map 0:s:0 -c:s:0 copy", " ".join(args))
        self.assertEqual(app.subtitle_sidecar_outputs(self.work / "m.mkv", with_text, [0]), ([], []))
        # 默认带英语字幕 / 不带字幕
        app.save_rules({"pick.subs": {"action": "eng"}})
        self.assertEqual(app.default_selection(with_text)["subs"], [1])
        app.save_rules({"pick.subs": {"action": "none"}})
        self.assertEqual(app.default_selection(with_text)["subs"], [])
        app.save_rules(None)
        self.assertEqual(app.default_selection(with_text)["subs"], [0])
        # 杜比视界保持 MKV；双层的不转
        dv8, dv7 = probe("hevc", dv=8), probe("hevc", dv=7)
        self.assertEqual(app.make_plan("file", dv8, "m.mkv")["container"], "mp4")
        app.save_rules({"video.dv": {"action": "keep"}})
        self.assertEqual((app.make_plan("file", dv8, "m.mkv")["container"], app.make_plan("file", dv8, "m.mkv")["needs"]),
                         ("mkv", "none"))
        self.assertEqual(app.make_plan("file", dv7, "m.mkv")["container"], "mp4")     # 要转单层就只能进 MP4
        app.save_rules({"video.dv": {"action": "keep"}, "video.dv7": {"action": "keep"}})
        self.assertEqual((app.make_plan("file", dv7, "m.mkv")["container"], app.make_plan("file", dv7, "m.mkv")["needs"]),
                         ("mkv", "none"))

    def test_partial_settings_persist_and_refresh_pending_media(self):
        media = probe(audio_tracks=[audio("ac3", "jpn"), audio("aac", "eng", default=True),
                                    audio("truehd", "eng")])
        item = {"status": "ready", "kind": "file", "payload": "test.mp4", "probe": probe("hevc", dv=8)}
        app.ITEMS["test"] = item
        self.assertEqual(app.default_selection(media)["audio"], [2])
        for key, entry in (("pick.audio", {"action": "first"}), ("audio.aac", {"action": "pair_ac3"}),
                           ("audio.aac", {"note": "家庭影院"}), ("video.dv", {"action": "keep"})):
            with self.request({"rules": {key: entry}}):
                self.assertTrue(app.api_rules_save()["ok"])
        self.assertEqual(app.default_selection(media)["audio"], [0])
        self.assertEqual(app.rule("audio.aac"), "pair_ac3")
        self.assertFalse(item["clean"])
        self.assertTrue(item["optional"])
        self.assertIn("转封装为MKV", item["plan"])
        with self.request({"rules": {"pick.audio": {"action": "default"}}}):
            result = app.api_rules_save()
        self.assertEqual(app.default_selection(media)["audio"], [1])
        row = next(r for r in result["rows"] if r["id"] == "audio.aac")
        self.assertEqual((row["action"], row["note"]), ("pair_ac3", "家庭影院"))
        for bad in ({"folder.foo": {"action": "x"}}, {"folder.library": {}}, {"audio.aac": {"action": []}}):
            with self.request({"rules": bad}):
                self.assertEqual(app.api_rules_save()[1], 400)
        app.save_rules({"audio.aac": {"action": "keep", "note": "LG G5 能直接播放。"}})
        # 旧版本存下的系统说明不当成用户写的备注：显示现在的默认原因
        self.assertEqual(next(r["note"] for r in app.rules_view()["rows"] if r["id"] == "audio.aac"),
                         app.RULE_REASONS["audio.aac"])

    def test_p7_preview_and_work_plan_follow_both_video_rules(self):
        media = probe("hevc", dv=7)
        for container in ("mp4", "keep"):
            for profile in ("convert", "keep"):
                app.save_rules({"video.dv": {"action": container}, "video.dv7": {"action": profile}})
                view = app.rules_view()
                plan = app.make_plan("file", media, "source.mp4")
                # 表格行：片源 / 文件格式 / 转不转 / 成品格式 / 成品画面。这里核对“P7 + MP4 片源”那一行
                row = next(r for r in view["video_outputs"][container][profile]
                           if r[0].startswith("双层") and r[1] == "MP4")
                self.assertEqual(row[3], plan["container"].upper())
                self.assertEqual("8.1" in row[4], "p7" in plan["must"])
                self.assertEqual(row[2] == "转换", plan["needs"] != "none")
                preview = app.format_preview("file", media, "source.mp4")
                self.assertEqual("P8.1" in preview["target"], profile == "convert")
                self.assertEqual("移除增强层" in preview["target_detail"], profile == "convert")
        self.assertIn("container", plan["should"])

    def test_rules_api_validates_and_refuses_while_converting(self):
        with self.request({"rules": {"audio.dts": {"action": "keep", "note": "x" * 999}}}):
            saved = app.api_rules_save()
        self.assertTrue(saved["ok"])
        self.assertEqual(len(next(r["note"] for r in saved["rows"] if r["id"] == "audio.dts")), 300)
        for bad in ({"rules": {"audio.dts": {"action": "explode"}}}, {"rules": {"nope": {"action": "keep"}}},
                    {"rules": "x"}):
            with self.request(bad):
                self.assertEqual(app.api_rules_save()[1], 400)
        self.assertEqual(app.rule("audio.dts"), "keep")                  # 失败的保存不改动已有规则
        with patch.object(app, "JOB_QUEUE", ["x"]), self.request({"reset": True}):
            self.assertEqual(app.api_rules_save()[1], 409)
        with self.request({"reset": True}):
            self.assertFalse(app.api_rules_save()["customized"])
        (self.work / app.RULES_FILE).write_text("{broken", "utf-8")       # 文件坏了就用默认
        self.assertEqual(app.rule("audio.dts"), "pair")
        for marker in ('id="navRules"', 'id="rulesNav"', 'id="rulesPage"', "location.hash==='#rules'", "/api/rules", "data-note="):
            self.assertIn(marker, app.PAGE)

    def test_subtitle_prepared_before_converting_lands_next_to_the_movie(self):
        self.movies.mkdir(exist_ok=True)
        dst = self.movies / "Movie.mkv"; dst.write_bytes(b"m")
        job = {"log": []}
        app.attach_prepared_subtitle(job, "movie.mkv", dst)             # 没备字幕：什么都不做
        self.assertNotIn("subtitle_sidecar", job)
        srt = "".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},800\n你好{n}\n\n" for n in range(1, 8))
        app.save_subtitle_candidate("movie.mkv", "zh.srt", data=srt.encode())
        app.attach_prepared_subtitle(job, "movie.mkv", dst)
        placed = self.movies / "Movie.zh-CN.srt"
        self.assertEqual(job["subtitle_sidecar"], str(placed))
        self.assertIn("你好7", placed.read_text("utf-8"))
        direct_card = app.PAGE.split("if(c.verdict==='none'&&c.all.verdict==='none'", 1)[1].split("const aOpts", 1)[0]
        self.assertIn("subBtn(it)", direct_card)                        # 直接入库前也能准备字幕

    # ---- 下载文件夹 / 影片文件夹可以在设置里选 ----
    def test_folders_can_be_chosen_in_settings(self):
        share = self.inbox.parent
        self.movies.mkdir(exist_ok=True)
        (share / "Films" / "4K").mkdir(parents=True); (share / "incoming").mkdir(); (share / "#recycle").mkdir()
        at = lambda name: str(share / name)                  # 测试里没接 Docker：NAS 上的位置和面板里的路径相同
        app.save_rules({"audio.dts": {"action": "keep"}})
        view = app.settings_view()
        self.assertEqual(view["groups"][-1], "文件夹")
        rows = {r["id"]: r for r in view["rows"] if r["group"] == "文件夹"}
        self.assertEqual((rows["folder.inbox"]["action"], rows["folder.library"]["action"]), (at("download"), at("Movies")))
        with patch.object(app, "request", types.SimpleNamespace(args={})):
            self.assertEqual(app.api_state()["folders"], {"inbox": "download", "library": "Movies",
                                                          "inbox_path": at("download"), "library_path": at("Movies")})
        with patch.object(app, "restart_service") as restart, patch.object(app, "SYSTEM_DIRS", set()):
            # 选错的都拒绝，且不重启
            for inbox, library in (("incoming", "incoming"), ("Films", "Films/4K"), ("nope", "Movies"),
                                   ("../x", "Movies"), ("download/dvwork", "Movies"), ("#recycle", "Movies")):
                with self.request({"rules": {"folder.inbox": {"action": at(inbox)}, "folder.library": {"action": at(library)}}}):
                    self.assertEqual(app.api_rules_save()[1], 400, (inbox, library))
            with self.request({"rules": {"folder.inbox": {"action": "incoming"}}}):       # 必须是完整路径
                self.assertEqual(app.api_rules_save()[1], 400)
            restart.assert_not_called()
            # 只改规则、文件夹没变：不重启
            with self.request({"rules": {"audio.dts": {"action": "pair"}, "folder.inbox": {"action": at("download")}}}):
                self.assertFalse(app.api_rules_save()["restarting"])
            restart.assert_not_called()
            # 换文件夹：记下来、把规则带过去、重启
            app.save_rules({"audio.flac": {"action": "keep"}})
            destination = share / "incoming" / app.WORK_NAME / app.RULES_FILE
            destination.parent.mkdir()
            destination.write_text('{"audio.flac":{"action":"replace"}}')
            with self.request({"rules": {"folder.inbox": {"action": at("incoming")},
                                         "folder.library": {"action": at("Films/4K")}}}):
                result = app.api_rules_save()
                self.assertTrue(result["restarting"])
                self.assertEqual(next(r["action"] for r in result["rows"] if r["id"] == "folder.inbox"), at("incoming"))
                self.assertEqual(json.loads(destination.read_text())["audio.flac"]["action"], "keep")
            restart.assert_called_once()
        saved = json.loads((share / app.FOLDERS_FILE_NAME).read_text("utf-8"))
        self.assertEqual(saved, {"inbox": at("incoming"), "library": at("Films/4K")})
        self.assertTrue((share / "incoming" / "dvwork" / app.RULES_FILE).is_file())
        # 下次启动按新的来；旧版记的相对位置照样认；文件坏了或内容不合法就回到默认
        with patch.object(app, "SHARE", share), patch.object(app, "SYSTEM_DIRS", set()):
            self.assertEqual(app.load_folders(), {"inbox": at("incoming"), "library": at("Films/4K")})
            self.assertEqual(app.folder_path(app.load_folders()["inbox"]), share / "incoming")
            (share / app.FOLDERS_FILE_NAME).write_text('{"inbox": "incoming", "library": "Films/4K"}', "utf-8")
            self.assertEqual(app.load_folders(), {"inbox": "incoming", "library": "Films/4K"})
            self.assertEqual(app.folder_path("incoming"), share / "incoming")
            (share / app.FOLDERS_FILE_NAME).write_text('{"inbox": "a", "library": "a"}', "utf-8")
            self.assertEqual(app.load_folders(), app.DEFAULT_FOLDERS)
            (share / app.FOLDERS_FILE_NAME).write_text("{broken", "utf-8")
            self.assertEqual(app.load_folders(), app.DEFAULT_FOLDERS)

    def test_nas_paths_map_to_panel_paths_through_the_mounts(self):
        mounts = [("/volume2/Share", "/share"), ("/volume1/video", "/volume1/video")]
        with patch.object(app, "PANEL_MOUNTS", mounts):
            self.assertEqual(app.panel_path("/volume2/Share/Movies"), "/share/Movies")
            self.assertEqual(app.panel_path("/volume2/Share"), "/share")
            self.assertEqual(app.panel_path("/volume1/video/4K"), "/volume1/video/4K")
            self.assertIsNone(app.panel_path("/volume2/ShareOther/x"))          # 只是名字开头一样，不算
            self.assertIsNone(app.panel_path("/volume3/x"))                     # 面板还没挂上
            self.assertEqual(app.host_path("/share/download/dvwork"), "/volume2/Share/download/dvwork")
            self.assertEqual(app.host_path("/somewhere/else"), "/somewhere/else")

    def test_choosing_a_folder_the_panel_cannot_see_remounts_the_container(self):
        self.movies.mkdir(exist_ok=True)
        share = self.inbox.parent
        mounts = [("/volume2/Share", str(share))]
        seen = {"/volume2/Share/download": [], "/volume2/Share/Movies": [], "/volume1/video": [], "/volume1/video/Films": []}
        def names(path):
            if path not in seen:
                raise FileNotFoundError(path)
            return seen[path]
        with patch.object(app, "PANEL_MOUNTS", mounts), patch.object(app, "host_folder_names", names), \
                patch.object(app, "remount_and_apply_folders") as remount, patch.object(app, "apply_folders") as apply:
            self.assertEqual(app.current_folders(), {"inbox": "/volume2/Share/download", "library": "/volume2/Share/Movies"})
            for bad in ("/volume1", "/usr/share/x", "/volume1/nope"):                # 整块硬盘、系统目录、不存在的
                with self.request({"rules": {"folder.library": {"action": bad}}}):
                    self.assertEqual(app.api_rules_save()[1], 400, bad)
            remount.assert_not_called(); apply.assert_not_called()
            with self.request({"rules": {"folder.library": {"action": "/volume1/video/Films"}}}):
                self.assertTrue(app.api_rules_save()["restarting"])
            apply.assert_not_called()
            self.assertEqual(remount.call_args[0][:2], ("/volume2/Share/download", "/volume1/video/Films"))

    def test_upgrade_helper_is_valid_python_and_can_add_folders(self):
        compile(app.UPGRADE_HELPER, "helper", "exec")
        # NAS 实测出过：只换影片文件夹时，新旧工作目录是同一个，复制规则文件报“是同一个文件”
        for marker in ("UPGRADE_EXTRA_BINDS", "UPGRADE_FOLDERS", "folders_backup", "/newinbox/dvwork", "os.path.samefile"):
            self.assertIn(marker, app.UPGRADE_HELPER)
        with patch.object(app, "PANEL_MOUNTS", [("/volume2/Share", "/share")]), patch.object(app, "WORK", Path("/share/download/dvwork")), \
                patch.object(app, "UPGRADE_STATUS_FILE", Path("/share/download/dvwork/.upgrade_status.json")):
            self.assertEqual(app.upgrade_status_binding(),
                             ({"/volume2/Share/download/dvwork": {"bind": "/oldwork", "mode": "rw"}}, "/oldwork/.upgrade_status.json"))

    # ---- 给别人部署用：名字、端口可配，工具在镜像里，成品归属可指定 ----
    def test_deployment_settings_come_from_the_environment(self):
        self.assertEqual((app.PANEL_CONTAINER, app.MAKEMKV_CONTAINER, app.JELLYFIN_CONTAINER, app.PANEL_PORT, app.JELLYFIN_PORT),
                         ("mediapanel", "makemkv", "jellyfin", 8097, 8096))             # 不设环境变量时和以前一样
        with patch.dict(app.os.environ, {"PORT": "9000", "X": " name "}):
            self.assertEqual((app.env_number("PORT", 8097), app.env_text("X", "d"), app.env_text("MISSING", "d")), (9000, "name", "d"))
        with patch.dict(app.os.environ, {"PORT": "abc"}):
            self.assertEqual(app.env_number("PORT", 8097), 8097)
        seen = []
        class Containers:
            def get(self, ref):
                seen.append(ref)
                if ref != "my-panel":
                    raise KeyError(ref)
                return "found"
        with patch.object(app, "PANEL_CONTAINER", "my-panel"):
            self.assertEqual(app.current_panel_container(types.SimpleNamespace(containers=Containers())), "found")
        self.assertEqual(seen, ["my-panel"])
        # 升级助手：端口映射和标签照搬，健康检查在新容器里做
        for marker in ("PortBindings", "labels=labels or None", "ports=ports or None", "new.exec_run", "UPGRADE_PORT"):
            self.assertIn(marker, app.UPGRADE_HELPER)
        self.assertNotIn("127.0.0.1:8097", app.UPGRADE_HELPER)

    def test_dovi_tool_ships_inside_the_image(self):
        dockerfile = app.EMBEDDED_DOCKERFILE
        for marker in ("dovi_tool-2.1.3-${target}.tar.gz", "sha256sum -c", "x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl",
                       app.BUNDLED_DOVI_TOOL + " --version"):
            self.assertIn(marker, dockerfile)
        self.assertIn(app.DOVI_TOOL, ("dovi_tool", app.BUNDLED_DOVI_TOOL))
        compose = (fixtures.APP_PATH.parent / "docker-compose.yml").read_text("utf-8")
        for marker in ("/var/run/docker.sock", ":/share", "PUID", "PANEL_CONTAINER", "JELLYFIN_URL"):
            self.assertIn(marker, compose)

    def test_outputs_are_handed_to_the_configured_owner(self):
        target = self.movies / "a.mkv"; self.movies.mkdir(exist_ok=True); target.write_bytes(b"x")
        with patch.object(app.os, "chown") as chown:
            app.hand_over(target)                                    # 没设：不动
            chown.assert_not_called()
            with patch.dict(app.os.environ, {"PUID": "1026", "PGID": "100"}):
                app.hand_over(target, self.movies / "missing.srt", None)
                chown.assert_called_once_with(str(target), 1026, 100)
            chown.reset_mock()
            with patch.dict(app.os.environ, {"PUID": "1026"}):
                app.hand_over(target)
                chown.assert_called_once_with(str(target), 1026, -1)   # 只设了用户：用户组不动

    def test_selfcheck_reports_tools_and_folders(self):
        self.movies.mkdir(exist_ok=True)
        versions = {"ffmpeg": "ffmpeg version 6", "mkvmerge": "mkvmerge v80"}
        with patch.object(app, "tool_version", lambda command: versions.get(Path(command[0]).name)), \
                patch.object(app, "request", types.SimpleNamespace(host="", args={})):
            result = app.api_selfcheck()
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertEqual(by_name["FFmpeg"]["state"], "ok")
        self.assertEqual(by_name["dovi_tool"]["state"], "missing")          # 必需的工具缺了要标出来
        self.assertEqual(by_name["下载文件夹"]["state"], "ok")
        self.assertIn(by_name["Jellyfin"]["state"], ("optional", "ok"))
        self.assertEqual(result["release"], app.APP_RELEASE)

    def test_cards_carry_quality_badges(self):
        def texts(media, track=None):
            return [b["text"] for b in app.media_badges(media, track)]
        uhd_dv = {"video": {"width": 3840, "height": 2160, "dv_profile": 8, "color_transfer": "smpte2084"},
                  "audio": [audio("eac3", channels=6, title="Dolby Digital Plus Atmos"), audio("ac3", channels=2, title="Commentary")]}
        self.assertEqual(texts(uhd_dv), ["4K", "杜比视界", "杜比全景声", "5.1"])
        hdr = {"video": {"width": 3840, "height": 1600, "dv_profile": 0, "color_transfer": "smpte2084"},
               "audio": [audio("truehd", channels=8), audio("ac3", channels=6)]}
        self.assertEqual(texts(hdr), ["4K", "HDR10", "无损音质", "7.1"])                      # 不指定时取音质最高的那条
        self.assertEqual(texts(hdr, hdr["audio"][1]), ["4K", "HDR10", "5.1"])                # 指定了电视会播的那条
        wide_sdr = {"video": {"width": 3840, "height": 2160, "dv_profile": 0, "color_transfer": "bt2020-10"},
                    "audio": [audio("pcm_s24le", channels=6)]}
        self.assertEqual(texts(wide_sdr), ["4K", "SDR", "无损音质", "5.1"])                   # 4K 广色域但不是 HDR：实测过的那张盘
        hd = {"video": {"width": 1920, "height": 1080, "dv_profile": 0, "color_transfer": "bt709"}, "audio": [audio("aac", channels=2)]}
        self.assertEqual(texts(hd), ["1080p", "SDR", "立体声"])
        self.assertEqual(app.media_badges(None), [])
        self.assertIn("mc-badges", app.PAGE)

    def test_subtitles_can_be_managed_without_jellyfin(self):
        # 没装 Jellyfin 的人：字幕窗口不能卡在“请填密钥”，影片旁边的字幕要照常能看、能删
        output = self.done_movie()
        (self.movies / "movie.chi.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", "utf-8")
        (self.movies / "movie.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", "utf-8")
        def call(endpoint, body):
            req = types.SimpleNamespace(get_json=lambda **kw: body, host="nas.local:8097")
            with patch.object(app, "request", req), patch.object(app, "jellyfin_reachable", lambda base: False):
                return endpoint()
        listed = call(app.api_subtitles_list, {"name": "movie.mkv"})
        self.assertFalse(listed["jellyfin"]); self.assertFalse(listed["needs_key"])
        self.assertIn("没有检测到 Jellyfin", listed["jellyfin_note"])
        rows = {x["filename"]: x for x in listed["existing"]}
        self.assertEqual(sorted(rows), ["movie.chi.srt", "movie.en.srt"])
        self.assertEqual((rows["movie.chi.srt"]["language"], rows["movie.en.srt"]["language"]), ("中文", "英语"))
        self.assertEqual(rows["movie.chi.srt"]["sync_id"], app.LOCAL_SUBTITLE_PREFIX + "movie.chi.srt")   # 也能校准
        # Jellyfin 在、只是没填密钥：提示填密钥，本地字幕照样列出来
        req = types.SimpleNamespace(get_json=lambda **kw: {"name": "movie.mkv"}, host="nas.local:8097")
        with patch.object(app, "request", req), patch.object(app, "jellyfin_reachable", lambda base: True):
            waiting = app.api_subtitles_list()
        self.assertTrue(waiting["needs_key"]); self.assertEqual(len(waiting["existing"]), 2)
        self.assertEqual(call(app.api_subtitles_delete, {"name": "movie.mkv", "filename": "movie.en.srt"})["ok"], True)
        self.assertFalse((self.movies / "movie.en.srt").exists()); self.assertTrue(output.exists())
        self.assertEqual(call(app.api_subtitles_delete, {"name": "movie.mkv", "index": 3})[1], 428)        # Jellyfin 里的那种才需要密钥

    def test_jellyfin_presence_is_checked_in_the_background(self):
        app.JELLYFIN_SEEN.update(base="", at=0.0, available=None, checking=False)
        with patch.object(app, "jellyfin_reachable", lambda base: False):
            app.jellyfin_available("nas.local:8097")
            for _ in range(50):
                if not app.JELLYFIN_SEEN["checking"] and app.JELLYFIN_SEEN["at"]:
                    break
                time.sleep(0.02)
            self.assertIs(app.jellyfin_available("nas.local:8097"), False)
        self.assertIsNone(app.jellyfin_available(""))                       # 不知道地址：不下结论
        self.assertIn("a.ghost.off", app.PAGE)

    def test_latest_version_is_read_from_the_project_tags(self):
        self.assertEqual(app.version_key("v0.10.0"), (0, 10, 0))
        self.assertGreater(app.version_key("0.10.0"), app.version_key("0.9.9"))       # 按数字比，不是按字符比
        self.assertIsNone(app.version_key("latest"))
        class Reply:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self.data).encode()
        tags = [{"name": "v0.9.0"}, {"name": "v0.11.2"}, {"name": "nightly"}, {"name": "v0.10.0"}]
        with patch.object(app.urlrequest, "urlopen", lambda req, timeout=0: Reply(tags)), patch.object(app, "APP_RELEASE", "0.10.0"):
            result = app.api_latest_version()
            self.assertEqual((result["latest"], result["newer"]), ("0.11.2", True))
        with patch.object(app.urlrequest, "urlopen", lambda req, timeout=0: Reply([{"name": "v0.10.0"}])), patch.object(app, "APP_RELEASE", "0.10.0"):
            self.assertFalse(app.api_latest_version()["newer"])
        def offline(req, timeout=0):
            raise OSError("no network")
        with patch.object(app.urlrequest, "urlopen", offline):
            self.assertEqual(app.api_latest_version()[1], 502)

    def test_space_check_also_looks_at_the_library_disk(self):
        item = {"kind": "file", "size": 20 * 1024 ** 3, "probe": None, "payload": ""}
        with patch.object(app, "disk_free", lambda where=None: 10 * 1024 ** 3 if where is not None else 900 * 1024 ** 3), \
                patch.object(app, "library_on_another_disk", lambda: True):
            ok, reason = app.precheck_space(item)
            self.assertFalse(ok); self.assertIn("影片文件夹所在的硬盘", reason)
        with patch.object(app, "disk_free", lambda where=None: 900 * 1024 ** 3), patch.object(app, "library_on_another_disk", lambda: True):
            self.assertTrue(app.precheck_space(item)[0])

    def test_buttons_and_controls_are_consistent(self):
        page = app.PAGE
        self.assertNotIn('class="subtitle-btn"', page)       # 加字幕的按钮在哪张卡片上都是同一种
        self.assertNotIn('class="retry"', page)
        self.assertIn('class="danger" data-discard=', page)   # 删除类的按钮都是同一种
        self.assertIn('class="danger" data-rebuild=', page)
        self.assertNotIn("<select disabled", page)            # 固定行为显示为说明
        self.assertIn('<table class="rule-matrix', page)
        self.assertIn('id="libraryName"', page)               # 标签显示的是影片文件夹的名字

    # ---- 已完成里展示影片库的影片 ----
    def test_polling_library_reads_cached_sidecars_and_actual_video_profile(self):
        self.movies.mkdir()
        for name in ("Film.mkv", "Film.Part.2.mkv", "Film.zh.srt", "Film.Part.2.chi.2.sup"):
            (self.movies / name).touch()
        media = probe("hevc", dv=7, subs=[sub("subrip")])
        paths = [self.movies / "Film.mkv", self.movies / "Film.Part.2.mkv"]
        app.sync_library_items(set())
        with patch.object(Path, "iterdir", autospec=True, side_effect=Path.iterdir) as listing, \
                patch.object(app, "ffprobe", return_value={}) as probe_file, \
                patch.object(app, "summarize_probe", return_value=media):
            app.refresh_output_info(paths)
            listing.assert_called_once_with(self.movies)
            self.assertEqual(probe_file.call_count, 2)
            listing.reset_mock()
            for _ in range(3):
                for item in app.api_state()["items"]:
                    self.assertEqual(item["info"]["subs"], "外挂 1 条，影片里 1 条")
                    self.assertTrue(item["info"]["picture"].endswith("杜比视界 P7"))
            listing.assert_not_called()
            (self.movies / "Film.zh.srt").unlink()
            app.refresh_output_info(paths)
            listing.assert_called_once_with(self.movies)
            self.assertEqual(probe_file.call_count, 2)
            self.assertEqual(app.output_info_lines(paths[0])["subs"], "影片里 1 条")
            self.assertEqual(app.output_info_lines(paths[1])["subs"], "外挂 1 条，影片里 1 条")

    def test_unreadable_movie_retries_only_after_the_file_changes(self):
        self.movies.mkdir()
        path = self.movies / "Broken.mkv"
        path.touch()
        app.sync_library_items(set())
        with patch.object(app, "ffprobe", return_value=None) as reader:
            for _ in range(4):
                app.refresh_output_info([path])
            self.assertEqual(reader.call_count, 3)
            self.assertTrue(app.api_state()["items"][0]["info_unavailable"])
            path.write_bytes(b"new movie")
            app.refresh_output_info([path], budget=0)
            self.assertFalse(app.api_state()["items"][0]["info_unavailable"])
            app.refresh_output_info([path])
            self.assertEqual(reader.call_count, 4)

    def test_library_movies_are_listed_so_subtitles_can_be_added_later(self):
        self.movies.mkdir(exist_ok=True)
        (self.movies / "Old.Movie.mkv").write_bytes(b"movie")
        (self.movies / "Movie.chi.srt").write_bytes(b"s")
        folder = self.movies / "柯南"; folder.mkdir(); (folder / "conan.mkv").write_bytes(b"m"); (folder / "conan.chi.srt").write_bytes(b"s")
        series = self.movies / "天道"; series.mkdir()
        for n in range(3): (series / f"E0{n}.mkv").write_bytes(b"e")
        # 本工具转出来、源还在的那部，不重复列
        source = self.inbox / "src.mkv"; source.write_bytes(b"x")
        mine = self.movies / "src.mkv"; mine.write_bytes(b"out")
        app.PROCESSED["src.mkv"] = {"output": str(mine), "source_size": 1}
        app.SIZE_HISTORY["src.mkv"] = 1
        with patch.object(app, "ffprobe", return_value=None):
            app.scan_once()
        names = sorted(n for n in app.ITEMS if n.startswith(app.LIBRARY_PREFIX))
        self.assertEqual(names, ["影片库/Old.Movie.mkv", "影片库/柯南/conan.mkv"])
        self.assertEqual(app.ITEMS["src.mkv"]["status"], "done")
        with patch.object(app, "request", types.SimpleNamespace(args={})):
            shown = {i["name"]: i for i in app.api_state()["items"]}
        old = shown["影片库/Old.Movie.mkv"]
        self.assertEqual((old["status"], old["library"], old["retryable"], old["deletable"], old["discardable"]),
                         ("done", True, False, False, False))
        self.assertEqual(old["output_name"], "Old.Movie.mkv")
        self.assertIsNone(old["info"])                           # 还没读到基本信息时不乱显示
        # 影片库里的影片不能被“重做”“删源”“不要了删除”
        for endpoint in (app.api_rebuild, app.api_retry, app.api_delete_source, app.api_discard_source):
            with self.request({"name": "影片库/Old.Movie.mkv"}):
                self.assertEqual(endpoint()[1], 409, endpoint.__name__)
        self.assertTrue((self.movies / "Old.Movie.mkv").exists())
        # 可以给它一键找字幕
        base, seen = self.fake_jellyfin([{"Id": "s1", "Name": "Old.chs"}],
                                        movies=[{"Id": "abc", "Path": "/media/movies/Old.Movie.mkv"}])
        app.save_jellyfin_api_key("k" * 32)
        self.assertTrue(self.auto(base, {"name": "影片库/Old.Movie.mkv"})["ok"])
        # 基本信息：格式、画面、声音（电视播不了的标出来）、字幕（外挂的和影片里的分开数）
        media = probe(audio_tracks=[audio("eac3", "jpn", 6, default=True), audio("dts", "jpn", 6, profile="DTS-HD MA")],
                      subs=[sub("subrip", "chi")])
        (self.movies / "Old.Movie.chi.srt").write_bytes(b"s")
        with patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=media):
            app.refresh_output_info([self.movies / "Old.Movie.mkv"])
        info = app.output_info_lines(self.movies / "Old.Movie.mkv")
        self.assertTrue(info["picture"].startswith("MKV · "))
        self.assertEqual(info["audio"], "日语 · 环绕声 · 普通音质 等 2 条")       # 只写电视上该选的那条
        only_dts = probe(audio_tracks=[audio("dts", "jpn", 6, profile="DTS-HD MA")])
        with patch.object(app, "ffprobe", return_value={}), patch.object(app, "summarize_probe", return_value=only_dts):
            (self.movies / "Old.Movie.mkv").write_bytes(b"movie2"); app.refresh_output_info([self.movies / "Old.Movie.mkv"])
        self.assertTrue(app.output_info_lines(self.movies / "Old.Movie.mkv")["audio"].endswith("（电视播不了）"))
        self.assertEqual(info["subs"], "外挂 1 条，影片里 1 条")
        # 影片被拿走后，条目和记录一起消失
        (self.movies / "Old.Movie.mkv").unlink()
        with patch.object(app, "ffprobe", return_value=None):
            app.scan_once()
        self.assertNotIn("影片库/Old.Movie.mkv", app.ITEMS)
        self.assertNotIn("影片库/Old.Movie.mkv", app.PROCESSED)

    def test_cards_talk_about_playback_and_every_rule_says_why(self):
        # 卡片只讲播放效果。电视上播放时切不了音轨（用户实测），不能写“可在播放时切换”
        card = app.conversion_card("file", EVA, "/x/movie.mkv", 25 * 1024**3)
        p7 = app.conversion_card("bdmv", probe("hevc", dv=7), "/x/disc", 60 * 1024**3)
        text = " ".join(card["now"] + card["gains"] + card["costs"] + p7["now"] + p7["gains"] + p7["costs"])
        self.assertIn("保留原始音轨，供功放或回音壁使用", text)
        self.assertIn("移除电视用不到的数据，画面观感不变", text)
        for wrong in ("播放时切换", "增强层", "双层信息", "兼容音轨", "封装", "Profile"):
            self.assertNotIn(wrong, text)
        # 设置页：每条可改的规则都带着“默认为什么这么做”
        rows = [r for r in app.rules_view()["rows"] if not r.get("note_only")]
        self.assertTrue(all(r["reason"] for r in rows), [r["id"] for r in rows if not r["reason"]])
        self.assertIn("LG G5", next(r["reason"] for r in rows if r["id"] == "audio.dtshd"))
        self.assertIn("ruleReason(r)", app.PAGE)

    def test_table_remarks_can_be_edited_and_saved(self):
        # 表格里每一行的备注默认是程序给的说明，用户可以改；改过的要存下来，换规则、换文件夹都不丢
        note_id = "note.video.0.mp4"
        row = next(r for r in app.rules_view()["rows"] if r["id"] == note_id)
        self.assertEqual((row["note_only"], row["has_note"], row["note"]), (True, False, ""))
        with self.request({"rules": {note_id: {"note": "我家电视 MP4 更稳"}, "note.codec.other": {"note": "x" * 999}}}):
            saved = app.api_rules_save()
        self.assertTrue(saved["ok"]); self.assertFalse(saved["customized"])                # 改备注不算改规则
        row = next(r for r in saved["rows"] if r["id"] == note_id)
        self.assertEqual((row["has_note"], row["note"]), (True, "我家电视 MP4 更稳"))
        self.assertEqual(len(app.saved_table_notes()["note.codec.other"]), 300)
        with self.request({"rules": {"audio.dts": {"action": "keep"}}}):                    # 再改规则，备注还在
            app.api_rules_save()
        self.assertEqual(app.saved_table_notes()[note_id], "我家电视 MP4 更稳")
        self.assertEqual(app.rule("audio.dts"), "keep")
        for bad in ({"note.video.0.mp4": {"action": "x"}}, {"note.video.0.mp4": {"note": 3}}, {"note.nope": {"note": "a"}}):
            with self.request({"rules": bad}):
                self.assertEqual(app.api_rules_save()[1], 400)
        with self.request({"rules": {note_id: {"note": ""}}}):                             # 可以清空成没有备注
            app.api_rules_save()
        self.assertEqual(app.saved_table_notes()[note_id], "")
        # 字幕页有一行固定规则：DVD、DVB、XSUB 图片字幕不带进成品
        self.assertEqual(app.rules_view()["subtitle_fixed"][0][:4], ["图片字幕（DVD、DVB、XSUB）", "丢弃", "无", "无"])
        # 媒体目录是输入框、没有“面板内路径”；音轨和字幕表格按成品格式分行
        self.assertNotIn("面板内路径", app.PAGE)
        for marker in ("noteInput(", "table-note", "源音轨编码", "成品音轨", "splitTable(", "data-folder=", 'id="folderModal"'):
            self.assertIn(marker, app.PAGE)

    def test_bracket_tag_folder_names_become_a_title_jellyfin_can_recognise(self):
        # NAS 实测：成品叫 “[BDMV][230308][Shin Evangelion Gekijouban][Movie].mkv”，Jellyfin 把片名认成 “[BDMV”
        self.assertEqual(app.output_stem("[BDMV][230308][Shin Evangelion Gekijouban][Movie]", "bdmv"),
                         "Shin Evangelion Gekijouban")
        self.assertEqual(app.output_stem("[BDMV][230308][Shin Evangelion Gekijouban][Movie]/UHD", "bdmv"),
                         "Shin Evangelion Gekijouban - UHD")
        self.assertEqual(app.output_stem("[自购] Your Name (2016) [BDMV][2160p]", "bdmv"), "Your Name (2016)")
        self.assertEqual(app.output_stem("[BDMV][2021].iso", "iso"), "[BDMV][2021]")          # 整理不出片名就不动
        # 正常的名字不碰
        self.assertEqual(app.output_stem("Neon.Genesis.Evangelion.1997.REMUX-GROUP.mkv", "file"),
                         "Neon.Genesis.Evangelion.1997.REMUX-GROUP")
        self.assertEqual(app.output_stem("Movie [2019]", "bdmv"), "Movie [2019]")

    def test_one_click_subtitle_says_when_jellyfin_did_not_recognise_the_movie(self):
        self.done_movie()
        app.save_jellyfin_api_key("k" * 32)
        base, _ = self.fake_jellyfin([], plugins=["Open Subtitles"],
                                     movies=[{"Id": "abc", "Name": "[BDMV", "Path": "/media/Movies/movie.mkv", "ProviderIds": {}}])
        error = self.auto(base, {"name": "movie.mkv"})[0]["error"]
        self.assertIn("没认出这是哪部片", error); self.assertIn("“[BDMV”", error); self.assertIn("识别", error)
        # 认出来了但确实没有字幕：还是原来的提示
        base, _ = self.fake_jellyfin([], plugins=["Open Subtitles"],
                                     movies=[{"Id": "abc", "Path": "/media/Movies/movie.mkv", "ProviderIds": {"Tmdb": "1"}}])
        self.assertIn("未找到中文字幕", self.auto(base, {"name": "movie.mkv"})[0]["error"])

    def test_rename_moves_movie_sidecars_and_record_together(self):
        self.movies.mkdir(exist_ok=True)
        out = self.movies / "[BDMV][Movie].mkv"; out.write_bytes(b"movie")
        sup = self.movies / "[BDMV][Movie].chi.sup"; sup.write_bytes(b"s")
        srt = self.movies / "[BDMV][Movie].zh-CN.srt"; srt.write_bytes(b"s")
        other = self.movies / "[BDMV][Movie] 2.mkv"; other.write_bytes(b"x")          # 名字相近的另一部片不能被带走
        app.ITEMS["disc"] = {"name": "disc", "status": "done", "kind": "bdmv"}
        app.PROCESSED["disc"] = {"output": str(out), "sidecars": [str(sup)], "subtitle_sidecar": str(srt)}
        def rename(title, name="disc"):
            with self.request({"name": name, "title": title}):
                return app.api_rename_output()
        for bad in ("", "a/b", "..", ".hidden", "x" * 200, 'a:b', "a\\b"):
            self.assertEqual(rename(bad)[1], 400, bad)
        (self.movies / "Taken.mkv").write_bytes(b"t")
        self.assertEqual(rename("Taken")[1], 409)                                     # 不覆盖已有文件
        self.assertTrue(out.exists())
        self.assertTrue(rename("  Real  Title (2021) ")["ok"])
        new = self.movies / "Real Title (2021).mkv"
        self.assertEqual(new.read_bytes(), b"movie")
        self.assertTrue((self.movies / "Real Title (2021).chi.sup").is_file())
        self.assertTrue((self.movies / "Real Title (2021).zh-CN.srt").is_file())
        self.assertFalse(out.exists()); self.assertTrue(other.exists())
        record = app.PROCESSED["disc"]
        self.assertEqual((record["output"], record["sidecars"], record["subtitle_sidecar"]),
                         (str(new), [str(self.movies / "Real Title (2021).chi.sup")], str(self.movies / "Real Title (2021).zh-CN.srt")))
        self.assertEqual(app.tracked_movie_output("disc"), new.resolve())             # 改名后仍能删源、加字幕
        # 影片库里的条目也能改；没入库的不能
        with patch.object(app, "ffprobe", return_value=None):
            app.scan_once()
        self.assertTrue(rename("Other Film", "影片库/Taken.mkv")["ok"])
        self.assertTrue((self.movies / "Other Film.mkv").is_file())
        app.ITEMS["raw"] = {"name": "raw", "status": "ready", "kind": "file"}
        self.assertEqual(rename("X", "raw")[1], 409)
        # 校准窗口里有“自动校准”按钮；“移除已选字幕”已去掉（每条字幕旁边有删除）
        self.assertNotIn("移除已选字幕", app.PAGE)
        # 校准窗口一打开就带着自动算出的时间（请求里带 auto），没有单独的“自动校准”按钮
        self.assertNotIn('id="subtitleSyncAuto"', app.PAGE); self.assertIn("auto:!confirmAudio", app.PAGE)
        for marker in ('id="renameModal"', "data-rename=", "/api/rename_output"):
            self.assertIn(marker, app.PAGE)

    def test_subtitle_pick_prefers_the_same_kind_of_release(self):
        # NAS 实测的三条结果：蓝光原盘转出来的片，应该挑蓝光版的字幕，而不是下载最多的流媒体版
        results = [
            {"Id": "a", "Name": "Evangelion.3.0+1.01.Thrice.Upon.a.Time.2021.1080p.AMZN.WEB-DL.DD+.5.1.H.264-GROUP", "DownloadCount": 3912, "Format": "srt"},
            {"Id": "b", "Name": "新世纪福音战士新剧场版 4：终 (2021) 1080p AAC", "DownloadCount": 1522, "Format": "srt"},
            {"Id": "c", "Name": "Shin.Evangelion.Gekijouban.EVANGELION.3.0.1.11.THRICE.UPON.A.TIME.2023.COMPLETE.BLURAY-GROUP", "DownloadCount": 1092, "Format": "srt"},
        ]
        self.assertEqual(app.pick_chinese_subtitle(results, "[BDMV][230308][Shin Evangelion] bdmv")["Id"], "c")
        self.assertEqual(app.pick_chinese_subtitle(results, "Movie.2021.1080p.AMZN.WEB-DL.mkv")["Id"], "a")
        self.assertEqual(app.pick_chinese_subtitle(results, "Movie.mkv")["Id"], "a")          # 看不出片源：按下载次数
        self.assertEqual(app.release_kind("Some.Film.REMUX-GRP"), "disc")
        self.assertIsNone(app.release_kind("Embedded abduction"))                              # 不把单词里的 bd、web 当标记

    def test_subtitle_dialog_lists_this_movies_subtitles_and_lets_the_user_choose(self):
        self.done_movie()
        app.ITEMS["movie.mkv"]["kind"] = "bdmv"
        app.save_jellyfin_api_key("k" * 32)
        streams = [
            {"Type": "Video", "Index": 0},
            {"Type": "Subtitle", "Index": 2, "IsExternal": True, "Language": "chi",
             "Path": "/config/metadata/library/ab/cd/movie.chi.srt"},
            {"Type": "Subtitle", "Index": 3, "IsExternal": True, "Language": "chi", "Path": "/media/Movies/movie.chi.sup"},
            {"Type": "Subtitle", "Index": 1, "IsExternal": False, "Language": "eng"},
        ]
        results = [
            {"Id": "web", "Name": "Movie.2021.AMZN.WEB-DL", "DownloadCount": 3912, "Format": "srt"},
            {"Id": "bd", "Name": "Movie.2023.COMPLETE.BLURAY-GRP.chs", "DownloadCount": 1092, "Format": "srt"},
            {"Id": "mt", "Name": "Movie machine", "DownloadCount": 5, "MachineTranslated": True},
        ]
        base, seen = self.fake_jellyfin(results, plugins=["Open Subtitles"], movies=[
            {"Id": "abc", "Name": "电影", "Path": "/media/Movies/movie.mkv", "ProviderIds": {"Tmdb": "1"}, "MediaStreams": streams}])
        def call(endpoint, **extra):
            req = types.SimpleNamespace(get_json=lambda **kw: {"name": "movie.mkv", **extra}, host="nas:8097")
            with patch.object(app, "request", req), patch.dict(app.os.environ, {"JELLYFIN_URL": base}), \
                    patch.object(app, "SUBTITLE_SETTLE_SECONDS", 0):
                return endpoint()
        # 现有字幕：只列这部片的外挂字幕；装在影片里的只报条数
        listed = call(app.api_subtitles_list)
        # 影片旁边有两份：一份 Jellyfin 已读到（.chi.sup），一份刚校准出来它还没发现（.zh-CN.srt）
        (self.movies / "movie.chi.sup").write_bytes(b"s"); (self.movies / "movie.zh-CN.srt").write_bytes(b"s")
        app.PROCESSED["movie.mkv"]["sidecars"] = [str(self.movies / "movie.chi.sup")]
        app.PROCESSED["movie.mkv"]["subtitle_sidecar"] = str(self.movies / "movie.zh-CN.srt")
        listed = call(app.api_subtitles_list)
        self.assertEqual([(x["index"], x["filename"], x["beside_movie"], x["origin"]) for x in listed["existing"]],
                         [(2, "movie.chi.srt", False, "从网上下载的"), (3, "movie.chi.sup", True, "转换时从影片里取出的"),
                          (None, "movie.zh-CN.srt", True, "校准后的")])
        self.assertTrue(all(x["when"] for x in listed["existing"] if x["beside_movie"]))
        self.assertEqual((listed["embedded"], listed["title"], listed["recognised"]), (1, "电影", True))
        # 卡片上的字幕条数跟着 Jellyfin 里的实际情况走（它下载的字幕不在影片旁边）
        out = self.movies / "movie.mkv"
        with patch.object(app, "OUTPUT_INFO", {out: {"probe": probe(), "external": 0}}):
            self.assertEqual(app.output_info_lines(out)["subs"], "外挂 3 条")
        self.assertIn("refreshSubtitlesLater()", app.PAGE)
        # 搜索：只列结果，不下载；蓝光来的片把蓝光版排第一并标推荐
        found = call(app.api_subtitles_search)["results"]
        self.assertEqual([x["id"] for x in found], ["bd", "web", "mt"])
        self.assertEqual([x["recommended"] for x in found], [True, False, False])
        self.assertEqual(found[0]["tags"], ["片源版本一致", "简体"])
        self.assertIn("片源版本不同，时间轴可能不符", found[1]["tags"])
        self.assertIn("机器翻译", found[2]["tags"])
        self.assertFalse(any(method == "POST" for method, _ in seen))
        # 下载用户选的那一份
        self.assertTrue(call(app.api_subtitles_download, subtitle_id="web")["ok"])
        self.assertIn(("POST", "/Items/abc/RemoteSearch/Subtitles/web"), seen)
        self.assertEqual(call(app.api_subtitles_download)[1], 400)
        # 删除：只能删外挂的
        self.assertTrue(call(app.api_subtitles_delete, index=2)["ok"])
        self.assertIn(("DELETE", "/Videos/abc/Subtitles/2"), seen)
        for bad in (1, 99):
            self.assertEqual(call(app.api_subtitles_delete, index=bad)[1], 409)
        self.assertEqual(call(app.api_subtitles_delete, index="2")[1], 400)
        self.assertEqual(len([1 for method, _ in seen if method == "DELETE"]), 1)
        # 影片旁边的字幕由面板自己删（NAS 实测让 Jellyfin 删会报 500），记录跟着清掉，再通知 Jellyfin 刷新
        self.assertTrue(call(app.api_subtitles_delete, filename="movie.zh-CN.srt")["ok"])
        self.assertFalse((self.movies / "movie.zh-CN.srt").exists())
        self.assertIsNone(app.PROCESSED["movie.mkv"]["subtitle_sidecar"])
        self.assertTrue(any(method == "POST" and "/Items/abc/Refresh" in path for method, path in seen))
        self.assertEqual(len([1 for method, _ in seen if method == "DELETE"]), 1)          # 没有去让 Jellyfin 删
        self.assertEqual(call(app.api_subtitles_delete, filename="../other.srt")[1], 400)  # 只认这部片旁边的文件
        self.assertTrue((self.movies / "movie.chi.sup").exists())
        # 没入库的片不能管理字幕
        app.ITEMS["movie.mkv"]["status"] = "ready"
        self.assertEqual(call(app.api_subtitles_list)[1], 409)

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_page_script_has_no_syntax_errors(self):
        # 出过一次：改弹窗时误删了一行函数头，整页脚本报语法错，列表一直停在“正在载入”
        page = app.PAGE
        import re
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)   # 页头里定深浅色的一小段 + 主脚本
        self.assertEqual(len(scripts), 2)
        for number, script in enumerate(scripts):
            target = self.work / f"page{number}.js"; target.write_text(script, "utf-8")
            result = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[:600])

    def test_download_waits_until_jellyfin_has_registered_the_subtitle(self):
        # NAS 实测：下载后马上列字幕，新下的那份还没出现
        self.done_movie()
        app.save_jellyfin_api_key("k" * 32)
        ext = {"Type": "Subtitle", "Index": 0, "IsExternal": True, "Path": "/config/metadata/library/a/movie.chi.srt"}
        counts = iter([[], [], [], [ext]])                       # 下载前 0 条；下载后第三次查才出现
        calls = []
        def streams(ctx, movie):
            calls.append(1); return next(counts, [ext])
        req = types.SimpleNamespace(get_json=lambda **kw: {"name": "movie.mkv", "subtitle_id": "x"}, host="nas:8097")
        with patch.object(app, "request", req), patch.object(app, "jellyfin_subtitle_streams", side_effect=streams), \
                patch.object(app, "jellyfin_find_movie", return_value={"Id": "abc"}), \
                patch.object(app, "jellyfin_call", return_value=None), patch.object(app.time, "sleep"):
            self.assertEqual(app.api_subtitles_download()["message"], "已下载，可在电视上选择")
            self.assertEqual(len(calls), 4)
            # 一直没登记上：不谎称已经好了
            with patch.object(app, "SUBTITLE_SETTLE_SECONDS", 0), patch.object(app, "jellyfin_subtitle_streams", return_value=[]):
                self.assertIn("正在处理", app.api_subtitles_download()["message"])

    def test_graphic_subtitle_packets_become_one_cue_per_line(self):
        # 数据取自 NAS 上那张 UHD 原盘：一句字幕 = 几个挨着的包（有一个大的），每秒重复一次；只有小包的一组是清屏
        def show(t, big):
            return [(t + 62, 22), (t + 59, 13), (t, 160), (t + 6, big), (t + 6, 3)]
        def clear(t):
            return [(t + 4, 14), (t + 1, 13), (t, 3)]
        packets = (show(4_205_688, 38294) + show(4_206_747, 38294) + show(4_207_748, 38294) + clear(4_209_381)
                   + show(4_209_402, 15337) + clear(4_211_135) + show(4_211_153, 22085) + show(4_212_214, 22085)
                   + show(4_213_300, 9000))                                  # 没清屏直接换下一句
        cues = app._graphic_cues(packets)
        self.assertEqual([c[0] for c in cues], [4_205_750, 4_209_464, 4_211_215, 4_213_362])
        self.assertEqual(cues[0][1], 4_209_464)                              # 清屏后紧跟着下一句：第一句到下一句出现为止
        self.assertEqual(cues[2][1], 4_213_362)                              # 被下一句顶掉

    @unittest.skipUnless(shutil.which("ffprobe"), "需要 ffprobe")
    def test_conversion_saves_subtitle_timing_so_calibration_works_after_the_source_is_gone(self):
        self.movies.mkdir(exist_ok=True)
        dst = self.movies / "Movie.mkv"; dst.write_bytes(b"m")
        srt = self.work / "anchor.srt"
        starts = [60_000, 63_400, 71_250, 80_900, 84_100, 97_700, 120_300, 126_050, 141_800, 150_500, 173_900, 181_200]
        app.write_srt(srt, [{"start": t, "end": t + 2_000, "text": f"line {i}"} for i, t in enumerate(starts)])
        self.assertEqual(app.save_subtitle_timing(dst, [srt, self.work / "missing.sup"]), 1)
        cues = app.load_subtitle_timing(dst)
        self.assertEqual((len(cues), cues[0]["start"], cues[0]["text"]), (12, 60_000.0, ""))
        srt.unlink()                                                         # 临时文件和源都没了，参照还在
        self.assertEqual(len(app.timing_anchor_cues("x", dst)), 12)
        # 下载来的字幕整体慢了 4.5 秒：按参照对回来
        late = [{"start": t + 4_500, "end": t + 6_500, "text": f"第{i}句"} for i, t in enumerate(starts)]
        aligned, transform = app.align_by_timing({"log": []}, cues, late, self.work / "aligned.srt")
        self.assertAlmostEqual(transform["offset_ms"], -4500, delta=60)
        self.assertEqual(app.parse_srt_text(aligned.read_text("utf-8"))[0]["start"], 60_000)
        # 改名后参照跟着走
        app.ITEMS["x"] = {"name": "x", "status": "done", "kind": "file"}
        app.PROCESSED["x"] = {"output": str(dst)}
        with self.request({"name": "x", "title": "New Name"}):
            self.assertTrue(app.api_rename_output()["ok"])
        self.assertEqual(len(app.load_subtitle_timing(self.movies / "New Name.mkv")), 12)
        self.assertIsNone(app.load_subtitle_timing(dst))
        # 转换时：没选中的字幕也导出一两条到临时目录取时间点；强制字幕和选中的不重复导
        media = probe(subs=[sub("subrip", "chi"), sub("hdmv_pgs_subtitle", "jpn"), sub("subrip", "eng", forced=True),
                            sub("subrip", "eng"), sub("subrip", "fre")])
        args, paths = app.anchor_subtitle_outputs(self.work, media, [0])
        self.assertEqual([p.name for p in paths], ["timing-anchor.1.sup", "timing-anchor.3.srt"])
        self.assertIn("-map 0:s:1 -c:s copy -f sup", " ".join(args))

    def test_video_rule_table_matches_what_conversion_really_does(self):
        # 设置页“转换规则”表的每一行，都要和转换流程实际的判断一致
        ext = {"MKV": ("file", "m.mkv"), "MP4": ("file", "m.mp4"), app.OTHER_FILE_FORMATS: ("file", "m.m2ts"),
               app.DISC_FORMATS: ("bdmv", "disc")}
        media = {"双层杜比视界（Profile 7）": probe("hevc", dv=7), "其他杜比视界": probe("hevc", dv=8), "HDR10、SDR": probe("hevc")}
        for dv in ("mp4", "keep"):
            for p7 in ("convert", "keep"):
                app.save_rules({"video.dv": {"action": dv}, "video.dv7": {"action": p7}})
                rows = app.rules_view()["video_outputs"][dv][p7]
                self.assertEqual(len(rows), 12)
                for name, fmt, action, result, picture, note_id in rows:
                    self.assertIn(note_id, app.TABLE_NOTE_IDS)
                    plan = app.make_plan(ext[fmt][0], media[name], ext[fmt][1])
                    self.assertEqual(action == "转换", plan["needs"] != "none", (dv, p7, name, fmt))
                    if action == "转换":
                        self.assertEqual(result, plan["container"].upper(), (dv, p7, name, fmt))
                    self.assertEqual("8.1" in picture, "p7" in plan["must"])
                    self.assertIn(action, ("转换", "不转换"))                      # 处理只有这两种说法
        app.save_rules(None)
        view = app.rules_view()
        # 每一格都写实际结果：不出现“等”“其余”“按上面”这类说法；文件格式列全
        everything = " ".join(" ".join(row[:5]) for row in view["video_outputs"]["mp4"]["convert"] + view["video_codecs"]
                              + view["subtitle_fixed"])
        for vague in ("等", "其余", "按上面", "同上"):
            self.assertNotIn(vague, everything)
        listed = {x.lower() for x in app.OTHER_FILE_FORMATS.split("、")} | {"mkv", "mp4"}
        self.assertEqual(listed, {x.lstrip(".") for x in app.VIDEO_EXT})                 # 面板认的文件格式一个不漏
        self.assertEqual(view["video_outputs"]["mp4"]["convert"][3][:5],
                         ["双层杜比视界（Profile 7）", app.DISC_FORMATS, "转换", "MP4", "杜比视界转为 Profile 8.1"])
        self.assertEqual(view["video_outputs"]["mp4"]["convert"][11][:5],
                         ["HDR10、SDR", app.DISC_FORMATS, "转换", "MKV", ""])
        self.assertEqual(view["video_outputs"]["mp4"]["convert"][9][:5],
                         ["HDR10、SDR", "MP4", "不转换", "MP4", "因音轨或字幕转换时，成品为 MKV"])
        # 画面编码表和文件格式表是同一种列：片源 / 编码 / 处理 / 成品 / 备注（第六项是备注的编号）
        self.assertTrue(all(len(row) == 6 and row[2] == "不转换" for row in view["video_codecs"]))
        # 画面编码：只有 H.264、HEVC 验证过；别的编码原样复制，卡片上如实说没试过
        old = probe("vc1", audio_tracks=[audio("dts", "eng", 6, default=True)])
        plan = app.make_plan("file", old, "m.mkv")
        self.assertIn("画面是 VC-1 编码，未经电视验证；转换不改变编码", plan["now"])
        fine_audio = app.make_plan("file", probe("mpeg2video"), "m.mkv")
        self.assertEqual((fine_audio["needs"], fine_audio["gains"][0]), ("none", "画面是 MPEG-2 编码，未经电视验证"))
        self.assertNotIn("没在电视上试过", " ".join(app.make_plan("file", probe("hevc"), "m.mkv")["gains"]))

    def test_alignment_finds_a_framerate_stretch_against_a_denser_reference(self):
        # 按 NAS 实测的形状造数据：参照比字幕密（多出音效说明），字幕每句有零点几秒的随机出入，
        # 而且整体快了千分之一（到 95 分钟时差 6 秒）。旧算法在这种数据上判成“对不上”。
        import random
        rng = random.Random(7)
        t, lines = 5000.0, []
        while t < 9_000_000:
            lines.append(t); t += rng.choice([1800, 2500, 3300, 4200, 6000, 9000, 15000])
        reference = sorted(lines + [x + 1200 for x in lines[::4]])                  # 多出来的音效说明
        ref_cues = [{"start": x, "end": x + 1500, "text": ""} for x in reference]
        spoken = [x for k, x in enumerate(lines) if k % 5 != 0]
        fast = [{"start": (x + rng.uniform(-250, 250) - 460) / 1.001, "end": x / 1.001 + 1500, "text": "x"} for x in spoken]
        found = app.estimate_subtitle_transform(ref_cues, fast)
        self.assertAlmostEqual(found["scale"], 1.001, places=6)
        self.assertAlmostEqual(found["offset_ms"], 460, delta=120)
        # 没拉伸、只是整体晚了 3.2 秒
        late = [{"start": x + 3200 + rng.uniform(-200, 200), "end": x + 4700, "text": "x"} for x in spoken]
        found = app.estimate_subtitle_transform(ref_cues, late)
        self.assertEqual(found["scale"], 1.0); self.assertAlmostEqual(found["offset_ms"], -3200, delta=120)
        # 不同剪辑版（中间少了 5 分钟）：拒绝，不硬套
        cut = [{"start": c["start"] - (300_000 if c["start"] > 3_600_000 else 0), "end": 0, "text": "x"}
               for c in late if not 3_300_000 < c["start"] <= 3_600_000]
        self.assertIsNone(app.estimate_subtitle_transform(ref_cues, cut))

    def test_disc_subtitle_timing_follows_clip_durations(self):
        # NAS 实测（UHD 原盘）：正片 = 12 秒的片头片段 + 正片片段。第二段的第一句在该片段 5.756 秒处，
        # 在整条时间线上是 17.77 秒；用 mkvmerge 只取字幕时它被放在了 6.26 秒，整部片的参照早了 11.5 秒。
        def show(t, big=38294):
            return [(t + 62, 22, 0), (t + 59, 13, 0), (t, 160, 0), (t + 6, big, 0), (t + 6, 3, 0)]
        def clear(t):
            return [(t + 4, 14, 0), (t + 1, 13, 0), (t, 3, 0)]
        first = {"start": 4199.0, "duration": 12.015, "streams": {3: {"key": "0x12a0", "codec": "hdmv_pgs_subtitle", "lang": "jpn",
                 "rows": show(4_198_938, 1280) + clear(4_199_500)}}}
        rows = []
        for n in range(6):
            rows += show(4_205_694 + n * 9000, 30000 + n) + clear(4_209_380 + n * 9000)
        second = {"start": 4200.0, "duration": 9290.28, "streams": {3: {"key": "0x12a0", "codec": "hdmv_pgs_subtitle", "lang": "jpn", "rows": rows}}}
        with patch.object(app, "_subtitle_packets", side_effect=[first, second]):
            tracks = app.disc_subtitle_time_points(["00005.m2ts", "00006.m2ts"], 9302.293)
        starts = [p[0] for p in tracks[0]["points"]]
        self.assertEqual(starts[0], 0)
        self.assertEqual(starts[1], 12_015 + 5_756)                     # 接在上一段画面结束之后
        self.assertEqual(starts[2] - starts[1], 9000)
        # 片段总时长和播放列表对不上：放弃，不给错的参照
        with patch.object(app, "_subtitle_packets", side_effect=[first, second]):
            self.assertEqual(app.disc_subtitle_time_points(["a", "b"], 9000.0), [])

    def test_a_subtitle_beside_the_movie_is_calibrated_in_place_again_and_again(self):
        # 用户反馈：校准后那份没有“校准时间”按钮，还另外多出一个文件。现在写回原文件，可以一遍遍校准
        self.movies.mkdir(exist_ok=True)
        output = self.movies / "movie.mkv"; output.write_bytes(b"v")
        app.ITEMS["m"] = {"name": "m", "status": "done", "kind": "file"}; app.PROCESSED["m"] = {"output": str(output)}
        beside = self.movies / "movie.chi.srt"
        app.write_srt(beside, [{"start": 10_000 + i * 4_000, "end": 11_500 + i * 4_000, "text": f"句{i}"} for i in range(8)])
        def calibrate(first_ms):
            body = {"name": "m", "subtitle_id": app.LOCAL_SUBTITLE_PREFIX + "movie.chi.srt", "confirm_audio": True,
                    "manual_first_start_ms": first_ms}
            with patch.object(app, "request", types.SimpleNamespace(get_json=lambda **kw: body, host="")), \
                    patch.object(app, "ffprobe", lambda path: None), patch.object(app, "refresh_jellyfin_for", lambda name: True):
                return app.api_jellyfin_subtitle_candidate()
        first = calibrate(25_000)
        self.assertTrue(first["applied"]); self.assertEqual(first["sidecar"], "movie.chi.srt")
        self.assertAlmostEqual(app.parse_srt_text(beside.read_text("utf-8"))[0]["start"], 25_000, delta=2)
        second = calibrate(31_500)                                             # 再校一次：还是这个文件
        self.assertEqual(second["sidecar"], "movie.chi.srt")
        cues = app.parse_srt_text(beside.read_text("utf-8"))
        self.assertAlmostEqual(cues[0]["start"], 31_500, delta=2)
        self.assertAlmostEqual(cues[-1]["start"] - cues[0]["start"], 28_000, delta=2)   # 只平移，间隔不变
        self.assertEqual(sorted(p.name for p in self.movies.iterdir()), ["movie.chi.srt", "movie.mkv"])   # 没有多出文件
        self.assertNotIn("subtitle_sidecar", app.PROCESSED["m"])
        body = {"name": "m", "subtitle_id": app.LOCAL_SUBTITLE_PREFIX + "../x.srt", "confirm_audio": True}
        with patch.object(app, "request", types.SimpleNamespace(get_json=lambda **kw: body, host="")):
            self.assertEqual(app.api_jellyfin_subtitle_candidate()[1], 409)     # 只认影片旁边的字幕

    def test_manual_first_line_keeps_the_stretch_found_automatically(self):
        # 自动算出要拉伸千分之一时，用户再手动改第一句的时间：只挪位置，拉伸要留着
        self.movies.mkdir(exist_ok=True)
        output = self.movies / "movie.mkv"; output.write_bytes(b"v")
        app.ITEMS["m"] = {"name": "m", "status": "done"}; app.PROCESSED["m"] = {"output": str(output)}
        import random
        rng = random.Random(3); t, lines = 20_000.0, []
        while t < 6_000_000:
            lines.append(t); t += rng.choice([1800, 2500, 3300, 4200, 6000, 9000, 15000])
        app.write_settings_json(app.subtitle_timing_path(output), {"tracks": [{"points": [[int(x), int(x) + 1500] for x in lines]}]})
        source = self.work / "c.srt"
        app.write_srt(source, [{"start": (x - 300) / 1.001, "end": (x - 300) / 1.001 + 1500, "text": f"句{i}"} for i, x in enumerate(lines)])
        preview = app.store_candidate_for_item("m", "c.srt", source.read_bytes(), auto=True)
        self.assertTrue(preview["confirmation_required"]); self.assertTrue(preview["automatic_available"])
        self.assertAlmostEqual(preview["scale"], 1.001, places=6)
        self.assertFalse(app.subtitle_sidecar_path(output).exists())               # 还没点应用，不写文件
        wanted = preview["automatic_first_start_ms"] + 2000                        # 用户把第一句再往后挪 2 秒
        applied = app.store_candidate_for_item("m", "c.srt", source.read_bytes(), confirm_audio=True, manual_first_start_ms=wanted)
        cues = app.parse_srt_text(app.subtitle_sidecar_path(output).read_text("utf-8"))
        self.assertAlmostEqual(cues[0]["start"], wanted, delta=2)
        self.assertAlmostEqual(applied["scale"], 1.001, places=6)
        self.assertAlmostEqual(cues[-1]["start"] - cues[0]["start"], lines[-1] - lines[0], delta=30)   # 拉伸还在

    def test_folder_picker_browses_any_folder(self):
        share = self.inbox.parent
        (share / "Films" / "4K").mkdir(parents=True); (share / "#recycle").mkdir(); (share / ".hidden").mkdir()
        root = str(share)
        def listing(path):
            with patch.object(app, "request", types.SimpleNamespace(args={"path": path})), patch.object(app, "SYSTEM_DIRS", set()):
                return app.api_folder_list()
        top = listing(root)
        self.assertEqual((top["path"], top["parent"], top["folders"]), (root, str(share.parent), ["Films", "download"]))   # 不列回收站、隐藏目录
        self.assertEqual(listing(root + "/Films/")["folders"], ["4K"])
        self.assertEqual(listing(root + "/download")["folders"], [])                  # 工作目录不让选
        for bad in (root + "/..", root + "/download/dvwork", root + "/.hidden", root + "/#recycle"):
            self.assertEqual(listing(bad)[1], 400, bad)
        self.assertEqual(listing(root + "/nope")[1], 404)
        with patch.object(app, "host_folder_names", lambda path: ["volume1", "volume2", "usr", "proc", "@eaDir"]), \
                patch.object(app, "request", types.SimpleNamespace(args={"path": ""})):
            self.assertEqual(app.api_folder_list(), {"path": "/", "parent": None, "folders": ["volume1", "volume2"]})   # 顶层不列系统目录
            with patch.object(app, "request", types.SimpleNamespace(args={"path": "/usr/lib"})):
                self.assertEqual(app.api_folder_list()[1], 400)

    def test_open_page_reloads_itself_after_an_upgrade(self):
        # NAS 实际出现过：服务已升级，浏览器里开着的旧页面没刷新，找不到新按钮
        served = app.index()
        self.assertNotIn("__APP_VERSION__", served)
        self.assertIn("const PAGE_VERSION='" + app.APP_VERSION + "';", served)
        self.assertEqual(len(app.APP_VERSION), 12)
        self.assertEqual(app.api_state()["app_version"], app.APP_VERSION)
        self.assertIn("state.app_version!==PAGE_VERSION", app.PAGE)
        self.assertIn("location.reload()", app.PAGE)
        self.assertIn("sessionStorage.getItem('reloadedFor')", app.PAGE)    # 同一版本只刷新一次，不会死循环

    def test_page_is_card_based(self):
        for marker in ('id="rows" class="cards"', "function cardBody", "function submitItem",
                       "selection={[item.name]", "全部保留", "直接入库", "取消排队",
                       "<b>现在</b>", "data-repreview=", "/api/repreview", "正在删除…",
                       "data-mkvupdate=", "/api/makemkv_update", "更新读盘工具"):
            self.assertIn(marker, app.PAGE)
        self.assertNotIn("不转会怎样", app.PAGE)
        for gone in ('id="go"', 'id="selectAll"', 'class="action-dock"', "<table>", "formatFlow(it.format_preview)"):
            self.assertNotIn(gone, app.PAGE)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class RealFfmpegRuleTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    tearDown = fixtures.PipelineTests.tearDown

    @staticmethod
    def streams(path):
        raw = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name:stream_tags=title,language:stream_disposition=default",
             "-of", "json", str(path)])
        return fixtures.json.loads(raw)["streams"]

    @staticmethod
    def digest(path, spec):
        return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", spec,
                                        "-c", "copy", "-f", "hash", "-hash", "sha256", "-"])

    def test_dts_file_keeps_one_audio_pair_and_the_chinese_subtitle(self):
        source = self.inbox / "movie.mkv"
        srt = self.work / "zh.srt"
        srt.write_text("1\n00:00:00,100 --> 00:00:00,800\n你好\n", "utf-8")
        made = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=s=64x64:r=24:d=1",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-i", str(srt), "-i", str(srt),
             "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "4:s", "-t", "1",
             "-c:v", "libx264", "-c:a:0", "dca", "-strict", "-2", "-c:a:1", "ac3", "-c:s", "srt",
             "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Main", "-disposition:a:0", "default",
             "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=Commentary",
             "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=chi", str(source)])
        if made.returncode != 0:
            self.skipTest("ffmpeg lacks DTS encoder")
        with patch.object(app, "disk_free", return_value=10**15):
            job = {"log": []}
            output = app.process_item(job, {"name": source.name, "kind": "file", "payload": str(source)})
        result = self.streams(output)
        self.assertEqual(output.suffix, ".mkv")
        self.assertEqual([(s["codec_type"], s["codec_name"]) for s in result],
                         [("video", "h264"), ("audio", "eac3"), ("audio", "dts")])       # 字幕不在影片文件里
        sidecar = output.with_name(output.stem + ".chi.srt")
        self.assertEqual(job["sidecars"], [str(sidecar)])
        self.assertIn("-->", sidecar.read_text("utf-8"))
        self.assertEqual([s["disposition"]["default"] for s in result if s["codec_type"] == "audio"], [1, 0])
        self.assertEqual(result[1]["tags"]["title"], "电视用")
        self.assertEqual(result[2]["tags"]["title"], "原始音质（电视播不了，留给功放或回音壁）")
        self.assertEqual(self.digest(source, "0:v:0"), self.digest(output, "0:v:0"))
        self.assertEqual(self.digest(source, "0:a:0"), self.digest(output, "0:a:1"))     # 原 DTS 逐字节未变
        self.assertEqual(job["verdict"], "should")
        self.assertTrue(source.exists())

    def test_saved_rules_drive_real_audio_and_subtitle_outputs(self):
        source = self.inbox / "settings.mkv"
        srt = self.work / "source.srt"
        srt.write_text("1\n00:00:00,100 --> 00:00:00,800\nSubtitle\n", "utf-8")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x64:r=24:d=1",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-i", str(srt),
                        "-map", "0:v", "-map", "1:a", "-map", "1:a", "-map", "2:s", "-map", "2:s", "-t", "1",
                        "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt", "-metadata:s:a:0", "language=jpn",
                        "-metadata:s:a:1", "language=eng", "-disposition:a:0", "0", "-disposition:a:1", "default",
                        "-metadata:s:s:0", "language=chi", "-metadata:s:s:1", "language=eng", str(source)], check=True)
        item = {"name": source.name, "kind": "file", "payload": str(source)}
        changes = {"pick.audio": {"action": "first"}, "audio.aac": {"action": "pair_ac3"},
                   "pick.subs": {"action": "eng"}, "subs.text": {"action": "embed"}}
        with RuleTests.request(self, {"rules": changes}):
            self.assertTrue(app.api_rules_save()["ok"])
        with patch.object(app, "disk_free", return_value=10**15):
            job = {"log": []}
            output = app.process_item(job, item)
            streams = self.streams(output)
            self.assertEqual([(s["codec_name"], s.get("tags", {}).get("language")) for s in streams[1:]],
                             [("ac3", "jpn"), ("aac", "jpn"), ("subrip", "eng")])
            self.assertEqual([s["disposition"]["default"] for s in streams[1:3]], [1, 0])
            self.assertEqual(job.get("sidecars", []), [])
            self.assertIn("添加 AC-3 并保留原轨", job["format_preview"]["target_detail"])
            self.assertEqual(self.digest(source, "0:a:0"), self.digest(output, "0:a:1"))
            self.assertEqual(self.digest(source, "0:v:0"), self.digest(output, "0:v:0"))
            with RuleTests.request(self, {"rules": {"pick.subs": {"action": "none"}, "audio.aac": {"action": "keep"}}}):
                self.assertTrue(app.api_rules_save()["ok"])
            job = {"log": []}
            selected = app.process_item(job, item)
            self.assertEqual([s["codec_name"] for s in self.streams(selected)], ["h264", "aac"])
            self.assertEqual(job["verdict"], "should")
            direct = app.process_item({"log": []}, {**item, "as_is": True})
            self.assertEqual(direct.read_bytes(), source.read_bytes())

    @unittest.skipUnless(shutil.which("dovi_tool") and shutil.which("mkvmerge"), "需要 dovi_tool 和 mkvmerge")
    def test_dv_mp4_changes_to_mkv_without_losing_p8_metadata(self):
        def run(*args):
            subprocess.run(args, cwd=self.work, check=True, capture_output=True)
        run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=128x72:r=24:d=1",
            "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params",
            "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:repeat-headers=1:aud=1",
            "-f", "hevc", "base.hevc")
        (self.work / "rpu.json").write_text(json.dumps({"cm_version": "V40", "length": 24}))
        run("dovi_tool", "generate", "-j", "rpu.json", "-o", "RPU.bin")
        run("dovi_tool", "inject-rpu", "-i", "base.hevc", "--rpu-in", "RPU.bin", "-o", "dv.hevc")
        run("mkvmerge", "-q", "-o", "dv.mkv", "--default-duration", "0:24fps", "dv.hevc")
        source = self.inbox / "source.mp4"
        run("ffmpeg", "-v", "error", "-i", "dv.mkv", "-f", "lavfi", "-i", "sine=r=48000:d=1",
            "-c:v", "copy", "-c:a", "ac3", "-tag:v", "hvc1", "-strict", "unofficial", str(source))
        before = app.summarize_probe(app.ffprobe(source))
        self.assertEqual(before["video"]["dv_profile"], 8)
        with RuleTests.request(self, {"rules": {"video.dv": {"action": "keep"}}}):
            self.assertTrue(app.api_rules_save()["ok"])
        with patch.object(app, "disk_free", return_value=10**15):
            job = {"log": []}
            output = app.process_item(job, {"name": source.name, "kind": "file", "payload": str(source)})
        self.assertEqual(output.suffix, ".mkv")
        self.assertEqual(job["verdict"], "should")
        def presentation_times(path):
            raw = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                           "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)])
            points = sorted(float(v) for v in raw.splitlines())
            return [round(t - points[0], 3) for t in points]
        self.assertEqual(presentation_times(source), presentation_times(output))
        after = app.summarize_probe(app.ffprobe(output))["video"]
        self.assertEqual([after[key] for key in ("dv_profile", "dv_compatibility_id", "dv_rpu_present", "dv_el_present")],
                         [8, 1, 1, 0])

    def test_lpcm_file_is_left_untouched(self):
        source = self.inbox / "movie.mkv"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=s=64x64:r=24:d=1",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=7.1",
                        "-t", "1", "-c:v", "libx264", "-c:a", "pcm_s24le", str(source)], check=True)
        with patch.object(app, "disk_free", return_value=10**15):
            job = {"log": []}
            output = app.process_item(job, {"name": source.name, "kind": "file", "payload": str(source)})
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.assertEqual(job["verdict"], "none")

    def test_mp4_mux_with_truehd_pair_and_text_subtitle_passes_self_check(self):
        source = self.inbox / "movie.mkv"
        srt = self.work / "zh.srt"
        srt.write_text("1\n00:00:00,100 --> 00:00:00,800\n你好\n", "utf-8")
        made = subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                               "-f", "lavfi", "-i", "testsrc2=s=64x64:r=24:d=1",
                               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1", "-i", str(srt),
                               "-t", "1", "-c:v", "libx264", "-c:a", "truehd", "-strict", "-2",
                               "-c:s", "srt", "-metadata:s:s:0", "language=chi", str(source)])
        if made.returncode != 0:
            self.skipTest("ffmpeg lacks TrueHD encoder")
        media = app.summarize_probe(app.ffprobe(source))
        output = self.work / "out.mp4"
        with patch.object(app, "disk_free", return_value=10**15):
            audio_count, sub_count, sidecars = app.ffmpeg_mp4_mux(
                {"log": []}, "封装MP4(保留DV)", source, media, output, None, media["duration"])
            app.verify_media_preserved(output, media, sub_count, expected_audio=audio_count)
        self.assertEqual((audio_count, sub_count, sidecars), (2, 0, [self.work / "out.chi.srt"]))
        self.assertIn("你好", sidecars[0].read_text("utf-8"))
        result = self.streams(output)
        self.assertEqual([s["codec_name"] for s in result], ["h264", "eac3", "truehd"])

    @unittest.skipUnless(shutil.which("mkvmerge"), "需要 mkvmerge")
    def test_raw_hevc_with_b_frames_keeps_presentation_order_in_mp4(self):
        source = self.work / "src.mkv"
        raw = self.work / "BL_P8.hevc"
        encoded = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc2=s=320x180:r=24000/1001:d=3", "-f", "lavfi", "-i", "sine=d=3",
             "-c:v", "libx265", "-x265-params", "bframes=4:log-level=error", "-c:a", "ac3", str(source)])
        if encoded.returncode != 0:
            self.skipTest("ffmpeg lacks libx265")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-map", "0:v:0", "-c", "copy",
                        "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(raw)], check=True)
        media = app.summarize_probe(app.ffprobe(source))
        output = self.work / "out.mp4"
        with patch.object(app, "disk_free", return_value=10**15):
            job = {"log": []}
            timed = app.rebuild_bl_timestamps(job, raw, media["video"]["r_frame_rate"], self.work)
            audio_count, sub_count, _ = app.ffmpeg_mp4_mux(job, "封装MP4(保留DV)", source, media, output,
                                                           None, media["duration"], bl_video=timed)
        self.assertFalse(raw.exists())
        rows = [line.split(",") for line in subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "packet=pts_time,dts_time,flags", "-of", "csv=p=0", str(output)]).decode().split()]
        self.assertFalse(any("D" in row[2] for row in rows), "不应有被丢弃的帧")
        self.assertTrue(any(row[0] != row[1] for row in rows), "B 帧显示时间应不同于解码时间")
        app.verify_media_preserved(output, media, sub_count, expected_audio=audio_count)


if __name__ == "__main__":
    unittest.main()
