"""只用于本地视觉回归，不会连接 NAS 或执行媒体任务。"""
import json
import sys
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
sys.modules["flask"] = fake_flask
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


GB = 1024 ** 3


def track(codec, lang, channels=6, title="", default=False, profile=""):
    return {"codec": codec, "lang": lang, "channels": channels, "title": title,
            "default": default, "profile": profile}


def subtitle(codec, lang, title=""):
    return {"codec": codec, "lang": lang, "title": title, "forced": False, "default": False}


def media(codec="h264", dv=0, width=1920, height=1080, transfer="", audio=(), subs=()):
    return {
        "video": {"codec": codec, "dv_profile": dv, "hdr": bool(transfer), "dynamic_range_known": True,
                  "color_transfer": transfer, "width": width, "height": height, "pix_fmt": "yuv420p10le",
                  "r_frame_rate": "24000/1001"},
        "video_count": 1, "audio": list(audio), "subs": list(subs), "attachments": 0, "duration": 6356,
    }


def ready(name, kind, size, probe, payload=None, **extra):
    payload = payload or "/share/download/" + name
    item = {
        "name": name, "kind": kind, "size": size, "size_h": app.human(size), "status": "ready",
        "clean": False, "error": None, "warnings": [], "selectable": True, "deletable": False,
        "retryable": False, "subtitle_candidate": False, "preview_status": "ready" if kind in app.DISC_KINDS else None,
        "plan": app.build_plan(kind, probe, payload)[0],
        "card": app.conversion_card(kind, probe, payload, size),
        "optional": app.processing_optional(kind, probe, payload),
    }
    item.update(extra)
    item.setdefault("discardable", item["status"] in app.DISCARDABLE)
    return item


UHD_P7 = media("hevc", dv=7, width=3840, height=2160, transfer="smpte2084",
               audio=[track("truehd", "eng", 8, "TrueHD Atmos 7.1", True), track("ac3", "eng", 6),
                      track("ac3", "chi", 6, "国语"), track("ac3", "eng", 2, "Director Commentary")],
               subs=[subtitle("hdmv_pgs_subtitle", "eng"), subtitle("hdmv_pgs_subtitle", "chi", "简体中文"),
                     subtitle("hdmv_pgs_subtitle", "chi", "繁體中文")])
EVA = media(audio=[track("dts", "jpn", 6, "DTS-HD MA 5.1", True, "DTS-HD MA"), track("dts", "jpn", 7, "DTS-ES 6.1"),
                   track("flac", "jpn", 2), track("dts", "eng", 6, "DTS-HD MA 5.1", profile="DTS-HD MA")],
            subs=[subtitle("hdmv_pgs_subtitle", "eng"), subtitle("hdmv_pgs_subtitle", "chi", "Chinese (Traditional)"),
                  subtitle("subrip", "eng")])
WEBDL = media("hevc", dv=8, width=3840, height=2160, transfer="smpte2084",
              audio=[track("eac3", "eng", 6, "Atmos", True)], subs=[subtitle("subrip", "chi", "简体中文")])
SET = "[BDMV][230308][Shin Evangelion Gekijouban][Movie]"

STATE = {
    "items": [
        ready(SET + "/UHD", "bdmv", 77 * GB, UHD_P7, "/share/download/" + SET + "/UHD"),
        ready(SET + "/Main Disc", "bdmv", 45 * GB, None, preview_status="failed", plan=[],
              preview_error=app.MAKEMKV_OUTDATED),
        ready(SET + "/Special Disc", "bdmv", 28 * GB, None, preview_status="no_feature", selectable=False,
              preview_error=app.NO_FEATURE_REASON, plan=[]),
        ready("The.End.of.Evangelion.1997.1080p.REMUX.mkv", "file", int(25.6 * GB), EVA,
              status="processing", selectable=False),
        ready("Project.Hail.Mary.2160p.WEB-DL.DV.mp4", "file", 27 * GB, WEBDL),
        ready("Some.Movie.2160p.mkv", "file", 40 * GB, EVA, status="queued", selectable=False),
        ready("Downloading.Movie.mkv", "file", 12 * GB, None, status="growing", selectable=False, plan=[]),
        {
            "name": "Evangelion.3.33.iso", "kind": "iso", "size": 58 * GB, "size_h": "54.2GB",
            "status": "done", "clean": False, "error": None,
            "warnings": ["提取出的轨道和预读时不一致，已改用自动预选，请验片"],
            "selectable": False, "deletable": True, "retryable": True,
            "output_name": "Evangelion.3.33.mkv", "output_size_h": "31.2GB", "done_ts": "11-17 00:23",
            "subtitle_candidate": False, "plan": ["MakeMKV提取正片"], "card": None, "done_verdict": "must",
            "sidecar_count": 0, "delete_block_reason": "",
        },
        {
            "name": "影片库/The.End.of.Evangelion.1997.mkv", "kind": "library", "size": 24 * GB, "size_h": "22.7GB",
            "status": "done", "clean": True, "error": None, "warnings": [], "library": True,
            "selectable": False, "deletable": False, "retryable": False, "discardable": False,
            "output_name": "The.End.of.Evangelion.1997.mkv", "output_size_h": "22.7GB",
            "subtitle_candidate": False, "plan": [], "card": None, "sidecar_count": 0, "delete_block_reason": "",
            "info": {"picture": "MKV · 1920×1080 · 23.976 fps · SDR",
                     "audio": "日语 · 环绕声 · 普通音质；日语 · 环绕声 · 最高音质（电视播不了）", "subs": "外挂 1 条，影片里 1 条"},
        },
    ],
    "job": {"name": "The.End.of.Evangelion.1997.1080p.REMUX.mkv", "phase": "正在转换", "percent": 42.3,
            "elapsed": 310, "eta_sec": 420, "eta_clock": "16:52", "step_idx": 1,
            "steps": ["检查空间", "转换并封装", "自检", "移入 Movies"], "log": ["16:40:01 开始", "16:40:02 转换中"]},
    "queue": ["Some.Movie.2160p.mkv"],
    "folders": {"inbox": "download", "library": "Movies"},
    "upgrade": {"status": "idle", "message": "当前已经是运行中的版本"},
    "subtitle_ai": {"configured": False, "endpoint": "", "model": ""},
    "disk": {"free": 123 * GB, "total": 7000 * GB, "free_h": "123.1GB", "total_h": "7.0TB",
             "pct_used": 98.3, "low": False},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            body = json.dumps(dict(STATE, release=app.APP_RELEASE, app_version=app.APP_VERSION), ensure_ascii=False).encode()
            content_type = "application/json"
        elif self.path.startswith("/api/folder_list"):
            from urllib.parse import urlparse, parse_qs
            where = "/" + (parse_qs(urlparse(self.path).query).get("path") or [""])[0].strip("/")
            tree = {"/": ["volume1", "volume2"], "/volume1": ["video", "music"], "/volume2": ["Share", "docker"],
                    "/volume2/Share": ["Movies", "download", "电视剧"], "/volume2/Share/download": ["complete", "incomplete"]}
            body = json.dumps({"path": where, "parent": None if where == "/" else (where.rpartition("/")[0] or "/"),
                               "folders": tree.get(where, [])}, ensure_ascii=False).encode()
            content_type = "application/json"
        elif self.path == "/api/rules":
            body = json.dumps(app.settings_view(), ensure_ascii=False).encode()
            content_type = "application/json"
        elif self.path.startswith("/api/jellyfin_subtitles?"):
            body = json.dumps({
                "ok": True,
                "subtitles": [
                    {
                        "id": "recent-0",
                        "filename": (
                            "Project.Hail.Mary.2026.2160p.iT.WEB-DL.H265.DV."
                            "HDR.DDP5.1.Atmos-BiVerse@ADWeb.zho.0.srt"
                        ),
                        "size_h": "114.0KB",
                        "modified": "07-26 15:22",
                        "origin": "从网上下载的",
                    },
                    {
                        "id": "recent",
                        "filename": (
                            "Project.Hail.Mary.2026.2160p.iT.WEB-DL.H265.DV."
                            "HDR.DDP5.1.Atmos-BiVerse@ADWeb.zho.srt"
                        ),
                        "size_h": "107.0KB",
                        "modified": "07-26 15:20",
                        "origin": "从网上下载的",
                    },
                    {
                        "id": "cache-2",
                        "filename": "2.srt",
                        "size_h": "104.0KB",
                        "modified": "07-20 16:49",
                        "origin": "校准后的",
                    },
                ],
            }, ensure_ascii=False).encode()
            content_type = "application/json"
        else:
            body = app.index().encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        fake = {
            "/api/subtitles/list": {"ok": True, "title": "福音战士新剧场版：终", "recognised": True, "embedded": 1, "existing": [
                {"index": 2, "filename": "Shin Evangelion Gekijouban.chi.srt", "language": "中文", "sync_id": "abc", "beside_movie": False},
                {"index": 3, "filename": "Shin Evangelion Gekijouban.chi.0.srt", "language": "中文", "sync_id": "abd", "beside_movie": False}]},
            "/api/subtitles/search": {"ok": True, "results": [
                {"id": "c", "name": "Shin.Evangelion.Gekijouban.EVANGELION.3.0.1.11.THRICE.UPON.A.TIME.2023.COMPLETE.BLURAY-GROUP",
                 "downloads": 1092, "format": "srt", "tags": ["片源版本一致"], "recommended": True},
                {"id": "a", "name": "Evangelion.3.0+1.01.Thrice.Upon.a.Time.2021.1080p.AMZN.WEB-DL.DD+.5.1.H.264-GROUP",
                 "downloads": 3912, "format": "srt", "tags": ["片源版本不同，时间轴可能不符"], "recommended": False},
                {"id": "b", "name": "新世纪福音战士新剧场版 4：终 (2021) 1080p AAC", "downloads": 1522, "format": "srt", "tags": [], "recommended": False}]},
            "/api/subtitles/download": {"ok": True, "message": "已下载，可在电视上选择"},
            "/api/subtitles/delete": {"ok": True, "message": "已删除"},
        }
        if self.path in fake:
            body = json.dumps(fake[self.path], ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/subtitle_candidate/jellyfin":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if payload.get("confirm_audio") is True:
                result = {
                    "ok": True,
                    "cue_count": 1888,
                    "filename": "Project.Hail.Mary.zho.0.srt",
                    "applied": True,
                    "alignment_method": "audio",
                    "offset_seconds": (
                        (payload.get("manual_first_start_ms", 1_094_719)
                         - 1_094_719) / 1000
                    ),
                    "sidecar": "Evangelion.3.33.zh-CN.srt",
                    "message": (
                        "已按手动校轴生成外置中文字幕"
                        if payload.get("manual_first_start_ms") is not None
                        else "音频校轴已确认，外置中文字幕已生成"
                    ),
                }
            else:
                result = {
                    "ok": True,
                    "cue_count": 1888,
                    "filename": "Project.Hail.Mary.zho.0.srt",
                    "confirmation_required": True,
                    "alignment_method": "audio",
                    "offset_seconds": -1092.22,
                    "scale": 1.042709,
                    "automatic_available": True,
                    "automatic_suspicious": True,
                    "source_first_start_ms": 1_094_719,
                    "automatic_first_start_ms": 2_499,
                    "preview_cues": [
                        {
                            "start_ms": 2499,
                            "end_ms": 6555,
                            "text": "这里是第一句中文字幕",
                        },
                        {
                            "start_ms": 6701,
                            "end_ms": 10382,
                            "text": "这里是第二句中文字幕",
                        },
                    ],
                    "source_preview_cues": [
                        {
                            "start_ms": 1_094_719,
                            "end_ms": 1_098_775,
                            "text": "这里是第一句中文字幕",
                        },
                        {
                            "start_ms": 1_098_921,
                            "end_ms": 1_102_602,
                            "text": "这里是第二句中文字幕",
                        },
                    ],
                    "message": "没有日文 OCR 锚点；请确认音频校轴结果",
                }
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/subtitle_candidate/remove":
            body = json.dumps({
                "ok": True,
                "removed": ["candidate.zho.srt", "Evangelion.3.33.zh-CN.srt"],
                "message": "已移除所选中文字幕；视频文件没有改动",
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
