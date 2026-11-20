# -*- coding: utf-8 -*-
"""
TV转码：把下载的影片（文件、蓝光原盘）整理成电视经 Jellyfin 能直接播放的格式。

下载文件夹 → 读取影片信息 → 按规则给出“现在 / 转换后 / 代价” → 转换 → 影片文件夹。
画面始终原样复制，不改分辨率、帧率和动态范围；只调整文件格式、音轨和字幕的放法。
默认规则来自 LG G5 + Jellyfin 的实测，可在设置页修改。

单文件应用：页面（HTML/CSS/JS）和构建镜像用的 Dockerfile 都内嵌在本文件里。
运行在 Docker 容器内，媒体共享文件夹挂载到 /share。
"""
import json
import io
import ipaddress
import hashlib
import csv
import math
import os
os.environ.setdefault("TZ", "Asia/Shanghai")
import re
import selectors
import sys
import shutil
import signal
import stat
import statistics
import subprocess
import tarfile
import threading
import time
import urllib.error as urlerror
import urllib.parse as urlparse
import urllib.request as urlrequest
import uuid
from contextvars import ContextVar
from bisect import bisect_left
from fractions import Fraction
from functools import wraps
try:
    time.tzset()   # 让 TZ 立即生效(仅Unix)
except AttributeError:
    pass
import traceback
from pathlib import Path

from flask import Flask, jsonify, request

# ---------- 路径与常量 ----------
SHARE = Path("/share")
FOLDERS_FILE_NAME = ".tv-transcode.json"         # 放在共享根目录，记着“下载文件夹”和“影片文件夹”选的是哪两个
DEFAULT_FOLDERS = {"inbox": "download", "library": "Movies"}
WORK_NAME = "dvwork"


# 选文件夹时不让进的系统目录（只在最顶层判断）
SYSTEM_DIRS = {"proc", "sys", "dev", "run", "etc", "bin", "sbin", "lib", "lib32", "lib64", "usr", "var", "tmp", "boot",
               "root", "opt", "srv", "lost+found", "initrd", "config", "host", "app", "snap"}


def folder_path(text):
    """设置里记的位置 → 面板里的路径。旧版记的是共享目录下的相对位置（如 download），新版记完整路径。"""
    path = Path(str(text or ""))
    return path if path.is_absolute() else SHARE / path


def folder_name_problem(text):
    """一个文件夹位置写得对不对（不管它存不存在）；没问题返回 None。"""
    text = str(text or "")
    parts = [part for part in Path(text).parts if part != "/"]
    if not parts or text != str(Path(text)) or any(part in ("..", ".") or part.startswith((".", "#", "@")) for part in parts):
        return "的位置无效"
    if WORK_NAME in parts:
        return "不能选在工作目录里"
    if Path(text).is_absolute() and parts[0] in SYSTEM_DIRS:
        return "不能选在系统目录里"
    return None


def folders_overlap(a, b):
    a, b = Path(a), Path(b)
    return a == b or a in b.parents or b in a.parents


def folder_choice_problem(inbox, library, must_exist=True):
    """检查选的两个文件夹能不能用；能用返回 None，否则返回一句原因。位置是面板里的路径（旧版的相对位置也认）。"""
    picked = {}
    for key, raw in (("下载文件夹", inbox), ("影片文件夹", library)):
        problem = folder_name_problem(raw)
        if problem:
            return key + problem
        location = folder_path(raw)
        if must_exist and not location.is_dir():
            return f"{key}不存在：{raw}"
        picked[key] = location.resolve()
        if picked[key] == SHARE.resolve():
            return f"{key}不能是整个共享目录"
    if folders_overlap(picked["下载文件夹"], picked["影片文件夹"]):
        return "下载文件夹和影片文件夹不能是同一个，也不能一个套着另一个"
    return None


def load_folders():
    """启动时读一次。没设过、文件坏了或选的位置不合法时，用默认的 download 和 Movies。"""
    chosen = dict(DEFAULT_FOLDERS)
    try:
        saved = json.loads((SHARE / FOLDERS_FILE_NAME).read_text("utf-8"))
        candidate = {key: str(saved.get(key) or "").strip() for key in DEFAULT_FOLDERS}
        candidate = {key: value if value.startswith("/") else value.strip("/") for key, value in candidate.items()}
        if not folder_choice_problem(candidate["inbox"], candidate["library"], must_exist=False):
            chosen = candidate
    except (OSError, ValueError, AttributeError):
        pass
    return chosen


FOLDERS = load_folders()
INBOX = folder_path(FOLDERS["inbox"])    # 下载文件夹：新下载的影片放这里
WORK = INBOX / WORK_NAME            # 施工区：下载文件夹里的子目录，扫描时跳过
MOVIES = folder_path(FOLDERS["library"])  # 影片文件夹：转好的成品放这里，Jellyfin 读这里
STATE_FILE = WORK / ".panel_state.json"
UPGRADE_STATUS_FILE = WORK / ".upgrade_status.json"
DISC_PREVIEW_CACHE_FILE = WORK / ".disc_preview_cache.json"

def env_text(name, default=""):
    return os.environ.get(name, "").strip() or default


def env_number(name, default):
    try:
        return int(env_text(name) or default)
    except ValueError:
        return default


# 部署时可以用环境变量改的东西；不设就是下面的默认值
PANEL_CONTAINER = env_text("PANEL_CONTAINER", "mediapanel")     # 面板自己的容器名（升级、换文件夹时要找到自己）
PANEL_PORT = env_number("PORT", 8097)
# 镜像里自带一份 dovi_tool；也可以用 DOVI_TOOL 指到别的位置
BUNDLED_DOVI_TOOL = "/opt/tvtranscode/dovi_tool"
DOVI_TOOL = env_text("DOVI_TOOL") or (BUNDLED_DOVI_TOOL if os.path.exists(BUNDLED_DOVI_TOOL) else "dovi_tool")
MAKEMKV_CONTAINER = env_text("MAKEMKV_CONTAINER", "makemkv")
MAKEMKVCON = "/opt/makemkv/bin/makemkvcon"
JELLYFIN_CONTAINER = env_text("JELLYFIN_CONTAINER", "jellyfin")
JELLYFIN_SUBTITLE_ROOTS = (
    "/config/metadata/library",
    "/config/data/subtitles",
)

VIDEO_EXT = {".mkv", ".mp4", ".m2ts", ".mts", ".m2t", ".ts", ".mov", ".avi"}
DISC_IMAGE_EXT = {".iso", ".img"}
DISC_KINDS = {"bdmv", "iso"}
# ---- 电视实测支持表（LG G5 + Jellyfin 10.11.11 + Jellyfin for WebOS 1.2.2，2026-11-16/17）----
# 以 Jellyfin 服务器日志为准。规则只依据这张表：
#   直接播放：EAC3、LPCM；内嵌文本字幕；HDR10(MKV/MP4)；杜比视界 MP4；4K 高码率 MKV。
#   电视解不了（服务器现转成 AAC 640k）：DTS、DTS-HD MA、TrueHD、FLAC。
#   杜比视界放 MKV 不稳定，成品必须用 MP4。
#   图形字幕 PGS：电视端“实验性 PGS 字幕渲染”开启时由电视自己画，不转码；
#     MKV 里内嵌、MP4 旁边放同名外挂 .sup 都实测可用，因此不再需要 OCR。
TV_DIRECT_AUDIO = {"aac", "ac3", "eac3", "mp3"}       # AC3/AAC/MP3 未单独实测，按常识视为可直放
MP4_ORIGINAL_AUDIO = {"truehd", "dts", "flac"}   # 能原样放进 MP4 的“电视解不了”的音轨
PGS_CODEC = "hdmv_pgs_subtitle"
GRAPHIC_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
AUDIO_TRANSCODE = {"truehd", "dts", "mlp", "flac"}     # 旧名保留：电视解不了、需要 EAC3 兼容轨的常见编码
MP4_AUDIO_COPY = {"aac", "ac3", "eac3", "mp3"}      # LG/webOS 可在 MP4 中稳妥直放
SUB_STRIP = {"hdmv_pgs_subtitle", "dvd_subtitle"}    # 需剥离的图形字幕
# 成品是 MKV 时，转 EAC3 的同时把原始 TrueHD/DTS 音轨也留下：EAC3 排在前面作为
# 电视一定能播的兼容轨，原轨给能解码的电视/功放用，音质不再有损失。
KEEP_ORIGINAL_AUDIO = True
ORIGINAL_AUDIO_KEEP_CODECS = {"truehd", "dts", "mlp", "flac"}   # 仅为兼容旧引用，规则见 kept_original_tracks
# MP4 成品（杜比视界、原盘 H.264/HEVC）也保留原轨。2026-11-16 在 LG G5 +
# Jellyfin 10.11.11 上用合成样片实测：杜比视界 MP4 里多一条 TrueHD 或 DTS，
# 默认播放（画面、杜比视界标志、EAC3 兼容轨）不受影响，手动选原轨也能出声。
# 若换了电视或 Jellyfin 版本后默认播放出问题，把这里改回 False。
KEEP_ORIGINAL_AUDIO_IN_MP4 = True
# ---------- 转换规则：一行一条“遇到什么 → 怎么处理”，页面上可改 ----------
# 默认值就是在 LG G5 上实测后定下的做法；备注写的是为什么这么做。别的电视不一样，改这里即可。
AUDIO_ACTIONS = [
    ("keep", "不转换"),
    ("pair", "转换：新增 E-AC-3，保留原音轨"),
    ("replace", "转换：替换为 E-AC-3"),
    ("pair_ac3", "转换：新增 AC-3，保留原音轨"),
    ("replace_ac3", "转换：替换为 AC-3"),
]
RULE_ROWS = [
    ("video.dv", "画面", "杜比视界影片的格式", [("mp4", "MP4"), ("keep", "MKV")], "mp4",
     "电视可点亮 MKV 中的杜比视界时选 MKV。"),
    ("video.dv7", "画面", "双层杜比视界（Profile 7）", [("convert", "转换为 Profile 8.1"), ("keep", "保留 Profile 7")], "convert",
     "电视支持双层杜比视界时选保留。转换尚未经真实片源验证。"),
    ("audio.dts", "声音", "DTS", AUDIO_ACTIONS, "pair", "包括 DTS Core 和 DTS-ES。"),
    ("audio.dtshd", "声音", "DTS-HD", AUDIO_ACTIONS, "pair", "包括 DTS-HD MA 和 DTS-HD HRA。"),
    ("audio.truehd", "声音", "Dolby TrueHD", AUDIO_ACTIONS, "pair", "包括带 Atmos 元数据的 TrueHD 音轨。"),
    ("audio.mlp", "声音", "MLP", AUDIO_ACTIONS, "pair", "MP4 输出只保留兼容轨。"),
    ("audio.flac", "声音", "FLAC", AUDIO_ACTIONS, "pair", ""),
    ("audio.pcm", "声音", "LPCM", AUDIO_ACTIONS, "keep", ""),
    ("audio.eac3", "声音", "E-AC-3", AUDIO_ACTIONS, "keep", "包括 Dolby Digital Plus Atmos。"),
    ("audio.ac3", "声音", "AC-3", AUDIO_ACTIONS, "keep", ""),
    ("audio.aac", "声音", "AAC", AUDIO_ACTIONS, "keep", ""),
    ("audio.mp3", "声音", "MP3", AUDIO_ACTIONS, "keep", ""),
    ("audio.other", "声音", "其他音频编码", AUDIO_ACTIONS, "pair", "匹配上方未列出的编码。"),
    ("subs.text", "字幕", "文字字幕（SRT、ASS、SSA）",
     [("sidecar", "转换：导出为外挂文件"), ("embed", "不转换：保留内嵌")], "sidecar", ""),
    ("subs.graphic", "字幕", "图片字幕（PGS）",
     [("sidecar", "转换：导出为外挂文件"), ("embed", "不转换：保留内嵌")], "sidecar", ""),
    ("pick.audio", "默认保留", "默认音轨",
     [("quality", "原语言中音质最高"), ("default", "片源标记的默认音轨"), ("first", "第一条正片音轨")], "quality",
     "原语言取自片源标记的默认音轨，无标记时取第一条。"
     "音质排序：TrueHD > DTS-HD MA > LPCM > FLAC > DTS-HD HRA > DTS > E-AC-3 > AC-3 > AAC，同级取声道数多者。不选评论音轨。"),
    ("pick.subs", "默认保留", "默认字幕语言",
     [("chi", "中文"), ("eng", "英语"), ("jpn", "日语"), ("none", "不选字幕")], "chi",
     "选择一条匹配语言的字幕，没有匹配时留空。"),
]
# 每条规则默认为什么这么做：写的是在 LG G5 上的实测依据，设置页上跟在规则旁边显示。
_REASON_PAIR = "LG G5 不支持，直接播放时由 Jellyfin 实时转码，音质下降。预先生成兼容音轨，原始音轨留给功放或回音壁。"
_REASON_KEEP = "LG G5 实测可直接播放。"
_REASON_GUESS = "未单独实测，通用支持。"
RULE_REASONS = {
    "video.dv": "LG G5 实测：MKV 中的杜比视界偶尔无法点亮，MP4 稳定。",
    "video.dv7": "LG G5 不支持双层杜比视界。转换仅移除增强层。",
    "audio.dts": _REASON_PAIR, "audio.dtshd": _REASON_PAIR, "audio.truehd": _REASON_PAIR,
    "audio.mlp": "与 TrueHD 同类，未单独实测，按不支持处理。", "audio.flac": _REASON_PAIR,
    "audio.pcm": _REASON_KEEP, "audio.eac3": _REASON_KEEP,
    "audio.ac3": _REASON_GUESS, "audio.aac": _REASON_GUESS, "audio.mp3": _REASON_GUESS,
    "audio.other": "未实测，按不支持处理。",
    "subs.text": "内嵌字幕需 Jellyfin 读完整个文件才显示（24GB 实测 2 分 34 秒）；外挂即时显示。",
    "subs.graphic": "同上。需在电视端 Jellyfin 开启“实验性 PGS 字幕渲染”。",
    "pick.audio": "每部影片保留一条音轨。",
    "pick.subs": "每部影片保留一条字幕。",
}
RULE_FIXED = [
    ("画面", "视频编码", "复制原始视频流", "保留源分辨率和帧率。"),
    ("画面", "蓝光原盘", "提取正片", "提取后应用视频、音轨和字幕规则。"),
    ("画面", "非杜比视界视频", "重新封装时使用 MKV", "原样入库时保留源容器。"),
]
# 旧面板会把系统说明作为用户备注一起保存。读取旧配置时只隐藏这些原文。
LEGACY_SYSTEM_NOTES = {
    'LG G5 实测：MKV 中的杜比视界偶尔无法点亮，MP4 稳定。',
    'LG G5 不认双层的杜比视界。转换只去掉电视用不上的增强层。',
    'LG G5 放不了这种声音，直接播的话 Jellyfin 会现场转成更差的音质。原来的留给以后的回音壁。',
    'LG G5 能直接播放。MP4 装不下它，进 MP4 时会换成 E-AC-3。',
    'LG G5 能直接播放。',
    '未单独实测，通用支持。',
    '未实测，按不支持处理。',
    '内嵌字幕需 Jellyfin 读完整个文件才显示（24GB 实测 2 分 34 秒）；外挂即时显示。',
    '同上。需在电视端 Jellyfin 开启“实验性 PGS 字幕渲染”。',
    '每部片默认只留一条字幕，和一条原语言里音质最好的声音；转换前可以在卡片上改。',
}
RULE_GROUPS = ["画面", "声音", "字幕", "默认保留"]
RULE_DEFAULTS = {row[0]: row[4] for row in RULE_ROWS}
RULES_FILE = ".rules.json"
_RULES_CACHE = {"stamp": None, "data": {}}


def _saved_rules():
    path = WORK / RULES_FILE
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return {}
    if _RULES_CACHE["stamp"] != stamp:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        # 旧版本把 MLP 与 TrueHD 合并设置。首次读取时继承原选择，以后两者可以分别修改。
        if "audio.mlp" not in data and isinstance(data.get("audio.truehd"), dict):
            data["audio.mlp"] = dict(data["audio.truehd"])
        _RULES_CACHE.update(stamp=stamp, data=data)
    return _RULES_CACHE["data"]


_ACTIVE_RULES = ContextVar("active_rules", default=None)
RULE_OPTIONS = {row[0]: dict(row[3]) for row in RULE_ROWS}


def with_rule_snapshot(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if _ACTIVE_RULES.get() is not None:
            return func(*args, **kwargs)
        token = _ACTIVE_RULES.set(_saved_rules())
        try:
            return func(*args, **kwargs)
        finally:
            _ACTIVE_RULES.reset(token)
    return wrapped


def rule(rule_id):
    """这条规则现在选的处理方式；没改过或存的值无效时用默认。"""
    values = _ACTIVE_RULES.get()
    saved = (values if values is not None else _saved_rules()).get(rule_id)
    action = saved.get("action") if isinstance(saved, dict) else None
    return action if isinstance(action, str) and action in RULE_OPTIONS[rule_id] else RULE_DEFAULTS[rule_id]


TESTED_VIDEO_CODECS = {"h264", "hevc"}        # 在 LG G5 上实测能直接播放的画面编码
VIDEO_CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "vc1": "VC-1", "mpeg2video": "MPEG-2", "av1": "AV1",
                     "vp9": "VP9", "mpeg4": "MPEG-4", "msmpeg4v3": "MPEG-4", "wmv3": "WMV", "mpeg1video": "MPEG-1"}
OTHER_FILE_FORMATS = "M2TS、MTS、M2T、TS、MOV、AVI"       # 面板认的视频文件里，MKV、MP4 之外的全部
DISC_FORMATS = "蓝光原盘（BDMV 文件夹、ISO、IMG）"


def video_codec_label(codec):
    codec = str(codec or "").lower()
    return VIDEO_CODEC_NAMES.get(codec, codec.upper() or "未知")


def video_matrix(dv, p7):
    """文件格式规则逐行列出来：片源 / 文件格式 / 处理 / 成品格式 / 备注。用的是转换流程里的同一套判断。

    每一行都写实际结果，不写“其余”“同上”。处理只有“转换”“不转换”两种。
    """
    rows = []
    for name, profile in (("双层杜比视界（Profile 7）", 7), ("其他杜比视界", 8), ("HDR10、SDR", 0)):
        target = output_container(profile, dv, p7)
        converting = converts_p7(profile, p7)
        picture = "杜比视界转为 Profile 8.1" if converting else ""
        for source, label in (("mkv", "MKV"), ("mp4", "MP4"), ("other", OTHER_FILE_FORMATS), ("disc", DISC_FORMATS)):
            convert = source in ("other", "disc") or converting or bool(profile and source != target)
            note = picture
            if convert:
                result = target.upper()
            else:
                result = source.upper()
                if not profile and source == "mp4":
                    # 这一行本身不转换；但声音页或字幕页的规则要转换这部片时，成品会是 MKV
                    note = "因音轨或字幕转换时，成品为 MKV"
            rows.append([name, label, "转换" if convert else "不转换", result, note, f"note.video.{profile}.{source}"])
    return rows


# 画面编码表和上面的文件格式表同一种列：片源 / 画面编码 / 处理 / 成品编码 / 备注
VIDEO_CODEC_ROWS = [
    ["所有影片", "H.264、HEVC", "不转换", "和片源相同", "LG G5 实测可播放", "note.codec.tested"],
    ["所有影片", "H.264、HEVC 之外的所有编码", "不转换", "和片源相同", "未在 LG G5 上验证", "note.codec.other"],
]
# 字幕页里固定不带进成品的那一行
SUBTITLE_FIXED_ROWS = [
    ["图片字幕（DVD、DVB、XSUB）", "丢弃", "无", "无", "电视不支持", "note.subs.unsupported"],
]
TABLE_NOTE_IDS = ({f"note.video.{profile}.{source}" for profile in (7, 8, 0) for source in ("mkv", "mp4", "other", "disc")}
                  | {row[5] for row in VIDEO_CODEC_ROWS} | {row[5] for row in SUBTITLE_FIXED_ROWS})


def saved_table_notes():
    notes = _saved_rules().get("_notes")
    return {key: str(value) for key, value in notes.items() if key in TABLE_NOTE_IDS} if isinstance(notes, dict) else {}


def rules_view():
    saved = _saved_rules()
    rows = []
    for rule_id, group, label, options, default, note in RULE_ROWS:
        mine = saved.get(rule_id) if isinstance(saved.get(rule_id), dict) else {}
        # 备注默认就是“为什么这么设”（LG G5 的实测依据）；用户改过就显示用户写的
        reason = RULE_REASONS.get(rule_id, "")
        personal_note = str(mine.get("note") or "")
        if not personal_note or personal_note in LEGACY_SYSTEM_NOTES or personal_note == note:
            personal_note = reason
        rows.append({"id": rule_id, "group": group, "label": label, "action": rule(rule_id), "default": default,
                     "options": [{"value": value, "label": text, "outputs": rule_outputs(rule_id, value)}
                                 for value, text in options],
                     "note": personal_note, "description": note, "default_note": reason,
                     "reason": reason})
    fixed = [{"group": g, "label": l, "action": a, "note": n} for g, l, a, n in RULE_FIXED]
    # 两个视频选项共同决定结果。预先算出四种组合，编辑中的预览无需写配置。
    video_outputs = {dv: {p7: video_matrix(dv, p7) for p7 in ("keep", "convert")} for dv in ("keep", "mp4")}
    # 表格行的备注也走“草稿 → 保存”那一套：每行给一个只有备注的条目（note_only）
    overrides = saved_table_notes()
    for note_id in sorted(TABLE_NOTE_IDS):
        rows.append({"id": note_id, "group": "字幕" if note_id.startswith("note.subs") else "画面", "label": "备注",
                     "action": "", "default": "", "options": [], "note_only": True,
                     "note": overrides.get(note_id, ""), "has_note": note_id in overrides,
                     "description": "", "default_note": "", "reason": ""})
    return {"rows": rows, "fixed": fixed, "groups": RULE_GROUPS, "video_outputs": video_outputs,
            "video_codecs": VIDEO_CODEC_ROWS, "subtitle_fixed": SUBTITLE_FIXED_ROWS,
            "customized": any(r["action"] != r["default"] for r in rows if not r.get("note_only"))}


def write_settings_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def save_rules(changes, directory=None, notes=None):
    """notes：表格行的备注（编号 → 文字）。不传就保留已有的。"""
    path = (directory or WORK) / RULES_FILE
    kept_notes = saved_table_notes() if notes is None else {
        key: str(value)[:300] for key, value in notes.items() if key in TABLE_NOTE_IDS}
    if changes is None:
        path.unlink(missing_ok=True)
    else:
        if not isinstance(changes, dict):
            raise ValueError("请提交有效的规则")
        data = {}
        for key, entry in changes.items():
            if key not in RULE_OPTIONS or not isinstance(entry, dict):
                raise ValueError("无法识别这条规则")
            action = entry.get("action")
            if not isinstance(action, str) or action not in RULE_OPTIONS[key]:
                raise ValueError("处理方式无效")
            data[key] = {"action": action, "note": str(entry.get("note") or "")[:300]}
        if kept_notes:
            data["_notes"] = kept_notes
        write_settings_json(path, data)
    _RULES_CACHE.update(stamp=None, data={})


def audio_rule_id(codec, profile=""):
    codec = (codec or "").lower()
    if codec == "dts":
        return "audio.dtshd" if "HD" in str(profile or "").upper() else "audio.dts"
    if codec.startswith("pcm_"):
        return "audio.pcm"
    return "audio." + codec if "audio." + codec in RULE_DEFAULTS else "audio.other"


def audio_action(codec, container="mkv", profile="", configured=None):
    """这条声音在成品里怎么处理：keep 不动 / pair 加一条电视能播的并留原来的 / replace 只留电视能播的。

    规则之外还有容器的硬限制：MP4 里放不了蓝光 LPCM，也只装得下 MP4_ORIGINAL_AUDIO 里的原始音轨。
    """
    codec = (codec or "").lower()
    setting = configured or rule(audio_rule_id(codec, profile))
    action = setting.split("_")[0]
    if action == "pair" and (not KEEP_ORIGINAL_AUDIO or (container == "mp4" and not KEEP_ORIGINAL_AUDIO_IN_MP4)):
        return "replace"
    if container == "mp4" and codec not in MP4_AUDIO_COPY | MP4_ORIGINAL_AUDIO:
        return "replace"
    return action


def audio_target(codec, profile="", configured=None):
    """要转码时转成什么：(ffmpeg 编码器, 码率)。规则里选了 AC-3 就是 AC-3 640k，否则 E-AC-3 1536k。"""
    if (configured or rule(audio_rule_id(codec, profile))).endswith("_ac3"):
        return ("ac3", "640k")
    return ("eac3", "1536k")


def subtitle_mode(codec, container="mkv", configured=None):
    """这条字幕怎么带进成品：sidecar 外挂 / embed 留在影片里 / None 带不了（DVD 字幕电视显示不了）。"""
    codec = (codec or "").lower()
    if not sidecar_format(codec):
        return None
    graphic = codec in GRAPHIC_SUB_CODECS
    mode = configured or rule("subs.graphic" if graphic else "subs.text")
    if mode == "embed" and container == "mp4" and graphic:
        return "sidecar"                       # MP4 装不下图片字幕
    return mode


def rule_outputs(rule_id, option):
    """设置页直接使用封装流程的容器限制与目标编码，显示两种成品各自的结果。"""
    outputs = {}
    for container in ("mkv", "mp4"):
        if rule_id.startswith("audio."):
            key = rule_id.split(".")[1]
            codec = {"dtshd": "dts", "pcm": "pcm_s24le"}.get(key, key)
            action = audio_action(codec, container, configured=option)
            encoder, _ = audio_target(codec, configured=option)
            label = "AC-3" if encoder == "ac3" else "E-AC-3"
            outputs[container] = {"keep": "和片源相同", "pair": label + " + 原音轨", "replace": label}[action]
        elif rule_id.startswith("subs."):
            codec = PGS_CODEC if rule_id == "subs.graphic" else "subrip"
            mode = subtitle_mode(codec, container, option)
            if codec == PGS_CODEC:
                outputs[container] = "外挂 .sup" if mode == "sidecar" else "内嵌"
            elif mode == "sidecar":
                outputs[container] = "外挂 .srt（SRT）或 .ass（ASS、SSA）"
            else:
                outputs[container] = "内嵌" if container == "mkv" else "内嵌（ASS、SSA 样式丢失）"
    return outputs


def subtitle_language_wanted(track, wanted=None):
    wanted = wanted or rule("pick.subs")
    if wanted == "none":
        return False
    if wanted == "chi":
        return is_chinese_track(track)
    lang = str(track.get("lang") or "").lower()
    return lang.startswith({"eng": "en", "jpn": "ja"}[wanted]) or lang == wanted


def converts_p7(dv_profile, mode=None):
    return dv_profile == 7 and (mode or rule("video.dv7")) == "convert"


def output_container(dv_profile, dv_container=None, p7_mode=None):
    """成品容器：带杜比视界且规则要求时用 MP4；双层杜比视界要转换时也只能进 MP4；其余 MKV。"""
    if not dv_profile:
        return "mkv"
    if (dv_container or rule("video.dv")) == "mp4" or converts_p7(dv_profile, p7_mode):
        return "mp4"
    return "mkv"


SCAN_INTERVAL = 8                                    # 秒

# ---- 字幕 OCR:把源里所有 PGS 图形字幕 OCR 成文本 SRT 内嵌(不判断语言,全转) ----
# PGS 图形字幕在 webOS 上会逼服务器烧录转码→卡;转成文本既不卡,又和片源天生同步
# (不像 Jellyfin 下载的字幕会因版本/帧率不同而恒定错位+越往后越飘)。
OCR_SUBS = False                                     # 2026-11-17 起默认关闭：电视能直接显示图形字幕，OCR 代码保留备用

# ---- 空间管理(基于真实余量调校) ----
# 媒体盘往往不止存电影，余量偏紧，所以预检从严。
SAFETY_MARGIN = 50 * 1024**3        # 永远给系统/其他用途留 50GB,处理不能吃到这条线以下
PEAK_FACTOR_BDMV = 2.6              # BDMV/ISO任务峰值 ≈ 源×2.6(提取正片 + DoVi中间流 + 成品并存)
PEAK_FACTOR_FILE = 1.1             # 普通文件：一遍封装出成品，约等于源大小（多一条 EAC3）
MIN_FREE_ABORT = 25 * 1024**3       # 处理中剩余跌破 25GB 立即中止,保护系统和其他服务

app = Flask(__name__)
LOCK = threading.RLock()
MAKEMKV_LOCK = threading.RLock()
UPGRADE_BUSY = {"accepted", "building", "ready", "switching"}
ACTIVE_OPERATIONS = set()
PIPELINE_HALT = ""

# item: {name, kind(file|dir|bdmv|iso|multi_disc), payload, size, status, probe, plan, clean, error}
ITEMS = {}
SIZE_HISTORY = {}
PROBE_FAILURES = {}  # name -> {size, count, retry_at}；探测失败的文件按退避重试
JOB_QUEUE = []
CURRENT_JOB = None   # {name, phase, percent, log[]}
PROCESSED = {}       # name -> {"output":..., "ts":...}
UPGRADE_STATE = {"status": "idle", "message": "当前已经是运行中的版本"}
DISC_PREVIEW_CACHE = {}
DISC_PREVIEW_QUEUE = []
DISC_PREVIEW_PENDING = set()
DISC_PREVIEW_ACTIVE = None
JELLYFIN_SUBTITLE_INDEX = {}


# ---------- 状态持久化 ----------
def load_state():
    global PROCESSED
    try:
        if STATE_FILE.exists():
            PROCESSED = json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        PROCESSED = {}


def save_state():
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(PROCESSED, ensure_ascii=False, indent=1), "utf-8")
        temp.replace(STATE_FILE)
    except Exception:
        pass


def load_disc_preview_cache():
    """载入原盘只读预分析缓存；缓存损坏时静默重建。"""
    global DISC_PREVIEW_CACHE
    try:
        if DISC_PREVIEW_CACHE_FILE.exists():
            data = json.loads(DISC_PREVIEW_CACHE_FILE.read_text("utf-8"))
            DISC_PREVIEW_CACHE = data if isinstance(data, dict) else {}
        else:
            DISC_PREVIEW_CACHE = {}
    except (OSError, ValueError):
        DISC_PREVIEW_CACHE = {}


def save_disc_preview_cache():
    """只保留最近 64 张盘的预读结果，避免状态文件无限增长。"""
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        recent = sorted(
            DISC_PREVIEW_CACHE.items(),
            key=lambda pair: pair[1].get("updated_epoch", 0),
            reverse=True,
        )[:64]
        data = dict(recent)
        temp = DISC_PREVIEW_CACHE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        temp.replace(DISC_PREVIEW_CACHE_FILE)
    except OSError:
        pass


def upgrade_status_path():
    return WORK / ".upgrade_status.json"


def write_upgrade_status(status, message, **extra):
    """持久化升级状态；容器切换后新版仍能向页面报告结果。"""
    global UPGRADE_STATE
    data = {
        "status": status,
        "message": message,
        "updated": time.strftime("%m-%d %H:%M:%S"),
        "updated_epoch": time.time(),
        **extra,
    }
    UPGRADE_STATE = data
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        temp = upgrade_status_path().with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        temp.replace(upgrade_status_path())
    except OSError:
        pass
    return data


def read_upgrade_status():
    try:
        saved = json.loads(upgrade_status_path().read_text("utf-8"))
        if isinstance(saved, dict) and saved.get("status"):
            return saved
    except (OSError, ValueError):
        pass
    return dict(UPGRADE_STATE)


def pipeline_block_reason():
    """无法确认远端提取已经停止时，持久暂停，避免重启后继续抢写磁盘。"""
    if PIPELINE_HALT:
        return PIPELINE_HALT
    marker = WORK / ".pipeline_block.txt"
    return marker.read_text("utf-8") if marker.exists() else ""


FOLDER_RESTARTING = False


def processing_block_reason():
    if FOLDER_RESTARTING:
        return "面板正在切换媒体目录"
    if read_upgrade_status().get("status") in UPGRADE_BUSY:
        return "服务正在升级，请等待升级结束后再处理媒体"
    if makemkv_update_running():
        return MAKEMKV_UPDATE.get("message") or "正在更新读盘工具"
    return pipeline_block_reason()


class lock_released:
    """在已持有 LOCK 的代码里临时放开它，用于删除大文件这类慢操作。

    删除一个几十 GB 的文件在机械盘上可能要几十秒。如果这期间一直占着全局锁，
    页面的状态刷新也会被堵住，看起来就像“点了没反应”。互斥由 media_mutation 保证，
    放开 LOCK 不会让别的修改操作插进来。
    """
    def __enter__(self):
        LOCK.release()

    def __exit__(self, *_exc):
        LOCK.acquire()
        return False


def media_mutation(func):
    """升级与有副作用的请求互斥，覆盖长时间字幕校轴请求。"""
    @wraps(func)
    def guarded(*args, **kwargs):
        token = uuid.uuid4().hex
        with LOCK:
            reason = processing_block_reason()
            if reason:
                return jsonify({"error": reason}), 409
            if ACTIVE_OPERATIONS:
                return jsonify({"error": "另一个修改操作正在进行，请稍后重试"}), 409
            ACTIVE_OPERATIONS.add(token)
        try:
            return func(*args, **kwargs)
        finally:
            with LOCK:
                ACTIVE_OPERATIONS.discard(token)
    return guarded


DISCARDABLE = {"ready", "error", "blocked"}   # 还没处理的条目：可以直接删掉不要的源

HELPER_STATES = {"ready", "switching"}   # 这两个状态由一次性升级助手容器推进
HELPER_GRACE_SECONDS = 180


def recover_interrupted_upgrade(startup=True):
    """升级助手已经不在、状态却还停在进行中时，解除互斥。

    启动时检查所有进行中状态（accepted/building 属于上一个进程，必然已中断）。
    运行期间只检查由助手容器推进的 ready/switching，并留出宽限期：状态刚写入、
    助手容器还没创建出来的瞬间不能误判。
    """
    saved = read_upgrade_status()
    status = saved.get("status")
    if status not in UPGRADE_BUSY:
        return
    if not startup:
        if status not in HELPER_STATES:
            return
        try:
            age = time.time() - float(saved.get("updated_epoch") or 0)
        except (TypeError, ValueError):
            age = HELPER_GRACE_SECONDS + 1
        if age <= HELPER_GRACE_SECONDS:
            return
    try:
        import docker
        with docker.from_env() as client:
            helpers = client.containers.list(filters={
                "name": "mediapanel-upgrader-" + str(saved.get("upgrade_id", ""))
            })
        if not helpers:
            write_upgrade_status("failed", "上次升级被中断，当前服务已恢复，可重新升级")
    except Exception:
        # 无法确认升级助手状态时保留互斥锁，避免误启动任务。
        pass


# 镜像构建文件内嵌在 app.py 里，页面升级时只需要上传 app.py 一个文件。
# 仓库里的 Dockerfile 必须与这里逐字一致（有测试把关），手工 docker build 用那一份。
EMBEDDED_DOCKERFILE = r'''FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg mkvtoolnix \
      tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-eng \
      tesseract-ocr-jpn tesseract-ocr-kor \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
# pgsrip 把 ISO 639-3 中文码 zho 直接交给 Tesseract；Debian 的模型文件却叫
# chi_sim.traineddata。补一个同目录别名，否则“中文包明明安装了”仍会 OCR 失败。
RUN set -eux; \
    chi_sim="$(find /usr/share/tesseract-ocr -name chi_sim.traineddata -print -quit)"; \
    test -n "$chi_sim"; \
    ln -sf "$(basename "$chi_sim")" "$(dirname "$chi_sim")/zho.traineddata"
RUN pip install --no-cache-dir \
      flask docker pgsrip==0.1.12 ffsubsync==0.5.0
# dovi_tool（转换双层杜比视界用）：按机器架构取官方发行包，核对校验值后放进镜像。
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64) target=x86_64-unknown-linux-musl; sum=3933a4ea48970ba63dc007e1889791343ab8dea7f46492554cec5da5aad8c13e ;; \
      aarch64) target=aarch64-unknown-linux-musl; sum=d5ca2195f343828c5822926145b5c9c2f6c0124a432b1dff0c4b7a86f0674ce3 ;; \
      *) echo "unsupported architecture: $(uname -m)"; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/dovi_tool.tar.gz')" \
      "https://github.com/quietvoid/dovi_tool/releases/download/2.1.3/dovi_tool-2.1.3-${target}.tar.gz"; \
    echo "${sum}  /tmp/dovi_tool.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/tvtranscode; \
    tar -xzf /tmp/dovi_tool.tar.gz -C /opt/tvtranscode; \
    rm /tmp/dovi_tool.tar.gz; \
    chmod 755 /opt/tvtranscode/dovi_tool; \
    /opt/tvtranscode/dovi_tool --version
COPY app.py /app/app.py
WORKDIR /app
EXPOSE 8097
CMD ["python", "app.py"]
'''


def extract_embedded_dockerfile(app_bytes):
    """从上传的 app.py 源码里取出内嵌的 Dockerfile；只解析语法树，不执行代码。"""
    import ast
    try:
        tree = ast.parse(app_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "EMBEDDED_DOCKERFILE"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            return node.value.value.encode("utf-8")
    return None


def validate_upgrade_files(app_bytes, dockerfile_bytes):
    """在构建前拒绝明显错误或不属于本项目的升级文件。"""
    if not app_bytes or len(app_bytes) > 2 * 1024 * 1024:
        raise ValueError("app.py 为空或超过 2MB")
    if not dockerfile_bytes or len(dockerfile_bytes) > 128 * 1024:
        raise ValueError("Dockerfile 为空或超过 128KB")
    try:
        app_source = app_bytes.decode("utf-8-sig")
        compile(app_source, "app.py", "exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError(f"app.py 语法检查失败: {exc}") from exc
    docker_text = dockerfile_bytes.decode("utf-8", "replace")
    if not re.search(r"(?mi)^\s*FROM\s+\S+", docker_text):
        raise ValueError("Dockerfile 缺少 FROM")
    if not re.search(r"(?mi)^\s*COPY\s+app\.py\s+", docker_text):
        raise ValueError("Dockerfile 必须把 app.py 复制进镜像")
    if "CMD" not in docker_text.upper():
        raise ValueError("Dockerfile 缺少启动 CMD")


def docker_build_context(app_bytes, dockerfile_bytes):
    """生成只含两个升级文件的 Docker tar build context。"""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in (("app.py", app_bytes), ("Dockerfile", dockerfile_bytes)):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = int(time.time())
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    stream.seek(0)
    return stream


UPGRADE_HELPER = r"""
import json
import os
import time
import urllib.request
from pathlib import Path
import docker

status_path = Path(os.environ["UPGRADE_STATUS_PATH"])
old_id = os.environ["UPGRADE_OLD_ID"]
new_image = os.environ["UPGRADE_NEW_IMAGE"]
upgrade_id = os.environ["UPGRADE_ID"]
# 换文件夹时用：要多挂进面板的 NAS 文件夹、新的文件夹设置、要带到新工作目录的文件
extra_binds = json.loads(os.environ.get("UPGRADE_EXTRA_BINDS") or "[]")
new_folders = os.environ.get("UPGRADE_FOLDERS") or ""
folders_path = Path(os.environ.get("UPGRADE_FOLDERS_PATH") or "/share/.tv-transcode.json")
done_message = os.environ.get("UPGRADE_DONE_MESSAGE") or "升级成功，服务已切换到新版本"
panel_port = os.environ.get("UPGRADE_PORT") or "8097"
folders_backup = None
folders_written = False

def write(status, message, **extra):
    data = {"status": status, "message": message,
            "updated": time.strftime("%m-%d %H:%M:%S"),
            "updated_epoch": time.time(),
            "upgrade_id": upgrade_id, **extra}
    tmp = status_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(status_path)

# 准备阶段（连接 Docker、读取旧容器配置）出错时旧容器还没被动过，必须把状态写成
# 失败再退出；否则状态永远停在“准备切换”，面板会一直拒绝所有操作。
try:
    client = docker.from_env()
    old = client.containers.get(old_id)
    old.reload()
    attrs = old.attrs
    original_name = old.name
    backup_name = f"{original_name}-rollback-{upgrade_id}"
    old_image_id = old.image.id
    volumes = {}
    for mount in attrs.get("Mounts", []):
        if mount.get("Type") == "bind":
            volumes[mount["Source"]] = {
                "bind": mount["Destination"],
                "mode": "rw" if mount.get("RW") else "ro",
            }
    for source in extra_binds:
        volumes.setdefault(source, {"bind": source, "mode": "rw"})
    environment = [
        value for value in attrs.get("Config", {}).get("Env", [])
        if not value.startswith("HOSTNAME=")
    ]
    network_mode = attrs.get("HostConfig", {}).get("NetworkMode") or "host"
    ports = {}
    for port, bindings in (attrs.get("HostConfig", {}).get("PortBindings") or {}).items():
        wanted = [(b.get("HostIp") or "0.0.0.0", int(b["HostPort"])) for b in bindings or [] if b.get("HostPort")]
        if wanted:
            ports[port] = wanted
    labels = dict(attrs.get("Config", {}).get("Labels") or {})
    restart_policy = attrs.get("HostConfig", {}).get("RestartPolicy") or {
        "Name": "unless-stopped"
    }
except Exception as exc:
    write("failed", f"升级准备失败，当前版本未受影响: {exc}")
    raise SystemExit(1)
new = None
old_stopped = False
old_renamed = False
try:
    write("switching", "正在切换容器")
    old.stop(timeout=25)
    old_stopped = True
    if new_folders:
        # 规则和 Jellyfin 密钥跟着走；再记下新的文件夹设置（失败时会改回去）
        import shutil
        target = Path("/newinbox/dvwork")
        target.mkdir(parents=True, exist_ok=True)
        for name, overwrite in ((".rules.json", True), (".jellyfin.json", False)):
            source = Path("/oldwork") / name
            destination = target / name
            if destination.exists() and source.exists() and os.path.samefile(source, destination):
                continue                      # 下载文件夹没换：就是同一个文件，不用带
            if source.is_file() and (overwrite or not destination.exists()):
                shutil.copy2(source, destination)
        folders_backup = folders_path.read_text("utf-8") if folders_path.is_file() else None
        folders_path.write_text(new_folders, "utf-8")
        folders_written = True
    old.rename(backup_name)
    old_renamed = True
    new = client.containers.run(
        new_image,
        name=original_name,
        detach=True,
        network_mode=network_mode,
        restart_policy=restart_policy,
        environment=environment,
        volumes=volumes,
        ports=ports or None,
        labels=labels or None,
    )
    deadline = time.time() + 75
    last_error = "服务尚未响应"
    probe = ("import urllib.request;urllib.request.urlopen("
             "'http://127.0.0.1:" + panel_port + "/api/state', timeout=3)")
    while time.time() < deadline:
        try:
            result = new.exec_run(["python", "-c", probe])
            if result.exit_code == 0:
                last_error = ""
                break
            last_error = (result.output or b"").decode("utf-8", "replace").strip().splitlines()[-1][:200] or "服务尚未响应"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    if last_error:
        raise RuntimeError("新版健康检查失败: " + last_error)
    old.remove(force=True)
    try:
        if old_image_id != new.image.id:
            client.images.remove(old_image_id)
    except Exception:
        pass
    write("success", done_message, image=new_image)
except Exception as exc:
    try:
        if new is not None:
            new.remove(force=True)
    except Exception:
        pass
    try:
        if folders_written:
            if folders_backup is None:
                folders_path.unlink()
            else:
                folders_path.write_text(folders_backup, "utf-8")
        if old_renamed:
            old.reload()
            old.rename(original_name)
        if old_stopped:
            old.start()
    except Exception as rollback_exc:
        write("failed", f"升级失败且自动回滚失败: {exc}; {rollback_exc}")
    else:
        write("rolled_back", f"没有成功，已自动恢复原样: {exc}")
"""


def current_panel_container(client):
    """找到面板自己所在的容器。群晖 --net=host 时 HOSTNAME 可能是宿主机名，所以先按约定名 mediapanel 找。"""
    errors = []
    for ref in (PANEL_CONTAINER, os.environ.get("HOSTNAME")):
        if not ref:
            continue
        try:
            return client.containers.get(ref)
        except Exception as exc:
            errors.append(f"{ref}: {exc}")
    raise RuntimeError(f"找不到面板自己的容器（按名字 {PANEL_CONTAINER} 找；名字不同请设置环境变量 PANEL_CONTAINER）: " + "；".join(errors))


PANEL_MOUNTS = None


def panel_mounts():
    """面板容器挂了哪些文件夹：[(NAS 上的位置, 面板里的位置)]。没接 Docker 时为空，此时两边路径相同。"""
    global PANEL_MOUNTS
    if PANEL_MOUNTS is None:
        found = []
        try:
            import docker
            client = docker.from_env()
            try:
                for mount in current_panel_container(client).attrs.get("Mounts", []):
                    if mount.get("Type") == "bind" and os.path.isdir(mount.get("Destination") or ""):
                        found.append((mount["Source"].rstrip("/") or "/", mount["Destination"].rstrip("/") or "/"))
            finally:
                client.close()
        except Exception:
            found = []
        PANEL_MOUNTS = found
    return PANEL_MOUNTS


def _translate(path, pairs):
    path = str(path).rstrip("/") or "/"
    best = None
    for source, target in pairs:
        if path == source or path.startswith(source.rstrip("/") + "/"):
            if best is None or len(source) > len(best[0]):
                best = (source, target)
    if best is None:
        return None
    return (best[1].rstrip("/") + path[len(best[0].rstrip("/")):]) or "/"


def host_path(path):
    """面板里的路径 → NAS 上的真实位置（给人看、给别的容器用）。"""
    return _translate(path, [(inside, outside) for outside, inside in panel_mounts()]) or str(path)


def panel_path(host):
    """NAS 上的位置 → 面板里的路径；面板还没挂这个位置时返回 None。没接 Docker 时两边相同。"""
    if not panel_mounts():
        return str(host)
    return _translate(host, panel_mounts())


def upgrade_status_binding():
    """给升级助手用：把工作目录挂到 /oldwork，状态文件写在那里。"""
    if not panel_mounts() or panel_path(host_path(WORK)) is None:      # 认不出挂载时退回老办法：工作目录就在 /share 下面
        return {}, "/share/" + str(UPGRADE_STATUS_FILE.relative_to(SHARE))
    return {host_path(WORK): {"bind": "/oldwork", "mode": "rw"}}, "/oldwork/" + UPGRADE_STATUS_FILE.name


def build_and_schedule_upgrade(app_bytes, dockerfile_bytes, upgrade_id):
    """后台构建候选镜像，并启动一次性助手完成自更新与回滚。"""
    client = None
    image_tag = f"mediapanel:update-{upgrade_id}"
    helper_started = False
    try:
        import docker
        write_upgrade_status("building", "正在构建新版镜像，当前服务保持运行",
                             upgrade_id=upgrade_id)
        client = docker.from_env()
        client.images.build(
            fileobj=docker_build_context(app_bytes, dockerfile_bytes),
            custom_context=True,
            tag=image_tag,
            rm=True,
            forcerm=True,
        )
        current = current_panel_container(client)
        current.reload()
        share_mount = next(
            (mount for mount in current.attrs.get("Mounts", [])
             if mount.get("Destination") == "/share" and mount.get("Type") == "bind"),
            None,
        )
        if not share_mount:
            raise RuntimeError("当前容器没有 /share 绑定，无法安全交接升级状态")
        write_upgrade_status("ready", "镜像构建成功，准备切换容器",
                             upgrade_id=upgrade_id, image=image_tag)
        client.containers.run(
            image_tag,
            command=["python", "-c", UPGRADE_HELPER],
            name=f"mediapanel-upgrader-{upgrade_id}",
            detach=True,
            auto_remove=True,
            network_mode="host",
            volumes={
                "/var/run/docker.sock": {
                    "bind": "/var/run/docker.sock", "mode": "rw"
                },
                share_mount["Source"]: {"bind": "/share", "mode": "rw"},
                **upgrade_status_binding()[0],
            },
            environment={
                "UPGRADE_STATUS_PATH": upgrade_status_binding()[1],
                "UPGRADE_PORT": str(PANEL_PORT),
                "UPGRADE_OLD_ID": current.id,
                "UPGRADE_NEW_IMAGE": image_tag,
                "UPGRADE_ID": upgrade_id,
            },
        )
        helper_started = True
    except Exception as exc:
        if client is not None and not helper_started:
            try:
                client.images.remove(image_tag, force=True)
            except Exception:
                pass
        write_upgrade_status("failed", f"升级准备失败，当前版本未受影响: {exc}",
                             upgrade_id=upgrade_id)


# ---------- 工具 ----------
def owner_ids():
    """设了 PUID / PGID 时，成品要交给的用户和用户组；没设返回 None（文件归 root，和以前一样）。"""
    uid, gid = env_text("PUID"), env_text("PGID")
    if not (uid.isdigit() or gid.isdigit()):
        return None
    return (int(uid) if uid.isdigit() else -1, int(gid) if gid.isdigit() else -1)


def hand_over(*paths):
    """把写进影片文件夹的文件交给 PUID / PGID 指定的用户。

    面板以 root 运行（要用 Docker 套接字），写出的文件默认归 root；有的 NAS 上别的程序因此读不了。
    """
    ids = owner_ids()
    if not ids:
        return
    for path in paths:
        try:
            if path and os.path.exists(str(path)):
                os.chown(str(path), *ids)
        except OSError as exc:
            print(f"改文件归属失败 {path}: {exc}", flush=True)


def human(n):
    n = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or u == "TB":
            return f"{int(n)}B" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def disk_free(where=None):
    """某个文件夹所在硬盘的剩余可用字节；不指定时看下载文件夹（转换的中间文件都放那里）。"""
    try:
        st = os.statvfs(str(where or INBOX))
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def disk_total():
    try:
        st = os.statvfs(str(INBOX))
        return st.f_blocks * st.f_frsize
    except OSError:
        return None


def library_on_another_disk():
    """影片文件夹和下载文件夹不在同一块硬盘上：成品要整份拷过去，那边也得有地方。"""
    try:
        return os.stat(str(MOVIES)).st_dev != os.stat(str(INBOX)).st_dev
    except OSError:
        return False


def estimate_peak(item):
    """估算处理该条目要多占多少空间（成品 + 中间文件的最高点）。

    现在的流程都是“视频原样复制”，成品和源差不多大：
      - 普通文件：一遍封装，约 1.1 倍（多一条 EAC3）；不用转换的原样复制约 1 倍。
      - 杜比视界 P7：多一份中间的基础层文件，约 2.2 倍。
      - 原盘直接读：一遍出成品 1.05 倍；需要再封装一遍 2.1 倍。
      - 原盘经 MakeMKV：取出一份 + 成品一份（P7 再加一份），按 2.6 倍。
    以前普通文件一律按 1.7 倍估，25GB 的片子要 43GB，空间稍紧就被误拦。
    """
    probe = item.get("probe")
    known = bool(probe and probe.get("video"))
    plan = make_plan(item["kind"], probe, item.get("payload", ""), item.get("selection")) if known else None
    if item["kind"] in DISC_KINDS:
        factor = PEAK_FACTOR_BDMV
        if disc_reads_directly(item) and plan:
            single = plan["container"] == "mkv" and not any(e["compat"] for e in plan["audio"]) and not plan["subs"]
            factor = 1.05 if single else 2.1
    elif plan and "p7" in plan["must"]:
        factor = 2.2
    elif plan and (plan["needs"] == "none" or item.get("as_is")):
        factor = 1.02
    else:
        factor = PEAK_FACTOR_FILE
    return int(item["size"] * factor)


def precheck_space(item):
    """任务开始前的空间预检; 返回 (ok:bool, reason:str)"""
    free = disk_free()
    if free is None:
        return True, ""   # 查不到就不拦(降级放行)
    need = estimate_peak(item) + SAFETY_MARGIN
    if free < need:
        return False, (f"空间不够：成品要占约 {human(estimate_peak(item))}，"
                       f"另外要给 NAS 留 {human(SAFETY_MARGIN)} 余量，一共需要 {human(need)}，"
                       f"现在只剩 {human(free)}。腾出 {human(need - free)} 以上再试。")
    if library_on_another_disk():
        library_free, library_need = disk_free(MOVIES), int(item["size"] * 1.1) + SAFETY_MARGIN
        if library_free is not None and library_free < library_need:
            return False, (f"影片文件夹所在的硬盘空间不够：成品约 {human(int(item['size'] * 1.1))}，"
                           f"加上 {human(SAFETY_MARGIN)} 余量需要 {human(library_need)}，现在只剩 {human(library_free)}。")
    return True, ""


JUNK_NAMES = {"@eaDir", "@SynoResource", ".DS_Store", "Thumbs.db", "desktop.ini"}


def is_junk_name(name):
    """群晖索引 / macOS / Windows 访问目录时自动留下的杂项，不属于媒体内容。"""
    return name in JUNK_NAMES or name.startswith("._")


def dir_size(p: Path):
    total = 0
    for root, dirs, files in os.walk(p, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not is_junk_name(d)]
        for f in files:
            if is_junk_name(f):
                continue
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def find_disc_sources(root: Path):
    """查找下载条目中的光盘源，兼容外层套目录与大小写不标准的 BDMV。"""
    if root.is_file():
        return [("iso", root)] if root.suffix.lower() in DISC_IMAGE_EXT else []
    if root.name.casefold() == "bdmv":
        return [("bdmv", root)]

    found = []
    for current, dirs, files in os.walk(root, onerror=lambda e: None):
        current = Path(current)
        bdmv_dirs = [d for d in dirs if d.casefold() == "bdmv"]
        for dirname in bdmv_dirs:
            found.append(("bdmv", current))
            dirs.remove(dirname)  # BDMV 内部很大，无需继续遍历 STREAM
        for filename in files:
            fp = current / filename
            if fp.suffix.lower() in DISC_IMAGE_EXT:
                found.append(("iso", fp))

    # 同一光盘根只保留一次，同时保持扫描顺序稳定
    unique = []
    seen = set()
    for kind, path in found:
        key = (kind, str(path.resolve()))
        if key not in seen:
            seen.add(key)
            unique.append((kind, path))
    return unique


def largest_video_in(p: Path):
    best, best_size = None, -1
    for root, _dirs, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            fp = Path(root) / f
            if fp.suffix.lower() in VIDEO_EXT:
                try:
                    s = fp.stat().st_size
                except OSError:
                    continue
                if s > best_size:
                    best, best_size = fp, s
    return best


def source_snapshot(path):
    """记录源文件清单与元数据；不读取几十 GB 的媒体正文。

    群晖索引的 @eaDir、macOS 的 .DS_Store / ._* 等杂项不计入；目录本身只记录
    “存在”，不记录时间和大小——往目录里放杂项会改动目录的修改时间，否则浏览一下
    文件夹就会让任务被判成“源已变化”。媒体文件的增删改仍由文件条目本身发现。
    """
    path = Path(path)
    entries = []
    if not path.exists() or path.is_symlink():
        raise ValueError("源不存在或是符号链接，无法安全核对")
    if path.is_file():
        files = [path]
    else:
        files = [path] + sorted(
            child for child in path.rglob("*")
            if not any(is_junk_name(part) for part in child.relative_to(path).parts)
        )
    for child in files:
        if child.is_symlink():
            raise ValueError("源目录包含符号链接，请先拆分为普通文件")
        relative = str(child.relative_to(path)) if child != path else "."
        if child.is_dir():
            entries.append((relative, True))
            continue
        stat = child.stat()
        entries.append((relative, False, stat.st_size, stat.st_mtime_ns,
                        stat.st_ctime_ns, stat.st_ino, stat.st_dev))
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()


def legacy_source_snapshot(path):
    """2026-11 之前的快照算法：计入全部条目（含杂项）和目录的时间、大小。

    只用于核对旧版本已经记录下来的快照，新任务不再生成。
    """
    path = Path(path)
    entries = []
    files = [path] if path.is_file() else sorted(path.rglob("*"))
    if not path.exists() or path.is_symlink():
        raise ValueError("源不存在或是符号链接，无法安全核对")
    for child in files:
        if child.is_symlink():
            raise ValueError("源目录包含符号链接，请先拆分为普通文件")
        stat = child.stat()
        entries.append((str(child.relative_to(path)) if child != path else ".",
                        child.is_dir(), stat.st_size, stat.st_mtime_ns,
                        stat.st_ctime_ns, stat.st_ino, stat.st_dev))
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()


def source_matches_snapshot(path, recorded):
    """源是否仍与完成时记录的快照一致；兼容旧版本记录的快照。"""
    if not recorded:
        return False
    return source_snapshot(path) == recorded or legacy_source_snapshot(path) == recorded


def multiple_video_files(path):
    """普通文件夹不能静默只选最大视频；样片也交给用户明确拆分。"""
    return sum(1 for p in Path(path).rglob("*")
               if p.is_file() and p.suffix.lower() in VIDEO_EXT) > 1


def audio_tv_direct(codec, container="mkv", profile=""):
    """这条声音是否原样留用（规则里选了“不动”，且成品容器装得下）。"""
    return audio_action(codec, container, profile) == "keep"


def audio_needs_transcode(codec, for_mp4=False):
    """这条音轨是否需要一条 EAC3 兼容轨（电视解不了它）。"""
    return not audio_tv_direct(codec, "mp4" if for_mp4 else "mkv")


def kept_original_tracks(probe, container, indices=None):
    """返回会在成品里同时保留原轨的源音轨序号（只看 indices 指定的音轨）。"""
    if not probe or container not in ("mkv", "mp4"):
        return []
    audio = probe.get("audio", [])
    chosen = range(len(audio)) if indices is None else indices
    kept = []
    for index in chosen:
        if audio_action(audio[index].get("codec"), container, audio[index].get("profile")) == "pair":
            kept.append(index)
    return kept


def ffprobe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, timeout=180)
        return json.loads(out.stdout.decode("utf-8", "replace"))
    except Exception:
        return None


def stream_duration(stream):
    """一条轨道自己的时长（秒）；MKV 写在 DURATION 标签里，MP4 写在 duration 字段。读不到返回 0。"""
    try:
        value = float(stream.get("duration") or 0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    for key, text in (stream.get("tags") or {}).items():
        if key.upper().startswith("DURATION"):
            match = re.match(r"\s*(\d+):(\d+):(\d+(?:\.\d+)?)", str(text))
            if match:
                return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    return 0.0


def summarize_probe(data):
    """把 ffprobe 结果压成面板需要的摘要"""
    if not data or "streams" not in data:
        return None
    s = {"video": None, "video_count": 0, "audio": [], "subs": [], "attachments": 0,
         "duration": float(data.get("format", {}).get("duration", 0) or 0)}
    for st in data["streams"]:
        t = st.get("codec_type")
        if t == "video" and (st.get("disposition") or {}).get("attached_pic"):
            # MKV/MP4 的封面图在 ffprobe 里是一条“视频流”；它不是正片视频轨，
            # 否则带封面的合规文件会被当成多视频轨而白白重新封装一遍。
            continue
        if t == "video":
            s["video_count"] += 1
            if s["video"] is None:
                dv = 0
                dovi = {}
                for sd in st.get("side_data_list", []) or []:
                    if "DOVI" in str(sd.get("side_data_type", "")).upper():
                        dv = int(sd.get("dv_profile", 0) or 0)
                        dovi = sd
                s["video"] = {
                    "codec": st.get("codec_name", "?"),
                    "dv_profile": dv,
                    "dv_compatibility_id": dovi.get("dv_bl_signal_compatibility_id"),
                    "dv_rpu_present": dovi.get("rpu_present_flag"),
                    "dv_el_present": dovi.get("el_present_flag"),
                    "dv_bl_present": dovi.get("bl_present_flag"),
                    "hdr": (st.get("color_transfer") in ("smpte2084", "arib-std-b67")),
                    "dynamic_range_known": True,
                    "color_transfer": st.get("color_transfer", ""),
                    "r_frame_rate": st.get("r_frame_rate", "24000/1001"),
                    "avg_frame_rate": st.get("avg_frame_rate", "0/0"),
                    "start_time": float(st.get("start_time", 0) or 0),
                    "width": int(st.get("width", 0) or 0),
                    "height": int(st.get("height", 0) or 0),
                    "pix_fmt": st.get("pix_fmt", ""),
                    "duration": stream_duration(st),
                }
        elif t == "audio":
            s["audio"].append({
                "codec": st.get("codec_name", "?"),
                "channels": int(st.get("channels", 2) or 2),
                "lang": (st.get("tags", {}) or {}).get("language", "und"),
                "title": (st.get("tags", {}) or {}).get("title", ""),
                "profile": st.get("profile") or "",
                "default": bool((st.get("disposition") or {}).get("default")),
            })
        elif t == "subtitle":
            tags = st.get("tags", {}) or {}
            s["subs"].append({
                "codec": st.get("codec_name", "?"),
                "lang": tags.get("language", "und"),
                "title": tags.get("title", ""),
                "forced": bool((st.get("disposition") or {}).get("forced")),
                "default": bool((st.get("disposition") or {}).get("default")),
            })
        elif t == "attachment":
            s["attachments"] += 1
    return s


VIDEO_LABELS = {
    "h264": "H.264",
    "hevc": "HEVC",
    "vc1": "VC-1",
    "vc1image": "VC-1",
    "mpeg2video": "MPEG-2",
    "mpeg4": "MPEG-4",
    "av1": "AV1",
}
AUDIO_LABELS = {
    "truehd": "TrueHD",
    "dts": "DTS",
    "eac3": "EAC3",
    "ac3": "AC3",
    "aac": "AAC",
    "flac": "FLAC",
}
LANGUAGE_LABELS = {
    "zho": "中文",
    "chi": "中文",
    "cmn": "中文",
    "jpn": "日语",
    "eng": "英语",
    "kor": "韩语",
    "und": "未标语言",
}


def frame_rate_label(rate):
    """把 24000/1001 等 ffprobe 帧率转换成稳定、易读的显示。"""
    try:
        fps = float(Fraction(str(rate)))
    except (ValueError, ZeroDivisionError):
        return "未知帧率"
    if abs(fps - round(fps)) < 0.001:
        return f"{int(round(fps))} fps"
    return f"{fps:.3f} fps"


def dynamic_range_label(video, target=False):
    """返回画面动态范围；P7 成品明确显示为电视可直放的 P8.1。"""
    if not video:
        return "未知动态范围"
    dv_profile = int(video.get("dv_profile", 0) or 0)
    if dv_profile:
        if target and converts_p7(dv_profile):
            return "杜比视界 P8.1"
        return f"杜比视界 P{dv_profile}"
    transfer = video.get("color_transfer", "")
    if transfer == "smpte2084":
        return "HDR10"
    if transfer == "arib-std-b67":
        return "HLG"
    if video.get("dynamic_range_known") is False:
        return "HDR/杜比待复核"
    return "HDR" if video.get("hdr") else "SDR"


def video_format_label(video, target=False):
    """面向用户的核心画面摘要：分辨率、帧率和动态范围。"""
    if not video:
        return "未知视频"
    width = int(video.get("width", 0) or 0)
    height = int(video.get("height", 0) or 0)
    resolution = f"{width}×{height}" if width and height else (
        f"{height}p" if height else "未知分辨率"
    )
    return " · ".join([
        resolution,
        frame_rate_label(video.get("r_frame_rate")),
        dynamic_range_label(video, target=target),
    ])


def dolby_outcome_label(video):
    """告诉用户 LG G5 最终是否会进入杜比视界模式。"""
    if video and int(video.get("dv_profile", 0) or 0):
        if int(video["dv_profile"]) == 7 and not converts_p7(7):
            return "保留杜比视界 P7，播放效果取决于终端"
        profile = "P8.1" if converts_p7(int(video.get("dv_profile", 0) or 0)) else (
            f"P{int(video.get('dv_profile', 0) or 0)}"
        )
        return f"预计支持杜比视界（{profile}，需电视验片）"
    if video and video.get("dynamic_range_known") is False:
        return "杜比视界待处理时复核"
    return "不会点亮杜比视界"


CHINESE_LANGS = {"zho", "chi", "zh", "cmn", "yue", "chs", "cht", "zh-cn", "zh-tw", "zh-hans", "zh-hant"}
CHINESE_HINT_RE = re.compile(r"中文|简体|簡體|繁体|繁體|国语|國語|粤语|粵語|chinese|mandarin|cantonese|\bchs\b|\bcht\b", re.I)
SIMPLIFIED_HINT_RE = re.compile(r"简|簡体|simplified|\bchs\b|hans|zh-cn", re.I)
TRADITIONAL_HINT_RE = re.compile(r"繁|traditional|\bcht\b|hant|zh-tw|cantonese|粤|粵", re.I)
COMMENTARY_RE = re.compile(r"comment|评论|評論|解说|解說|导演评|導演評|audio description|descripti", re.I)
SDH_RE = re.compile(r"\bsdh\b|hearing|听障|聽障", re.I)


def is_commentary(track):
    return bool(COMMENTARY_RE.search(str(track.get("title") or "")))


def is_chinese_track(track):
    lang = str(track.get("lang") or "").lower()
    return lang in CHINESE_LANGS or bool(CHINESE_HINT_RE.search(str(track.get("title") or "")))


def audio_quality_score(track):
    """同一语言有多条音轨时挑“最好的一条”：无损优先，其次声道数。"""
    codec = str(track.get("codec") or "").lower()
    profile = str(track.get("profile") or "") + " " + str(track.get("title") or "")
    if codec in ("truehd", "mlp"):
        base = 90
    elif codec == "dts" and re.search(r"\bMA\b|DTS:?X|Master", profile, re.I):
        base = 85
    elif codec.startswith("pcm_"):
        base = 80
    elif codec == "flac":
        base = 78
    elif codec == "dts" and re.search(r"HRA|High Res", profile, re.I):
        base = 70
    elif codec == "dts":
        base = 60
    elif codec == "eac3":
        base = 50
    elif codec == "ac3":
        base = 40
    elif codec == "aac":
        base = 30
    else:
        base = 20
    return base * 10 + int(track.get("channels") or 0)


def default_selection(probe, audio_mode=None, subtitle_language=None):
    """自动预选：一条原声音轨（同语言里最好的那条）+ 一条中文字幕。

    “原声”只能靠线索判断：标了默认的音轨，否则第一条非评论音轨。国内发行版把国语
    配音排第一、动画碟默认英语配音等情况会选错，所以页面上必须让用户看得到、改得了。
    """
    audio = (probe or {}).get("audio", [])
    subs = (probe or {}).get("subs", [])
    chosen_audio = []
    if audio:
        usable = [i for i, track in enumerate(audio) if not is_commentary(track)] or list(range(len(audio)))
        anchor = next((i for i in usable if audio[i].get("default")), usable[0])
        language = str(audio[anchor].get("lang") or "und").lower()
        same_language = [i for i in usable if str(audio[i].get("lang") or "und").lower() == language]
        best = max(same_language, key=lambda i: (audio_quality_score(audio[i]), -i))
        mode = audio_mode or rule("pick.audio")
        chosen_audio = [{"quality": best, "default": anchor, "first": usable[0]}[mode]]
    chosen_subs = []
    candidates = [j for j, track in enumerate(subs)
                  if subtitle_language_wanted(track, subtitle_language) and sidecar_format(track.get("codec")) is not None]
    if candidates:
        def rank(j):
            track = subs[j]
            title = str(track.get("title") or "") + " " + str(track.get("lang") or "")
            return (
                0 if track.get("forced") else 1,
                0 if SDH_RE.search(title) else 1,
                2 if SIMPLIFIED_HINT_RE.search(title) else (0 if TRADITIONAL_HINT_RE.search(title) else 1),
                1 if track.get("codec") not in GRAPHIC_SUB_CODECS else 0,
                -j,
            )
        chosen_subs = [max(candidates, key=rank)]
    return {"audio": chosen_audio, "subs": chosen_subs}


def normalize_selection(probe, selection):
    """校验用户选择；缺失或越界时回到自动预选。返回 {"audio": [...], "subs": [...]}。"""
    audio = (probe or {}).get("audio", [])
    subs = (probe or {}).get("subs", [])
    fallback = default_selection(probe)
    if not isinstance(selection, dict):
        return fallback
    if selection.get("all"):
        return {"audio": list(range(len(audio))),
                "subs": [j for j, track in enumerate(subs) if sidecar_format(track.get("codec")) is not None]}
    def clean(values, limit):
        result = []
        for value in values if isinstance(values, (list, tuple)) else []:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if 0 <= value < limit and value not in result:
                result.append(value)
        return result
    chosen_audio = clean(selection.get("audio"), len(audio)) if "audio" in selection else fallback["audio"]
    chosen_subs = clean(selection.get("subs"), len(subs)) if "subs" in selection else fallback["subs"]
    if audio and not chosen_audio:
        chosen_audio = fallback["audio"]      # 有音轨的片子不允许一条都不留
    chosen_subs = [j for j in chosen_subs if sidecar_format(subs[j].get("codec")) is not None]
    return {"audio": chosen_audio, "subs": chosen_subs}


def remap_selection(before, after, selection):
    """原盘在提取前按预读结果选的轨，提取后按“语言 + 编码 + 第几条”对到真实轨道上。

    MakeMKV 预读列表和提取出的 MKV 轨道顺序不保证一致。对不上的轨返回 None，
    由调用方退回自动预选并留下警告。
    """
    if not isinstance(selection, dict) or selection.get("all") or not before or not after:
        return selection
    def key(track):
        return (str(track.get("lang") or "und").lower()[:3], str(track.get("codec") or "").lower().split("_")[0])
    mapped = {}
    for kind in ("audio", "subs"):
        if kind not in selection:
            continue
        old_tracks, new_tracks = before.get(kind, []), after.get(kind, [])
        result = []
        for index in selection.get(kind) or []:
            if not isinstance(index, int) or not 0 <= index < len(old_tracks):
                return None
            wanted = key(old_tracks[index])
            nth = sum(1 for track in old_tracks[:index] if key(track) == wanted)
            matches = [j for j, track in enumerate(new_tracks) if key(track) == wanted]
            if nth >= len(matches):
                return None
            result.append(matches[nth])
        mapped[kind] = result
    return mapped


def audio_track_label(track):
    codec = str(track.get("codec") or "?").lower()
    name = AUDIO_LABELS.get(codec, "LPCM" if codec.startswith("pcm_") else codec.upper())
    profile = str(track.get("profile") or "")
    if codec == "dts" and re.search(r"\bMA\b", profile):
        name = "DTS-HD MA"
    elif codec == "dts" and "X" in profile.upper().replace("DTS", ""):
        name = "DTS:X"
    if re.search(r"atmos", str(track.get("title") or "") + profile, re.I):
        name += " 全景声"
    channels = int(track.get("channels") or 0)
    layout = {1: "1.0", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1"}.get(channels, f"{channels}ch" if channels else "")
    language = LANGUAGE_LABELS.get(str(track.get("lang") or "und").lower(), str(track.get("lang") or "und"))
    label = " · ".join(part for part in (language, (name + " " + layout).strip()) if part)
    if is_commentary(track):
        label += " · 评论音轨"
    return label


def sub_track_label(track):
    codec = str(track.get("codec") or "?").lower()
    language = LANGUAGE_LABELS.get(str(track.get("lang") or "und").lower(), str(track.get("lang") or "und"))
    title = str(track.get("title") or "")
    if is_chinese_track(track):
        language = "中文"
        if SIMPLIFIED_HINT_RE.search(title):
            language = "简体中文"
        elif TRADITIONAL_HINT_RE.search(title):
            language = "繁体中文"
    kind = "图形" if codec in GRAPHIC_SUB_CODECS else "文本"
    label = f"{language} · {kind}"
    if track.get("forced") or re.search(r"forced|foreign|强制", title, re.I):
        label += " · 仅外语片段"
    if SDH_RE.search(title):
        label += " · 听障"
    return label


def audio_choice_label(track):
    """下拉框里给人看的音轨名：语言、几个声道的效果、音质档次，不出现编码名。"""
    codec = str(track.get("codec") or "").lower()
    language = LANGUAGE_LABELS.get(str(track.get("lang") or "und").lower(), str(track.get("lang") or "und"))
    if is_commentary(track):
        return f"{language} · 评论解说（不是正片声音）"
    channels = int(track.get("channels") or 0)
    sound = "环绕声" if channels >= 6 else ("立体声" if channels == 2 else ("单声道" if channels == 1 else ""))
    profile = str(track.get("profile") or "") + " " + str(track.get("title") or "")
    lossless = (codec in ("truehd", "mlp", "flac") or codec.startswith("pcm_")
                or (codec == "dts" and re.search(r"\bMA\b|DTS:?X|Master", profile, re.I)))
    parts = [language, sound, "最高音质" if lossless else "普通音质"]
    if re.search(r"atmos", profile, re.I):
        parts.append("全景声")
    return " · ".join(part for part in parts if part)


def media_badges(probe, audio_track=None):
    """卡片上一眼能看懂的小标签：清晰度、动态范围、音质。

    用的是电视和片源介绍里常见的叫法（4K、杜比视界、HDR10、杜比全景声），每个带一个类别给页面上色。
    audio_track 是电视上会播的那条音轨；不给时取音质最高的正片音轨。
    """
    video = (probe or {}).get("video")
    if not video:
        return []
    badges = []
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width >= 3800 or height >= 2100:
        badges.append({"text": "4K", "kind": "res"})
    elif width >= 1900 or height >= 1060:
        badges.append({"text": "1080p", "kind": "res"})
    elif width >= 1260 or height >= 700:
        badges.append({"text": "720p", "kind": "res"})
    transfer = video.get("color_transfer", "")
    if int(video.get("dv_profile", 0) or 0):
        badges.append({"text": "杜比视界", "kind": "dv"})
    elif transfer == "smpte2084":
        badges.append({"text": "HDR10", "kind": "hdr"})
    elif transfer == "arib-std-b67":
        badges.append({"text": "HLG", "kind": "hdr"})
    elif video.get("dynamic_range_known") is not False:
        badges.append({"text": "SDR", "kind": "plain"})
    tracks = [t for t in (probe.get("audio") or []) if not is_commentary(t)]
    track = audio_track or (max(tracks, key=audio_quality_score) if tracks else None)
    if track:
        codec = str(track.get("codec") or "").lower()
        profile = str(track.get("profile") or "") + " " + str(track.get("title") or "")
        if re.search(r"atmos", profile, re.I):
            badges.append({"text": "杜比全景声", "kind": "audio"})
        elif codec == "dts" and re.search(r"DTS:?X", profile, re.I):
            badges.append({"text": "DTS:X", "kind": "audio"})
        elif (codec in ("truehd", "mlp", "flac") or codec.startswith("pcm_")
              or (codec == "dts" and re.search(r"\bMA\b|Master", profile, re.I))):
            badges.append({"text": "无损音质", "kind": "audio"})
        channels = int(track.get("channels") or 0)
        layout = {8: "7.1", 7: "6.1", 6: "5.1", 2: "立体声", 1: "单声道"}.get(channels)
        if layout:
            badges.append({"text": layout, "kind": "plain"})
    return badges


def sub_choice_label(track):
    """下拉框里给人看的字幕名：只说语言和用途。"""
    language = LANGUAGE_LABELS.get(str(track.get("lang") or "und").lower(), str(track.get("lang") or "und"))
    title = str(track.get("title") or "")
    if is_chinese_track(track):
        language = "中文"
        if SIMPLIFIED_HINT_RE.search(title):
            language = "简体中文"
        elif TRADITIONAL_HINT_RE.search(title):
            language = "繁体中文"
    label = language
    if track.get("forced") or re.search(r"forced|foreign|强制", title, re.I):
        label += " · 只翻译外语片段"
    if SDH_RE.search(title):
        label += " · 含音效说明"
    return label


def choice_labels(tracks, label_of, recommended):
    """生成下拉选项：推荐的那条标出来，名字相同的用原标题里的版本名或序号区分。"""
    labels = [label_of(track) for track in tracks]
    result = []
    for index, (track, label) in enumerate(zip(tracks, labels)):
        if labels.count(label) > 1:
            version = re.search(r"\(([^()]{1,24})\)\s*$", str(track.get("title") or ""))
            nth = labels[:index + 1].count(label)
            label += f"（{version.group(1)}）" if version else f"（第 {nth} 条）"
        if index in recommended:
            label += "（推荐）"
        result.append(label)
    return result


@with_rule_snapshot
def make_plan(kind, probe, payload="", selection=None):
    """根据实测支持表决定这部片要不要转、转什么。纯计算，不碰文件。

    返回 dict：
      needs      "must" 必须转换 / "should" 建议转换 / "none" 不用转换
      container  成品容器（"mp4" 仅用于杜比视界，其余 "mkv"）
      audio      [{"src", "compat": 是否加 EAC3 兼容轨, "keep": 是否保留原轨, "lossy": 原轨是否丢失}]
      subs       [{"src", "how": "copy" | "text" | "sidecar"}]（成品字幕）
      gains / costs  给用户看的一句话清单
    """
    video = (probe or {}).get("video") or {}
    dv_profile = int(video.get("dv_profile") or 0)
    is_disc = kind in DISC_KINDS
    container = output_container(dv_profile)
    sel = normalize_selection(probe, selection)
    source_ext = Path(str(payload)).suffix.lower()
    kept = set(kept_original_tracks(probe, container, sel["audio"]))
    audio_plan = []
    for index in sel["audio"]:
        track = probe["audio"][index]
        compat = not audio_tv_direct(track.get("codec"), container, track.get("profile"))
        audio_plan.append({"src": index, "compat": compat, "keep": index in kept,
                           "lossy": compat and index not in kept})
    subs_plan = []
    for index in sel["subs"]:
        codec = (probe["subs"][index].get("codec") or "").lower()
        # 选中字幕按格式规则和目标容器决定内嵌或外挂。
        mode = subtitle_mode(codec, container)
        if mode:
            subs_plan.append({"src": index, "how": mode})

    # now：现在直接播会怎样（事实）；gains：转换后怎样；costs：代价。
    # 全部用“电视上看起来、听起来怎么样”来说，不出现码率、编码名、转码这类术语。
    must, should, now, gains, costs = [], [], [], [], []
    if is_disc:
        must.append("disc")
        now.append("蓝光原盘，电视无法直接播放")
        gains.append("转为电视可直接播放的影片")
    if converts_p7(dv_profile):
        must.append("p7")
        now.append("双层杜比视界，电视不支持，无法点亮")
        gains.append("杜比视界可以点亮")
        costs.append("移除电视用不到的数据，画面观感不变")
    elif dv_profile and container == "mp4" and (is_disc or source_ext != ".mp4"):
        must.append("dv-container")
        if not is_disc:
            now.append("杜比视界在当前文件格式下可能无法点亮")
        gains.append("杜比视界稳定点亮")
    elif dv_profile and container == "mkv" and not is_disc and source_ext and source_ext != ".mkv":
        should.append("container")
        now.append("文件格式与设置中的杜比视界格式不一致")
        gains.append("转为设置的格式，杜比视界不变")
    if not is_disc and not dv_profile and source_ext and source_ext not in AS_IS_CONTAINERS:
        should.append("container")
        now.append("该文件格式未经电视验证，播放可能卡顿")
        gains.append("转为已验证的文件格式")
    compat_tracks = [entry for entry in audio_plan if entry["compat"]]
    if compat_tracks:
        should.append("audio")
        tested_audio = any(probe["audio"][entry["src"]].get("codec") in AUDIO_TRANSCODE for entry in compat_tracks)
        if not is_disc and tested_audio:
            now.append("电视不支持此音轨，由 NAS 临时转换：音质下降，起播和快进变慢")
        gains.append("音质提升，即点即播" if tested_audio else "新增电视可直接播放的音轨")
        if all(entry["keep"] for entry in compat_tracks):
            gains.append("保留原始音轨，供功放或回音壁使用")
        else:
            costs.append("部分音轨仅保留压缩版本")
    if any(entry["how"] == "sidecar" for entry in subs_plan):
        if not is_disc:
            should.append("subs")
            now.append("字幕内嵌在影片中，电视加载需数分钟")
        gains.append("字幕即时显示")
    total_audio = len((probe or {}).get("audio", []))
    total_subs = len((probe or {}).get("subs", []))
    if not must and not should and (len(sel["audio"]) < total_audio or len(subs_plan) < total_subs):
        should.append("selection")
        gains.append("仅保留所选音轨和字幕，画面不变")
    needs = "must" if must else ("should" if should else "none")
    codec = str(video.get("codec") or "").lower()
    untested = (f"画面是 {video_codec_label(codec)} 编码，未经电视验证；转换不改变编码"
                if codec and codec not in TESTED_VIDEO_CODECS else "")
    if needs == "none":
        gains = [untested.split("；")[0]] if untested else ["电视可直接播放，画面和声音保持原样"]
        now = []
    elif untested:
        now.append(untested)
    if rule("pick.subs") == "chi" and not any(is_chinese_track(track) for track in (probe or {}).get("subs", [])):
        (gains if needs == "none" else now).append("无中文字幕，可在入库后添加")
    if needs != "none" and (total_audio > len(sel["audio"]) or total_subs > len(subs_plan)):
        costs.append(f"成品仅含所选的 {len(sel['audio'])} 条音轨"
                     + (f"和 {len(subs_plan)} 条字幕" if subs_plan else "，不含字幕"))
    return {"needs": needs, "must": must, "should": should, "container": container,
            "dv_profile": dv_profile, "selection": sel, "audio": audio_plan, "subs": subs_plan,
            "now": now, "gains": gains, "costs": costs}


def estimate_minutes(kind, plan, size, direct_disc=False):
    """粗略的耗时估计（J4125 + 机械盘的实测量级），只用于给用户一个数量级。"""
    gigabytes = max(float(size or 0) / 1024**3, 0.1)
    per_gb = 0.35                                  # 复制/转封装一遍
    if kind in DISC_KINDS and direct_disc:
        single = plan["container"] == "mkv" and not any(e["compat"] for e in plan["audio"]) and not plan["subs"]
        per_gb += 0.0 if single else 0.35          # 直接读盘：一遍出成品，或再封装一遍
    elif kind in DISC_KINDS:
        per_gb += 0.65                             # MakeMKV 提取
    if "p7" in plan["must"]:
        per_gb += 0.5                              # dovi_tool 转换 + 重建时间戳
    minutes = gigabytes * per_gb
    return max(1, int(round(minutes / 5.0) * 5) if minutes >= 8 else int(math.ceil(minutes)))


VERDICT_LABELS = {"must": "必须转换", "should": "建议转换", "none": "不用转换"}


@with_rule_snapshot
def conversion_card(kind, probe, payload="", size=0, selection=None):
    """页面卡片需要的全部内容：结论、得失、可选的音轨与字幕。"""
    if not probe or not probe.get("video"):
        return None
    def verdict(chosen):
        plan = make_plan(kind, probe, payload, chosen)
        costs = list(plan["costs"])
        direct_disc = ((probe.get("disc_preview") or {}).get("backend") == "direct")
        minutes = estimate_minutes(kind, plan, size, direct_disc)
        if plan["needs"] == "none":
            costs = ["复制进影片库，预计耗时 %d 分钟" % minutes]
        else:
            costs.insert(0, "预计耗时 %d 分钟" % minutes if minutes < 90
                         else "预计耗时 %.1f 小时" % (minutes / 60.0))
        return plan, {"verdict": plan["needs"], "verdict_label": VERDICT_LABELS[plan["needs"]],
                      "now": plan["now"], "gains": plan["gains"], "costs": costs}
    plan, summary = verdict(selection)
    chosen = plan["selection"]
    recommended = default_selection(probe)
    sub_modes = {j: subtitle_mode(track.get("codec"), plan["container"])
                 for j, track in enumerate(probe.get("subs", []))}
    # 单选时，结论只依赖字幕的处理方式。用每种方式的一条代表轨，避免逐轨交叉枚举。
    examples = {"none": []}
    for j, mode in sub_modes.items():
        if mode:
            examples.setdefault(mode, [j])
    by_selection = {}
    for i in range(len(probe.get("audio", []))) or [-1]:
        by_selection[str(i)] = {mode: verdict({"audio": [i] if i >= 0 else [], "subs": tracks})[1]
                                for mode, tracks in examples.items()}
    default_mode = sub_modes.get(next(iter(chosen["subs"]), -1)) or "none"
    card = dict(summary)
    card.update({
        "picture": video_format_label(probe["video"], target=True),
        "audio": [{"i": i, "label": label} for i, label in enumerate(
            choice_labels(probe.get("audio", []), audio_choice_label, recommended["audio"]))],
        "subs": [{"i": j, "label": label, "mode": sub_modes[j]} for j, label in enumerate(
            choice_labels(probe.get("subs", []), sub_choice_label, recommended["subs"]))
            if sidecar_format(probe["subs"][j].get("codec")) is not None],
        "selected": chosen,
        "by_audio": {i: modes[default_mode] for i, modes in by_selection.items()},
        "by_selection": by_selection,
        "all": verdict({"all": True})[1],
        # 不是“必须转换”的普通 MKV/MP4 才允许不转直接入库
        "can_import": plan["needs"] != "must" and kind not in DISC_KINDS
                      and Path(str(payload)).suffix.lower() in AS_IS_CONTAINERS,
    })
    return card


def target_container(kind, probe):
    """成品容器：只有杜比视界用 MP4（MKV 里实测会丢杜比），其余一律 MKV。

    HDR10 和普通画质放 MKV 实测都是直接播放，而且 MKV 能内嵌图形字幕和 LPCM。
    """
    video = (probe or {}).get("video") or {}
    return output_container(int(video.get("dv_profile") or 0))


def format_preview(kind, probe, payload="", preview_status=None, preview_error=None,
                   as_is=False, selection=None):
    """返回页面上的“当前画面 → G5 播放结果”说明。"""
    if not probe or not probe.get("video"):
        if kind == "iso":
            source = "ISO 光盘镜像"
        elif kind == "bdmv":
            source = "BDMV 原盘目录"
        elif kind == "multi_disc":
            source = "多张光盘"
        else:
            source = (Path(payload).suffix.lstrip(".").upper() or "媒体文件")
        analyzing = preview_status in {"queued", "analyzing"}
        failed = preview_status == "failed"
        if analyzing:
            source_detail = "正在用 MakeMKV 只读预分析主片，不提取、不生成文件"
            target = "正在预读转换后规格"
            target_detail = "完成后会提前显示分辨率、帧率、杜比/HDR、音轨和字幕"
        elif failed:
            source_detail = "只读预分析未完成；原盘没有被修改"
            target = "预读失败，处理时复核"
            target_detail = preview_error or "可继续处理，提取主片后会再次精确探测"
        elif kind == "multi_disc":
            source_detail = "同一目录里有多张盘，拆分前不会读取任何一张"
            target = "需先拆分"
            target_detail = "每张盘放到 download 下独立的顶层目录后，才会显示播放规格"
        else:
            source_detail = (
                "等待只读预分析分辨率、帧率、杜比/HDR、音轨和盘内字幕"
                if kind in DISC_KINDS else "等待媒体探测"
            )
            target = "等待预读" if kind in DISC_KINDS else "等待探测"
            target_detail = (
                "预读后直接显示 LG G5 预计播放规格"
                if kind in DISC_KINDS else "探测后显示 LG G5 实际播放结果"
            )
        return {
            "source": source,
            "source_detail": source_detail,
            "target": target,
            "target_detail": target_detail,
            "provisional": True,
            "pre_read": False,
            "disc_pending": kind in DISC_KINDS,
        }

    video = probe["video"]
    source = video_format_label(video)
    audio_tracks = probe.get("audio", [])
    audio_count = len(audio_tracks)
    pgs_count = sum(1 for sub in probe.get("subs", [])
                    if sub.get("codec") == "hdmv_pgs_subtitle")
    audio_codecs = []
    audio_languages = []
    for track in audio_tracks:
        label = AUDIO_LABELS.get(
            track.get("codec"), str(track.get("codec") or "?").upper()
        )
        if label not in audio_codecs:
            audio_codecs.append(label)
        lang = LANGUAGE_LABELS.get(
            str(track.get("lang") or "und").lower(),
            str(track.get("lang") or "und"),
        )
        if lang not in audio_languages:
            audio_languages.append(lang)
    audio_meta = []
    if audio_codecs:
        audio_meta.append(" / ".join(audio_codecs))
    if audio_languages:
        audio_meta.append(" / ".join(audio_languages))
    audio_suffix = f"（{'；'.join(audio_meta)}）" if audio_meta else ""
    disc_preview = probe.get("disc_preview") or {}
    source_kind = "转换前预读主片" if disc_preview else (
        "蓝光正片" if kind in DISC_KINDS else "媒体文件"
    )
    source_bits = [source_kind,
                   f"{audio_count} 条音轨{audio_suffix}"]
    if disc_preview.get("duration_h"):
        source_bits.insert(1, f"片长 {disc_preview['duration_h']}")
    if pgs_count:
        pgs_languages = []
        for sub in probe.get("subs", []):
            if sub.get("codec") != "hdmv_pgs_subtitle":
                continue
            lang = LANGUAGE_LABELS.get(
                str(sub.get("lang") or "und").lower(),
                str(sub.get("lang") or "und"),
            )
            if lang not in pgs_languages:
                pgs_languages.append(lang)
        pgs_suffix = f"（{' / '.join(pgs_languages)}）" if pgs_languages else ""
        source_bits.append(f"{pgs_count} 条 PGS{pgs_suffix}")
    elif probe.get("subs"):
        source_bits.append(f"{len(probe['subs'])} 条文本字幕")
    else:
        source_bits.append("无字幕")

    target = video_format_label(video, target=not as_is)
    if as_is:
        target_bits = ["原样复制，保留源容器及全部音轨、字幕"]
        return {
            "source": source,
            "source_detail": " · ".join(source_bits),
            "target": target,
            "target_detail": " · ".join(target_bits),
            "provisional": False,
            "pre_read": False,
        }
    target_bits = [dolby_outcome_label(video), "画面直通，分辨率和帧率不变"]
    plan = make_plan(kind, probe, payload, selection)
    target_bits.extend(audio_plan_labels(plan, probe) or ["所选音轨保留原编码"])
    if "p7" in plan["must"]:
        target_bits.append("保留基础层和动态元数据，移除增强层")
    if pgs_count and OCR_SUBS:
        target_bits.append(OCR_PLANNED_DETAIL)
    else:
        for mode, label in (("embed", "内嵌"), ("sidecar", "外挂")):
            count = sum(track["how"] == mode for track in plan["subs"])
            if count:
                target_bits.append(f"{label}字幕 {count} 条")
        if not plan["subs"]:
            target_bits.append("未选字幕")
    return {
        "source": source,
        "source_detail": " · ".join(source_bits),
        "target": target,
        "target_detail": " · ".join(target_bits),
        "provisional": bool(disc_preview),
        "pre_read": bool(disc_preview),
    }


AS_IS_CONTAINERS = {".mkv", ".mp4"}
AS_IS_PLAN = "原样入库（未转换）"
OPTIONAL_TAG = "也可原样入库"


def processing_optional(kind, probe, payload=""):
    """这个文件是否可以不转换、原样入库（结论不是“必须转换”的普通 MKV/MP4）。"""
    if kind in DISC_KINDS or kind == "multi_disc" or not probe or not probe.get("video"):
        return False
    if Path(str(payload)).suffix.lower() not in AS_IS_CONTAINERS:
        return False
    return make_plan(kind, probe, payload)["needs"] != "must"


def audio_plan_labels(plan, probe):
    counts = {}
    for entry in plan["audio"]:
        if entry["compat"]:
            track = probe["audio"][entry["src"]]
            codec, _ = audio_target(track["codec"], track.get("profile"))
            label = "AC-3" if codec == "ac3" else "E-AC-3"
            action = f"添加 {label} 并保留原轨" if entry["keep"] else f"替换为 {label}"
            counts[action] = counts.get(action, 0) + 1
    return [f"{count} 条音轨{action}" for action, count in counts.items()]


def build_plan(kind, probe, payload="", selection=None):
    """生成动作清单; 返回 (actions:[str], clean:bool)。clean 表示无需转换、可直接入库。"""
    actions = []
    warnings = []
    if kind in DISC_KINDS:
        actions.append("MakeMKV提取正片")
    if not probe:
        return actions, False
    plan = make_plan(kind, probe, payload, selection)
    video = probe["video"]
    if "p7" in plan["must"]:
        actions.append("DoVi P7→P8")
    if "dv-container" in plan["must"] or (plan["container"] == "mp4" and kind in DISC_KINDS):
        actions.append("杜比视界→MP4封装")
    if video and video["codec"] in ("vc1", "vc1image"):
        warnings.append("⚠ VC-1 未在电视上实测，可能触发转码")
    if video and video["dv_profile"] not in (0, 5, 7, 8):
        warnings.append(f"⚠ DoVi P{video['dv_profile']}尚未针对LG实盘验证")
    actions.extend(audio_plan_labels(plan, probe))
    sidecars = sum(1 for entry in plan["subs"] if entry["how"] == "sidecar")
    if sidecars:
        actions.append(f"{sidecars}条字幕做成外挂文件")
    if "selection" in plan["should"]:
        actions.append("按选轨规则重新封装")
    if "container" in plan["should"]:
        actions.append("转封装为MKV")
    clean = (kind not in DISC_KINDS) and plan["needs"] == "none"
    if clean:
        actions.append("已合规,直接入库")
    return actions + warnings, clean


# ---------- ISO / BDMV 转换前只读预分析 ----------
def disc_preview_signature(kind, payload, size):
    """缓存键同时绑定路径、大小和修改时间，替换同名镜像后会自动失效。"""
    path = Path(payload)
    try:
        mtime_ns = path.stat().st_mtime_ns
        resolved = str(path.resolve())
    except OSError:
        mtime_ns = 0
        resolved = str(path)
    raw = f"{kind}\0{resolved}\0{int(size or 0)}\0{mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()


def _disc_duration(value):
    value = str(value or "").strip()
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(value)
    except (TypeError, ValueError):
        return 0


def _disc_duration_label(seconds):
    seconds = int(seconds or 0)
    hours, remain = divmod(seconds, 3600)
    minutes = remain // 60
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟" if minutes else ""


def _makemkv_codec(values, stream_type):
    text = " ".join(str(value) for value in values.values()).lower()
    if stream_type == "video":
        if "hevc" in text or "h265" in text or "h.265" in text:
            return "hevc"
        if "avc" in text or "h264" in text or "h.264" in text:
            return "h264"
        if "vc-1" in text or "vc1" in text:
            return "vc1"
        if "mpeg2" in text or "mpeg-2" in text:
            return "mpeg2video"
        return "?"
    if stream_type == "audio":
        if "truehd" in text:
            return "truehd"
        if "dts" in text:
            return "dts"
        if "lpcm" in text or "pcm" in text:
            return "pcm_s16le"
        if "e-ac-3" in text or "eac3" in text or "dd+" in text:
            return "eac3"
        if "ac-3" in text or "ac3" in text:
            return "ac3"
        if "aac" in text:
            return "aac"
        if "flac" in text:
            return "flac"
        return "?"
    if "pgs" in text or "hdmv" in text:
        return "hdmv_pgs_subtitle"
    if "vobsub" in text or "dvd subtitle" in text:
        return "dvd_subtitle"
    return "subrip" if "srt" in text or "subrip" in text else "?"


def _makemkv_frame_rate(value):
    text = str(value or "")
    fraction = re.search(r"\((\d+\s*/\s*\d+)\)", text)
    if fraction:
        return fraction.group(1).replace(" ", "")
    number = re.search(r"(\d+(?:\.\d+)?)", text)
    if not number:
        return "24000/1001"
    try:
        return str(Fraction(number.group(1)).limit_denominator(100000))
    except (ValueError, ZeroDivisionError):
        return number.group(1)


def _makemkv_channels(value):
    text = str(value or "")
    layout = re.search(r"(\d+)\.(\d+)", text)
    if layout:
        return int(layout.group(1)) + int(layout.group(2))
    number = re.search(r"(\d+)", text)
    return int(number.group(1)) if number else 2


def parse_makemkv_info(text):
    """解析 makemkvcon -r info 的 TINFO/SINFO，选择与实际流程一致的最长主片。

    MakeMKV 的机器人格式是 CSV：
    TINFO:title,id,code,value / SINFO:title,stream,id,code,value。
    未在 info 中明确出现的杜比/HDR 信息保持“待复核”，绝不猜测为杜比视界。
    """
    titles = {}
    streams = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not (line.startswith("TINFO:") or line.startswith("SINFO:")):
            continue
        prefix, _, payload = line.partition(":")
        try:
            fields = next(csv.reader([payload]))
        except (csv.Error, StopIteration):
            continue
        try:
            if prefix == "TINFO" and len(fields) >= 4:
                title, info_id = int(fields[0]), str(fields[1])
                titles.setdefault(title, {})[info_id] = fields[-1]
            elif prefix == "SINFO" and len(fields) >= 5:
                title, stream, info_id = int(fields[0]), int(fields[1]), str(fields[2])
                values = streams.setdefault(title, {}).setdefault(stream, {})
                values[info_id] = fields[-1]
                if info_id == "1":
                    values["_type_code"] = str(fields[3])
        except (TypeError, ValueError):
            continue

    candidates = []
    for title, title_streams in streams.items():
        has_video = any(
            values.get("_type_code") == "6201"
            or "video" in str(values.get("1", "")).lower()
            for values in title_streams.values()
        )
        if not has_video:
            continue
        info = titles.get(title, {})
        duration = _disc_duration(info.get("9"))
        try:
            size = int(re.sub(r"\D", "", str(info.get("10", ""))) or 0)
        except ValueError:
            size = 0
        candidates.append((duration >= 2400, duration, size, title))
    if not candidates:
        raise ValueError("MakeMKV 没有报告可用的视频标题")
    candidates.sort(reverse=True)
    _long_enough, duration, _size, title = candidates[0]

    summary = {
        "video": None,
        "video_count": 0,
        "audio": [],
        "subs": [],
        "attachments": 0,
        "duration": duration,
    }
    for _stream, values in sorted(streams[title].items()):
        stream_type_text = str(values.get("1", "")).lower()
        type_code = values.get("_type_code")
        joined = " ".join(str(value) for value in values.values())
        if type_code == "6201" or "video" in stream_type_text:
            summary["video_count"] += 1
            if summary["video"] is not None:
                continue
            resolution = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", values.get("19", ""))
            width = int(resolution.group(1)) if resolution else 0
            height = int(resolution.group(2)) if resolution else 0
            dv_match = (
                re.search(r"(?:dvhe|dvh1)[._-]?0?(\d)", joined, re.I)
                or re.search(r"dolby\s+vision.*?profile\s*([0-9]+)", joined, re.I)
            )
            dv_profile = int(dv_match.group(1)) if dv_match else 0
            has_hlg = bool(re.search(r"\bHLG\b|arib.?std.?b67", joined, re.I))
            has_hdr = bool(re.search(r"\bHDR(?:10|10\+)?\b|smpte.?2084", joined, re.I))
            codec = _makemkv_codec(values, "video")
            inferred_sdr = codec in {"h264", "vc1", "mpeg2video"} or (
                bool(height) and height <= 1080
            )
            dynamic_known = bool(dv_profile or has_hdr or has_hlg or inferred_sdr)
            summary["video"] = {
                "codec": codec,
                "dv_profile": dv_profile,
                "hdr": bool(has_hdr or has_hlg or dv_profile),
                "dynamic_range_known": dynamic_known,
                "color_transfer": (
                    "arib-std-b67" if has_hlg else
                    "smpte2084" if has_hdr or dv_profile else ""
                ),
                "r_frame_rate": _makemkv_frame_rate(values.get("21", "")),
                "width": width,
                "height": height,
                "pix_fmt": "",
            }
        elif type_code == "6202" or "audio" in stream_type_text:
            summary["audio"].append({
                "codec": _makemkv_codec(values, "audio"),
                "channels": _makemkv_channels(values.get("22", "")),
                "lang": values.get("3", "und") or "und",
            })
        elif type_code == "6203" or "subtitle" in stream_type_text:
            summary["subs"].append({
                "codec": _makemkv_codec(values, "subtitle"),
                "lang": values.get("3", "und") or "und",
                "title": values.get("2", ""),
            })
    if not summary["video"]:
        raise ValueError("主片标题没有可识别的视频轨")
    summary["disc_preview"] = {
        "title": title,
        "title_name": titles.get(title, {}).get("2", ""),
        "duration_h": _disc_duration_label(duration),
        "similar_titles": sum(1 for _long, seconds, _size, _title in candidates
                              if duration > 0 and seconds >= duration * 0.9),
    }
    return summary


MAKEMKV_OUTDATED = "读盘工具（MakeMKV）的版本过期了，更新之后才能读原盘。点“更新读盘工具”即可"
MAKEMKV_CRASHED = "读盘工具读这张盘时自己崩溃了，还没查明原因"
# makemkv 容器把设置和注册码放在 /config（/config/.MakeMKV 指向它），而 MakeMKV 只认
# $HOME/.MakeMKV。从外面 exec 进去默认 HOME=/root，读不到注册码，只能当“试用版”跑。
MAKEMKV_ENV = {"HOME": "/config"}
MAKEMKV_UPDATE = {"status": "idle", "message": ""}


def makemkv_update_running():
    return MAKEMKV_UPDATE.get("status") == "running"


def _set_makemkv_update(status, message):
    MAKEMKV_UPDATE.update(status=status, message=message, updated=time.strftime("%m-%d %H:%M:%S"))
    print("MakeMKV 更新: " + message, flush=True)


def makemkv_run_options(old, extra_binds=()):
    """照着现有读盘工具容器的配置，给出重建它要用的参数（挂载、环境变量、网络、重启策略、设备都带上）。"""
    attrs = old.attrs
    host = attrs.get("HostConfig", {}) or {}
    # 只带用户自己设的环境变量；镜像自带的默认值（含版本号）要用新镜像的
    defaults = set(old.image.attrs.get("Config", {}).get("Env") or [])
    environment = [value for value in attrs["Config"].get("Env") or []
                   if value not in defaults and not value.startswith("HOSTNAME=")]
    ports = {}
    for port, bindings in (host.get("PortBindings") or {}).items():
        if bindings:
            ports[port] = [(b.get("HostIp") or "0.0.0.0", int(b["HostPort"])) for b in bindings if b.get("HostPort")]
    devices = [f"{d['PathOnHost']}:{d['PathInContainer']}:{d.get('CgroupPermissions', 'rwm')}"
               for d in host.get("Devices") or []]
    return dict(
        environment=environment, volumes=list(host.get("Binds") or []) + list(extra_binds),
        restart_policy=host.get("RestartPolicy") or {"Name": "unless-stopped"},
        network_mode=host.get("NetworkMode") or "bridge",
        ports=ports or None, devices=devices or None,
        privileged=bool(host.get("Privileged")), cap_add=host.get("CapAdd") or None,
    )


def ensure_makemkv_sees_inbox():
    """读盘工具容器也得能看到下载文件夹（原盘在那里），而且路径要和面板里的一样。

    换了下载文件夹后它可能看不到：这时照原样重建它，只多挂这一个文件夹；失败就恢复原样。
    """
    try:
        import docker
        client = docker.from_env()
    except Exception:
        return
    new = None
    old_stopped = old_renamed = False
    try:
        try:
            old = client.containers.get(MAKEMKV_CONTAINER)
        except Exception:
            return                                  # 没装读盘工具：只是读不了原盘
        old.reload()
        inside, outside = str(INBOX), host_path(INBOX)
        covered = _translate(inside, [(m.get("Destination", "").rstrip("/") or "/", m.get("Source", "").rstrip("/") or "/")
                                      for m in old.attrs.get("Mounts", []) if m.get("Type") == "bind"])
        if covered == outside or not panel_mounts():
            return
        print(f"读盘工具看不到下载文件夹 {outside}，正在给它补挂", flush=True)
        options = makemkv_run_options(old, [f"{outside}:{inside}"])
        backup_name = f"{MAKEMKV_CONTAINER}-old-{time.strftime('%Y%m%d%H%M%S')}"
        old.stop(timeout=20)
        old_stopped = True
        old.rename(backup_name)
        old_renamed = True
        new = client.containers.run(old.image.id, name=MAKEMKV_CONTAINER, detach=True, **options)
        for _attempt in range(30):
            new.reload()
            if new.status == "running":
                break
            time.sleep(2)
        if new.status != "running":
            raise RuntimeError("补挂后启动不起来")
        old.remove(force=True)
    except Exception as exc:
        try:
            if new is not None:
                new.remove(force=True)
            if old_renamed:
                old.reload()
                old.rename(MAKEMKV_CONTAINER)
            if old_stopped:
                old.start()
        except Exception as rollback_exc:
            exc = f"{exc}；恢复原样也失败了：{rollback_exc}"
        print(f"读盘工具补挂下载文件夹失败，原盘暂时读不了：{exc}", flush=True)
    finally:
        client.close()


def update_makemkv_container():
    """拉取 makemkv 容器所用镜像的最新版，并用原来的配置重建容器；失败时恢复旧容器。

    MakeMKV 的免费试用只对最新版本有效，旧版本会直接拒绝读盘（“version is too old”），
    重启容器没有用，只能换成新版镜像。这里做的事和在群晖 Docker 里“重新下载镜像、
    清除并重建容器”一样：挂载目录、环境变量、网络、重启策略都照原样带过去。
    """
    import docker
    client = docker.from_env()
    new = None
    old_stopped = old_renamed = False
    backup_name = f"{MAKEMKV_CONTAINER}-old-{time.strftime('%Y%m%d%H%M%S')}"
    try:
        old = client.containers.get(MAKEMKV_CONTAINER)
        old.reload()
        attrs = old.attrs
        image_ref = attrs["Config"]["Image"]
        repository, _, tag = image_ref.rpartition(":")
        if not repository or "/" in tag:          # 没写标签，或冒号属于仓库地址里的端口
            repository, tag = image_ref, "latest"
        _set_makemkv_update("running", f"正在下载最新的读盘工具（{repository}:{tag}），可能要几分钟")
        image = client.images.pull(repository, tag=tag)
        if image.id == old.image.id:
            _set_makemkv_update("failed", "已经是能下载到的最新版本，但它仍然过期。只能等官方发布新版后再更新")
            return
        options = makemkv_run_options(old)
        _set_makemkv_update("running", "下载完成，正在换上新版本")
        old.stop(timeout=20)
        old_stopped = True
        old.rename(backup_name)
        old_renamed = True
        new = client.containers.run(image.id, name=MAKEMKV_CONTAINER, detach=True, **options)
        for _attempt in range(30):            # 最多等一分钟
            new.reload()
            if new.status == "running":
                break
            time.sleep(2)
        if new.status != "running":
            raise RuntimeError("新版本启动不起来")
        time.sleep(10)        # 容器启动脚本要先取到试用注册码
        old.remove(force=True)
        with LOCK:
            for item in ITEMS.values():       # 之前读取失败的盘自动重读
                if item.get("kind") in DISC_KINDS and item.get("preview_status") == "failed":
                    item["preview_status"] = None
                    item["preview_error"] = None
        _set_makemkv_update("done", "读盘工具已更新，正在重新读取原盘")
    except Exception as exc:
        rollback = ""
        try:
            if new is not None:
                new.remove(force=True)
            if old_renamed:
                old.reload()
                old.rename(MAKEMKV_CONTAINER)
            if old_stopped:
                old.start()
        except Exception as rollback_exc:
            rollback = f"；恢复旧版本也失败了：{rollback_exc}"
        _set_makemkv_update("failed", f"更新没成功，已恢复原样：{exc}{rollback}"[:500])
    finally:
        client.close()


def makemkv_info(src):
    """通过现有 MakeMKV 容器只读扫描原盘；不会执行 mkv/backup。"""
    import docker
    client = docker.from_env()
    try:
        container = client.containers.get(MAKEMKV_CONTAINER)
    except Exception as exc:
        raise RuntimeError(f"找不到运行中的 {MAKEMKV_CONTAINER} 容器") from exc
    command = [
        MAKEMKVCON, "-r", "--minlength=2400",
        "info", makemkv_source_spec(Path(src)),
    ]
    with MAKEMKV_LOCK:
        try:
            result = container.exec_run(command, demux=True, environment=MAKEMKV_ENV)
        finally:
            client.close()
    output = result.output
    if isinstance(output, tuple):
        output = b"\n".join(part for part in output if part)
    text = (output or b"").decode("utf-8", "replace")
    if result.exit_code != 0:
        # MakeMKV 自己说的话在 MSG 行的第 4 个字段里，取最后几条给用户看
        said = []
        for line in text.splitlines():
            if not line.startswith("MSG:"):
                continue
            try:
                fields = next(csv.reader([line[4:]], escapechar="\\"))
                if len(fields) > 3 and fields[3].strip():
                    said.append(fields[3].strip())
            except (csv.Error, StopIteration):
                continue
        detail = "；".join(said[-3:])[-400:]
        if "too old" in detail.lower():
            raise RuntimeError(MAKEMKV_OUTDATED)
        if result.exit_code in (139, -11, 134, -6):       # 段错误 / 异常中止：程序自己崩了
            raise RuntimeError(MAKEMKV_CRASHED)
        if not detail:
            detail = "读盘工具没有给出原因。可以先点“更新读盘工具”，再点“重新读取”"
        raise RuntimeError(f"读不了这张盘（代码 {result.exit_code}）：{detail}")
    return text


# ---------- 未加密原盘：不经 MakeMKV，直接读盘里的播放列表 ----------
# 下载来的“原盘”多数已经去掉了加密。这种盘 mkvmerge 能直接按播放列表把正片取出来，
# 不需要 MakeMKV（它有试用期、版本过期、新版本在个别盘上直接崩溃等问题）。
# 只有加密的盘、镜像文件（ISO）和双层杜比视界（正片里有两条画面）才仍然交给 MakeMKV。
MKVMERGE_AUDIO_CODECS = {
    "PCM": "pcm_bluray", "AC-3": "ac3", "E-AC-3": "eac3", "TrueHD": "truehd", "TrueHD Atmos": "truehd",
    "MLP": "mlp", "DTS": "dts", "DTS-ES": "dts", "DTS-HD Master Audio": "dts",
    "DTS-HD High Resolution Audio": "dts", "DTS:X": "dts", "AAC": "aac", "FLAC": "flac",
}
NO_FEATURE_MARK = "没有报告可用的视频标题"
DISC_MIN_SECONDS = 2400      # 短于 40 分钟的标题不当作正片


class DirectDiscUnsupported(Exception):
    """这张盘不能直接读（加密、双层杜比视界等），应改用 MakeMKV。"""


def _child_dir(parent: Path, name: str):
    """按名字找子目录，不区分大小写（BDMV / bdmv、PLAYLIST / playlist 都有人用）。"""
    try:
        for child in parent.iterdir():
            if child.is_dir() and child.name.casefold() == name.casefold():
                return child
    except OSError:
        pass
    return None


def disc_is_plain_folder(src):
    """是不是一张没有加密的蓝光目录。有 AACS 目录就当作加密盘。"""
    src = Path(src)
    if not src.is_dir():
        return False
    bdmv = _child_dir(src, "BDMV")
    if not bdmv or not _child_dir(bdmv, "PLAYLIST") or not _child_dir(bdmv, "STREAM"):
        return False
    return not (_child_dir(src, "AACS") or _child_dir(src, "MAKEMKV"))


def _mkvmerge_identify(path):
    try:
        out = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, timeout=180)
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except Exception:
        return None
    return data if (data.get("container") or {}).get("recognized") else None


def direct_disc_info(src):
    """读未加密蓝光目录里的播放列表，选出最长的正片，返回和 parse_makemkv_info 同样结构的摘要。"""
    src = Path(src)
    playlist_dir = _child_dir(_child_dir(src, "BDMV"), "PLAYLIST")
    playlists = sorted(p for p in playlist_dir.iterdir() if p.suffix.lower() == ".mpls")[:400]
    candidates = []
    for playlist in playlists:
        data = _mkvmerge_identify(playlist)
        if not data or not any(t.get("type") == "video" for t in data.get("tracks", [])):
            continue
        props = (data.get("container") or {}).get("properties") or {}
        duration = float(props.get("playlist_duration") or 0) / 1e9
        files = [Path(f) for f in props.get("playlist_file") or []]
        candidates.append((duration >= DISC_MIN_SECONDS, duration, int(props.get("playlist_size") or 0),
                           playlist, data, files))
    if not candidates:
        raise ValueError("这张盘里" + NO_FEATURE_MARK)
    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    long_enough, duration, _size, playlist, data, files = candidates[0]
    if not long_enough:
        raise ValueError("这张盘里" + NO_FEATURE_MARK)
    clips = []
    for clip in files:
        try:
            clips.append((clip.stat().st_size, clip))
        except OSError:
            pass
    if not clips:
        raise DirectDiscUnsupported("找不到正片的画面文件")
    main_clip = max(clips)[1]
    clip_probe = summarize_probe(ffprobe(main_clip))
    if not clip_probe or not clip_probe.get("video"):
        raise DirectDiscUnsupported("正片的画面文件读不出来（可能是加密的）")
    if clip_probe.get("video_count", 1) > 1:
        # 双层杜比视界：基础层和增强层是两条画面，需要 MakeMKV 合成一条
        raise DirectDiscUnsupported("正片里有两条画面（双层杜比视界）")
    summary = {"video": clip_probe["video"], "video_count": 1, "audio": [], "subs": [],
               "attachments": 0, "duration": duration}
    for track in data.get("tracks", []):
        props = track.get("properties") or {}
        language = props.get("language") or "und"
        if track.get("type") == "audio":
            name = str(track.get("codec") or "")
            summary["audio"].append({
                "codec": MKVMERGE_AUDIO_CODECS.get(name, name.lower() or "?"),
                "channels": int(props.get("audio_channels") or 2), "lang": language, "title": "",
                "profile": "DTS-HD MA" if "Master" in name else ("Atmos" if "Atmos" in name else ""),
                "default": False, "mkv_id": track.get("id"),
            })
        elif track.get("type") == "subtitles":
            name = str(track.get("codec") or "")
            summary["subs"].append({
                "codec": PGS_CODEC if "PGS" in name else name.lower() or "?",
                "lang": language, "title": "", "forced": False, "default": False,
                "mkv_id": track.get("id"),
            })
    summary["disc_preview"] = {
        "title": playlist.name, "title_name": playlist.name,
        "duration_h": _disc_duration_label(duration),
        "similar_titles": sum(1 for c in candidates if c[1] >= duration * 0.9),
        "backend": "direct", "playlist": str(playlist),
    }
    return summary


def read_disc_info(src):
    """原盘预读的统一入口：未加密的蓝光目录直接读，其余交给 MakeMKV。"""
    if disc_is_plain_folder(src):
        try:
            return direct_disc_info(src)
        except DirectDiscUnsupported as exc:
            print(f"直接读盘不适用，改用 MakeMKV: {exc}", flush=True)
    return parse_makemkv_info(makemkv_info(src))


def direct_disc_rip(job, info, out: Path, selection=None):
    """用 mkvmerge 按播放列表把正片取成 MKV。selection 给出时只带选中的音轨和字幕。

    视频、音频、字幕都是原样复制，章节和语言标记来自播放列表。
    """
    preview = info["disc_preview"]
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["mkvmerge", "--gui-mode", "-o", str(out)]
    if selection is not None:
        audio_ids = [info["audio"][i]["mkv_id"] for i in selection["audio"]]
        sub_ids = [info["subs"][j]["mkv_id"] for j in selection["subs"]]
        cmd += (["--audio-tracks", ",".join(str(i) for i in audio_ids)] if audio_ids else ["--no-audio"])
        cmd += (["--subtitle-tracks", ",".join(str(i) for i in sub_ids)] if sub_ids else ["--no-subtitles"])
        for position, track_id in enumerate(audio_ids):
            cmd += ["--default-track-flag", f"{track_id}:{'yes' if position == 0 else 'no'}"]
        for position, track_id in enumerate(sub_ids):
            cmd += ["--default-track-flag", f"{track_id}:{'yes' if position == 0 else 'no'}"]
    cmd.append(preview["playlist"])
    set_phase(job, "取出原盘正片", 0)
    log(job, f"直接读取原盘正片（{preview['title']}，{preview.get('duration_h') or ''}），不经 MakeMKV")
    def progress(line):
        match = re.search(r"progress\s+(\d+)%", line)
        if match:
            job["percent"] = min(99, int(match.group(1)))
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            detail = read_process_progress(job, process, progress)
            if process.returncode not in (0, 1):
                raise RuntimeError("读取原盘正片失败: " + detail[-600:])
            if process.returncode == 1:
                add_warning(job, "读取原盘时有警告，请验片: " + detail[-400:])
        finally:
            stop_local_process(process)
    media = summarize_probe(ffprobe(out))
    if not media or not media.get("video") or media.get("duration", 0) <= 0:
        raise RuntimeError("取出的正片读不出来（这张盘可能是加密的）")
    if abs(media["duration"] - info["duration"]) > 2:
        raise RuntimeError("取出的正片时长和盘里记录的不一致，拒绝入库")
    job["percent"] = 100
    return out


def rip_disc(job, src: Path, outdir: Path):
    """原盘提取的统一入口；返回 (取出的 MKV, 预读摘要)。"""
    if disc_is_plain_folder(src):
        try:
            info = direct_disc_info(src)
        except DirectDiscUnsupported as exc:
            log(job, f"这张盘不能直接读（{exc}），改用 MakeMKV")
        else:
            if info["disc_preview"].get("similar_titles", 0) > 1:
                add_warning(job, "原盘有多个时长接近的标题，本次只取最长的；它可能不是所需剪辑版，请验片")
            return direct_disc_rip(job, info, outdir / "title.mkv"), info
    return makemkv_rip(job, src, outdir), None


def cached_disc_preview(kind, payload, size):
    signature = disc_preview_signature(kind, payload, size)
    cached = DISC_PREVIEW_CACHE.get(signature) or {}
    probe = cached.get("probe")
    return probe if probe and probe.get("video") else None


def schedule_disc_preview(name, kind, payload, size):
    """同一原盘只排队一次；真正读取由单独线程串行执行，保护 NAS。"""
    signature = disc_preview_signature(kind, payload, size)
    if signature in DISC_PREVIEW_PENDING:
        return
    DISC_PREVIEW_PENDING.add(signature)
    DISC_PREVIEW_QUEUE.append((name, kind, str(payload), int(size), signature))


def disc_preview_loop():
    global DISC_PREVIEW_ACTIVE
    while True:
        task = None
        with LOCK:
            if (not CURRENT_JOB and not JOB_QUEUE and DISC_PREVIEW_QUEUE
                    and not ACTIVE_OPERATIONS and not processing_block_reason()):
                task = DISC_PREVIEW_QUEUE.pop(0)
                name, _kind, _payload, _size, signature = task
                DISC_PREVIEW_ACTIVE = name
                item = ITEMS.get(name)
                if item:
                    item["preview_status"] = "analyzing"
                    item["preview_error"] = None
        if task is None:
            time.sleep(1)
            continue

        name, kind, payload, size, signature = task
        try:
            probe = read_disc_info(payload)
            with LOCK:
                item = ITEMS.get(name)
                current_signature = (
                    disc_preview_signature(item["kind"], item["payload"], item["size"])
                    if item else None
                )
                if item and current_signature == signature:
                    item["probe"] = probe
                    item["preview_status"] = "ready"
                    item["preview_error"] = None
                    item["plan"], item["clean"] = build_plan(kind, probe)
                    DISC_PREVIEW_CACHE[signature] = {
                        "probe": probe,
                        "updated_epoch": time.time(),
                    }
                    save_disc_preview_cache()
        except Exception as exc:
            with LOCK:
                item = ITEMS.get(name)
                if item:
                    no_feature = NO_FEATURE_MARK in str(exc)
                    item["preview_status"] = "no_feature" if no_feature else "failed"
                    item["preview_error"] = NO_FEATURE_REASON if no_feature else str(exc)[:500]
        finally:
            with LOCK:
                DISC_PREVIEW_PENDING.discard(signature)
                DISC_PREVIEW_ACTIVE = None


# ---------- 扫描线程 ----------
def scan_loop():
    while True:
        try:
            scan_once()
        except Exception:
            traceback.print_exc()
        try:
            recover_interrupted_upgrade(startup=False)
        except Exception:
            traceback.print_exc()
        time.sleep(SCAN_INTERVAL)


NO_FEATURE_REASON = "这张盘里没有 40 分钟以上的正片（多半是花絮盘），不处理"


def discover_sources():
    """列出 download 里的全部条目：(名字, 类型, 待处理路径, 源根路径, 拦截原因, 大小)。

    一个下载目录里有多张光盘时，每张盘各成一个条目，名字是“目录/子盘”，源根路径是
    这张盘自己的目录或镜像文件——处理、核对和删除都只针对这一张，不用手动拆目录。
    """
    found = []
    for entry in sorted(INBOX.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.resolve() == WORK.resolve():   # 跳过施工区 download/dvwork
            continue
        if entry.is_dir():
            disc_sources = find_disc_sources(entry)
            if len(disc_sources) == 1:
                kind, payload = disc_sources[0]
                found.append((entry.name, kind, payload, entry, None, dir_size(entry)))
            elif len(disc_sources) > 1:
                for kind, path in disc_sources:
                    if path == entry:
                        found.append((entry.name, "multi_disc", entry, entry,
                                      "光盘根目录里还混着别的光盘，请手动整理后再处理", dir_size(entry)))
                        continue
                    try:
                        size = path.stat().st_size if path.is_file() else dir_size(path)
                    except OSError:
                        continue
                    found.append((f"{entry.name}/{path.relative_to(entry)}", kind, path, path, None, size))
            else:
                pv = largest_video_in(entry)
                if not pv:
                    continue
                blocked = ("目录里有多个视频，请先拆成各自的目录（包括样片），避免遗漏或误删"
                           if multiple_video_files(entry) else None)
                found.append((entry.name, "dir", pv, entry, blocked, dir_size(entry)))
        else:
            suffix = entry.suffix.lower()
            if suffix in DISC_IMAGE_EXT:
                kind = "iso"
            elif suffix in VIDEO_EXT:
                kind = "file"
            else:
                continue
            try:
                found.append((entry.name, kind, entry, entry, None, entry.stat().st_size))
            except OSError:
                continue
    return found


OUTPUT_INFO = {}          # 成品路径 -> 文件状态、探测结果、外挂数量；扫描线程填，页面只读
SIDECAR_EXT = {".srt", ".ass", ".ssa", ".sup"}


def count_output_sidecars(stems_by_folder):
    """每个目录只列一次。片名有共同前缀时，字幕属于匹配最长的那部。"""
    for folder, counts in stems_by_folder.items():
        try:
            for entry in folder.iterdir():
                if entry.suffix.lower() not in SIDECAR_EXT or not entry.is_file():
                    continue
                stem = entry.stem
                while stem:
                    if stem in counts:
                        counts[stem] += 1
                        break
                    stem = stem.rpartition(".")[0]
        except OSError:
            for stem in counts:
                counts[stem] = None


def refresh_output_info(paths, budget=2):
    """给已入库的影片读一次基本信息（格式、分辨率、声音、字幕），缓存起来。

    只读文件头，很快；每轮最多新读 budget 部，免得影片库很大时拖慢扫描。文件变了会重读。
    """
    wanted = {}
    stems_by_folder = {}
    for path in dict.fromkeys(map(Path, paths)):
        try:
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            wanted[path] = (info.st_size, info.st_mtime_ns)
            stems_by_folder.setdefault(path.parent, {})[path.stem] = 0
    count_output_sidecars(stems_by_folder)
    with LOCK:
        for stale in OUTPUT_INFO.keys() - wanted.keys():
            OUTPUT_INFO.pop(stale, None)
        todo = []
        for path, stamp in wanted.items():
            known = OUTPUT_INFO.get(path) or {}
            if known.get("stamp") != stamp:
                known = OUTPUT_INFO[path] = {"stamp": stamp, "probe": None, "tries": 0}
            known["external"] = stems_by_folder[path.parent][path.stem]
            if known["probe"] is None and known["tries"] < 3 and len(todo) < budget:
                todo.append((path, known))
    for path, known in todo:
        probe = summarize_probe(ffprobe(path))
        with LOCK:
            known["probe"] = probe
            known["tries"] += 1


JELLYFIN_EXTERNAL_SUBS = {}      # 成品路径 -> Jellyfin 里这部片的外挂字幕条数（打开字幕窗口时更新）


def output_info_lines(path):
    """卡片上给人看的三行：画面、声音、字幕。还没读到时返回 None。"""
    path = Path(path)
    with LOCK:
        known = OUTPUT_INFO.get(path) or {}
        probe = known.get("probe")
        external = known.get("external")
    if not probe or not probe.get("video"):
        return None
    container = path.suffix.lstrip(".").lower()
    picture = container.upper() + " · " + video_format_label(probe["video"])
    tracks = probe.get("audio") or []
    # 只写电视上该选的那一条：能直接播的里面，优先片子标了默认的；其余的用“等 N 条”带过
    def playable(track):
        return track.get("codec") not in AUDIO_TRANSCODE
    if tracks:
        pick = (next((t for t in tracks if playable(t) and t.get("default")), None)
                or next((t for t in tracks if playable(t)), None)
                or next((t for t in tracks if t.get("default")), None) or tracks[0])
        audio = audio_choice_label(pick) + ("" if playable(pick) else "（电视播不了）")
        if len(tracks) > 1:
            audio += f" 等 {len(tracks)} 条"
    else:
        pick = None
        audio = "没有声音"
    embedded = len(probe.get("subs") or [])
    # Jellyfin 自己下载的字幕默认存在它的资料目录里，不在影片旁边；按它报的条数算，卡片才对得上
    with LOCK:
        in_jellyfin = JELLYFIN_EXTERNAL_SUBS.get(str(path.resolve()))
    if in_jellyfin is not None and (external is None or in_jellyfin > external):
        external = in_jellyfin
    parts = ([f"外挂 {external} 条"] if external else []) + ([f"影片里 {embedded} 条"] if embedded else [])
    if external is None:
        parts.append("无法读取外挂字幕目录")
    return {"picture": picture, "audio": audio, "subs": "，".join(parts) or "没有字幕",
            "badges": media_badges(probe, pick)}


LIBRARY_KIND = "library"
LIBRARY_PREFIX = "影片库/"


def library_movies():
    """影片库（Movies）里的影片：顶层的视频文件，以及只装着一部影片的子文件夹。

    装着很多集的文件夹（剧集）不列出来，免得把列表淹掉。返回 [(显示名, 路径, 大小)]。
    """
    found = []
    try:
        entries = sorted(MOVIES.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name.startswith((".", "#", "@")):
            continue
        try:
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXT:
                found.append((entry.name, entry, entry.stat().st_size))
            elif entry.is_dir():
                videos = [f for f in sorted(entry.iterdir())
                          if f.is_file() and f.suffix.lower() in VIDEO_EXT and not f.name.startswith(".")]
                if len(videos) == 1:
                    found.append((f"{entry.name}/{videos[0].name}", videos[0], videos[0].stat().st_size))
        except OSError:
            continue
    return found


def sync_library_items(seen):
    """把影片库里的影片登记成“已入库”的条目，这样删掉源文件之后还能继续给它加字幕。

    已经有对应源条目（本工具转出来、源还在）的不重复登记。只读不改影片库里的文件。
    """
    with LOCK:
        claimed = {str(Path(PROCESSED[name].get("output", ""))) for name in seen
                   if name in PROCESSED and not PROCESSED[name].get("library")}
    listed = set()
    for label, path, size in library_movies():
        if str(path) in claimed:
            continue
        name = LIBRARY_PREFIX + label
        listed.add(name)
        with LOCK:
            previous = ITEMS.get(name) or {}
            ITEMS[name] = {
                "name": name, "kind": LIBRARY_KIND, "payload": str(path), "root": str(path),
                "size": size, "status": "done", "probe": previous.get("probe"), "plan": [], "clean": True,
                "error": None, "preview_status": None, "preview_error": None,
                "source_changed": False, "optional": False,
            }
            record = PROCESSED.get(name)
            if not record or record.get("output") != str(path):
                PROCESSED[name] = {"output": str(path), "library": True}
    with LOCK:
        for name in [n for n, record in PROCESSED.items() if record.get("library") and n not in listed]:
            PROCESSED.pop(name, None)
    return listed


def scan_once():
    INBOX.mkdir(parents=True, exist_ok=True)
    seen = set()
    for name, kind, payload, root, blocked_reason, size in discover_sources():
        seen.add(name)

        # ffprobe 可能要读机械盘上的大文件（最长 180 秒），必须在全局锁之外执行，
        # 否则页面状态接口会跟着卡住。这里先在锁内判断要不要探测。
        with LOCK:
            prev = SIZE_HISTORY.get(name)
            it = ITEMS.get(name)
            failure = PROBE_FAILURES.get(name)
            if failure and failure["size"] != size:
                PROBE_FAILURES.pop(name, None)
                failure = None
            need_probe = (
                kind not in DISC_KINDS and not blocked_reason
                and name not in PROCESSED and prev == size
                and not (it and it["status"] in ("processing", "queued", "error"))
                and not (it and it.get("probe"))
                and not (failure and time.time() < failure["retry_at"])
            )
        fresh_probe = summarize_probe(ffprobe(payload)) if need_probe else None

        with LOCK:
            if need_probe and fresh_probe is None:
                # 探测不了的文件不再每 8 秒重读一次：30 秒起步，逐次翻倍到 10 分钟。
                count = (PROBE_FAILURES.get(name) or {}).get("count", 0) + 1
                PROBE_FAILURES[name] = {
                    "size": size, "count": count,
                    "retry_at": time.time() + min(600, 30 * 2 ** (count - 1)),
                }
            elif fresh_probe is not None:
                PROBE_FAILURES.pop(name, None)
            prev = SIZE_HISTORY.get(name)
            stable = prev is not None and prev == size
            SIZE_HISTORY[name] = size
            it = ITEMS.get(name)
            if it and it["status"] in ("processing", "queued"):
                it["size"] = size
                continue
            source_changed = False
            if blocked_reason:
                status = "blocked"
            elif name in PROCESSED and prev is not None and prev != size:
                # 已完成任务的源正在变大/变小：多半是手动删源后又下载了同名内容。
                # 记录保留，但先如实显示“下载中”，不冒充已完成。
                status = "growing"
            elif name in PROCESSED:
                status = "done"
                recorded_size = PROCESSED[name].get("source_size")
                source_changed = recorded_size is not None and recorded_size != size
            elif it and it["status"] == "error":
                status = "error"
            elif not stable:
                status = "growing"
            else:
                status = "ready"
            probe = it["probe"] if (it and it.get("probe") and prev == size) else None
            preview_status = (it or {}).get("preview_status")
            preview_error = (it or {}).get("preview_error")
            if prev != size:
                preview_status = None
                preview_error = None
            if status == "ready" and probe is None and kind not in DISC_KINDS:
                probe = fresh_probe
            if kind in DISC_KINDS and status in ("ready", "done", "error"):
                cached = probe or cached_disc_preview(kind, payload, size)
                if cached:
                    probe = cached
                    preview_status = "ready"
                    preview_error = None
                elif preview_status not in {"queued", "analyzing", "failed", "no_feature"}:
                    schedule_disc_preview(name, kind, payload, size)
                    preview_status = "queued"
                    preview_error = None
            if status == "blocked":
                actions, clean = ["需手动整理" if kind == "multi_disc" else "需拆分多个视频"], False
            else:
                actions, clean = build_plan(kind, probe, payload) if status in ("ready", "done", "error") else ([], False)
            ITEMS[name] = {
                "name": name, "kind": kind, "payload": str(payload), "root": str(root),
                "size": size, "status": status, "probe": probe,
                "plan": actions, "clean": clean,
                "error": blocked_reason or ((it or {}).get("error") if status == "error" else None),
                "preview_status": preview_status,
                "preview_error": preview_error,
                "source_changed": source_changed,
                "optional": (status in ("ready", "error")
                             and processing_optional(kind, probe, payload)),
            }
    seen |= sync_library_items(seen)
    with LOCK:
        outputs = [record.get("output") for name, record in PROCESSED.items() if name in seen and record.get("output")]
    refresh_output_info(outputs)
    with LOCK:
        for name in list(ITEMS):
            if name not in seen and ITEMS[name]["status"] != "processing":
                ITEMS.pop(name)
                SIZE_HISTORY.pop(name, None)
                PROBE_FAILURES.pop(name, None)


# ---------- 处理流水线 ----------
def log(job, msg):
    if len(msg) > 400:
        msg = msg[:400] + "…"
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    job["log"].append(line)
    if len(job["log"]) > 120:
        job["log"] = job["log"][-120:]


def add_warning(job, msg):
    """记录需在任务完成后继续展示的警告，同时写入实时日志。"""
    warnings = job.setdefault("warnings", [])
    if msg not in warnings:
        warnings.append(msg)
    log(job, "注意: " + msg)


def plan_steps(job, kind, is_p7=False, container="mkv", direct=False, **_legacy):
    """任务开始时规划步骤清单，前端据此显示进度。"""
    steps = []
    if kind in DISC_KINDS:
        steps.append("取出原盘正片")
    steps.append("探测流信息")
    if direct:
        steps.append("直接入库(复制中)")
    else:
        if is_p7:
            steps.append("DoVi P7→P8 转换")
        steps.append("封装MP4(保留DV)" if container == "mp4" else "封装MKV(视频直通)")
        steps.append("成品自检")
    steps.append("移入 Movies")
    job["steps"] = steps
    job["step_idx"] = 0


def set_phase(job, phase, percent=None):
    if job.get("phase") != phase:
        job["phase_start"] = time.time()   # 新阶段,重置计时用于ETA
        # 更新步骤索引:在已规划的steps里定位当前阶段
        steps = job.get("steps") or []
        if phase in steps:
            job["step_idx"] = steps.index(phase)
    job["phase"] = phase
    if percent is not None:
        job["percent"] = percent


def stop_local_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait(timeout=5)


def read_process_progress(job, process, on_line, extra_streams=(), tick=None):
    """合并消费进度与错误输出；即使没有输出也检查磁盘，不让管道互锁。"""
    tail = bytearray()
    pending = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        for stream in extra_streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            if tick:
                tick()
            free = disk_free()
            if free is not None and free < MIN_FREE_ABORT:
                raise RuntimeError(f"磁盘空间告急(仅剩 {human(free)})，已停止当前处理")
            for key, _events in selector.select(timeout=1):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                tail.extend(chunk)
                del tail[:-8192]
                pending.extend(chunk)
                lines = re.split(br"[\r\n]", pending)
                pending = bytearray(lines.pop()[-8192:])
                for line in lines:
                    on_line(line.decode("utf-8", "replace").strip())
    if pending:
        on_line(pending.decode("utf-8", "replace").strip())
    while process.poll() is None:
        free = disk_free()
        if free is not None and free < MIN_FREE_ABORT:
            raise RuntimeError("磁盘空间不足，已停止当前处理")
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    return tail.decode("utf-8", "replace")[-1600:]


def run_ffmpeg_progress(job, cmd, total_duration, phase):
    set_phase(job, phase, 0)
    def progress(line):
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                if total_duration > 0:
                    job["percent"] = min(99, int(line.split("=")[1]) / 1e6 / total_duration * 100)
            except ValueError:
                pass
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            detail = read_process_progress(job, process, progress)
            if process.returncode != 0:
                raise RuntimeError(f"ffmpeg 失败({phase}): {detail}")
        finally:
            stop_local_process(process)
    job["percent"] = 100


def _dir_size(p: Path):
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def makemkv_source_spec(src: Path):
    """生成 makemkvcon 输入参数: 文件夹用 file:, 光盘镜像必须用 iso:。"""
    if src.is_file() and src.suffix.lower() in DISC_IMAGE_EXT:
        return f"iso:{src}"
    return f"file:{src}"


def source_size(src: Path):
    """MakeMKV 输入大小,用于提取进度估算。"""
    try:
        return src.stat().st_size if src.is_file() else _dir_size(src)
    except OSError:
        return 0


BRACKET_NOISE_RE = re.compile(
    r"^(bdmv|bd|bdrom|bd-?box|uhd|uhd-?bd|iso|movie|ova|disc ?\d*|\d{4,8}|\d{3,4}[pi]|4k|2160p|hevc|avc|h\.?26[45]|remux|"
    r"自购|自抓|原盘|蓝光|剧场版)$", re.I)


def clean_release_name(text):
    """把“[BDMV][230308][Shin Evangelion Gekijouban][Movie]”这种全是方括号标签的名字整理成片名。

    NAS 实测：成品沿用这种名字时，Jellyfin 把片名认成“[BDMV”，匹配不到影片资料，也搜不到字幕。
    只处理以方括号开头的名字；去掉 BDMV、日期、Movie 这类标签，剩下的当片名。整理不出来就原样返回。
    """
    text = str(text)
    if not text.lstrip().startswith(("[", "【")):
        return text
    outside = re.sub(r"[\[【][^\]】]*[\]】]", " ", text).strip(" -_.")
    if outside:
        return re.sub(r"\s+", " ", outside)
    groups = [g.strip() for g in re.findall(r"[\[【]([^\]】]*)[\]】]", text)]
    kept = [g for g in groups if g and not BRACKET_NOISE_RE.match(g)]
    return " ".join(kept) or text


def output_stem(name, kind):
    """生成成品基础名；ISO 与普通文件去掉扩展名，多盘目录里的子盘用“目录 - 子盘”。"""
    parts = name.split("/")
    if kind in ("file", "iso"):
        parts[-1] = Path(parts[-1]).stem
    parts[0] = clean_release_name(parts[0])
    return " - ".join(parts)


def choose_main_title(job, mkvs):
    """按时长优先、大小兜底选择电影正片，并记录多标题歧义供用户验片。"""
    ranked = []
    for path in mkvs:
        probe = summarize_probe(ffprobe(path))
        duration = probe["duration"] if probe else 0
        ranked.append((duration, path.stat().st_size, path))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    chosen = ranked[0][2]
    if len(ranked) > 1:
        summary = "；".join(
            f"{path.name} {duration / 60:.0f}分钟/{human(size)}"
            for duration, size, path in ranked[:5]
        )
        log(job, f"MakeMKV 产出多个长标题，按时长优先选择正片: {summary}")
        if ranked[1][0] >= ranked[0][0] * 0.9:
            add_warning(job, "存在时长接近的正片候选，可能是不同剪辑版、分支播放列表或剧集碟；"
                        "本次按最长标题处理，请重点验片后再删源")
    return chosen


def stop_makemkv_exec(client, container, exec_id, pidfile, outdir):
    """只终止本任务的 PID；用唯一输出目录校验进程身份，绝不 killall。"""
    script = r"""
    task_pid=$(cat "$1" 2>/dev/null) || exit 1
    case "$task_pid" in ''|*[!0-9]*) exit 1;; esac
    [ "$task_pid" -gt 1 ] || exit 1
    tr '\000' '\n' < "/proc/$task_pid/cmdline" | grep -Fx -- "$2" >/dev/null || exit 1
    kill -s KILL "-$task_pid"
    """
    for attempt in range(20):
        if not client.api.exec_inspect(exec_id).get("Running"):
            return True
        # 低空间紧急停止整个独立进程组，避免辅助子进程继续写入。
        container.exec_run(["/bin/sh", "-c", script, "panel-stop",
                            str(pidfile), str(outdir)])
        time.sleep(0.5)
    return not client.api.exec_inspect(exec_id).get("Running")


MAKEMKV_SUMMARY_CODES = {5004, 5005, 5036, 5037}


def verify_makemkv_log(logfile):
    """MakeMKV 普通提取失败可能仍返回 0；检查与界面语言无关的机器人消息码。"""
    failed = False
    saved = False
    if not logfile.is_file():
        raise RuntimeError("MakeMKV 缺少提取结果日志，无法确认成功")
    with logfile.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.startswith("MSG:"):
                continue
            try:
                fields = next(csv.reader([line[4:]], escapechar="\\"))
                code = int(fields[0])
                if code == 5003:
                    failed = True
                # 结果汇总有两种形态：全部成功时只带“已保存数”一个参数
                # （如 5036 Copy complete. %1 titles saved.），有失败时才多带
                # “失败数”（如 5037 … %1 titles saved, %2 failed.）。两种都要认，
                # 否则每一次正常提取都会被当成残片拒绝。5014“Saving %1 titles
                # into directory”只是开始提示，不在此列。
                if code in MAKEMKV_SUMMARY_CODES and len(fields) >= 6:
                    saved_count = int(fields[5])
                    failed_count = 0
                    if len(fields) >= 7:
                        failed_count = int(fields[6])
                    saved = saved or saved_count > 0
                    failed = failed or failed_count > 0
            except (ValueError, IndexError, csv.Error):
                continue
    if failed or not saved:
        raise RuntimeError("MakeMKV 未确认标题完整保存或报告了提取失败，拒绝使用残片")


def makemkv_rip(job, src: Path, outdir: Path):
    """使用可检查退出码、可定向停止的 Docker exec，按文件大小显示进度。"""
    import docker
    global PIPELINE_HALT
    set_phase(job, "取出原盘正片", 0)
    outdir.mkdir(parents=True, exist_ok=True)
    with MAKEMKV_LOCK:
        expected = parse_makemkv_info(makemkv_info(src))
        if expected["disc_preview"].get("similar_titles", 0) > 1:
            add_warning(job, "原盘有多个时长接近的标题，本次只提取最长候选；它可能不是所需剪辑版，请验片")
        client = docker.from_env()
        container = client.containers.get(MAKEMKV_CONTAINER)
        pidfile = outdir / ".makemkv.pid"
        logfile = outdir / ".makemkv.log"
        command = [MAKEMKVCON, "-r", "--minlength=2400",
                   "mkv", makemkv_source_spec(src),
                   str(expected["disc_preview"]["title"]), str(outdir)]
        wrapper = 'echo $$ > "$1"; task_log="$2"; shift 2; exec "$@" > "$task_log" 2>&1'
        # 非交互 shell 在后台派生一个非进程组首进程，再调用 setsid；
        # 避免 BusyBox setsid 自行 fork 后提前返回（它没有 util-linux 的 -w）。
        supervisor = 'setsid "$@" & task_child=$!; wait "$task_child"'
        exec_id = client.api.exec_create(
            container.id, ["/bin/sh", "-c", supervisor, "panel-supervisor",
                           "/bin/sh", "-c", wrapper, "panel-rip",
                           str(pidfile), str(logfile)] + command,
            environment=MAKEMKV_ENV,
        )["Id"]
        src_size = max(source_size(src), 1)
        log(job, "MakeMKV 开始提取；只在正常退出并通过自检后接受成品")
        try:
            client.api.exec_start(exec_id, detach=True)
            while True:
                info = client.api.exec_inspect(exec_id)
                if not info.get("Running"):
                    break
                free = disk_free()
                if free is not None and free < MIN_FREE_ABORT:
                    raise RuntimeError(f"磁盘空间告急(仅剩 {human(free)})，停止提取")
                job["percent"] = min(99, _dir_size(outdir) / src_size * 100)
                time.sleep(1)
            detail = ""
            if logfile.exists():
                with logfile.open("rb") as stream:
                    stream.seek(max(0, logfile.stat().st_size - 1600))
                    detail = stream.read().decode("utf-8", "replace")
            if info.get("ExitCode") != 0:
                raise RuntimeError(f"MakeMKV 提取失败（退出码 {info.get('ExitCode')}），拒绝使用残片: {detail}")
            verify_makemkv_log(logfile)
        except Exception:
            try:
                stopped = stop_makemkv_exec(client, container, exec_id, pidfile, outdir)
            except Exception:
                stopped = False
            if not stopped:
                job["preserve_work"] = True
                reason = "无法确认 MakeMKV 已停止，队列已暂停；请检查 makemkv 容器后清除 dvwork/.pipeline_block.txt"
                try:
                    (WORK / ".pipeline_block.txt").write_text(reason, "utf-8")
                except OSError:
                    PIPELINE_HALT = reason + "；暂停标记无法写入，请人工检查后重启处理台"
                add_warning(job, reason)
            raise
        finally:
            client.close()
    mkvs = list(outdir.glob("*.mkv"))
    if not mkvs:
        raise RuntimeError("MakeMKV 没有产出 MKV；请检查原盘、加密/注册码或标题时长")
    main_title = choose_main_title(job, mkvs)
    media = summarize_probe(ffprobe(main_title))
    if not media or not media.get("video") or media.get("duration", 0) <= 0:
        raise RuntimeError("MakeMKV 正片无法解析或没有有效时长")
    if abs(media["duration"] - expected["duration"]) > 2:
        raise RuntimeError("MakeMKV 提取时长与原盘预读不符，拒绝将不完整正片入库")
    job["percent"] = 100
    log(job, f"提取完成，选正片: {main_title.name} ({human(main_title.stat().st_size)})")
    for extra in mkvs:
        if extra != main_title:
            extra.unlink(missing_ok=True)
    return main_title


def dovi_convert(job, src: Path, jobdir: Path):
    """P7→P8.1 保留 BL+RPU，丢弃 EL；同时消费两个进程的输出并监控空间。"""
    bl = jobdir / "BL_P8.hevc"
    set_phase(job, "DoVi P7→P8 转换", 0)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
         "-f", "hevc", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    dv = None
    try:
        dv = subprocess.Popen(
            [DOVI_TOOL, "-m", "2", "convert", "--discard", "-", "-o", str(bl)],
            stdin=ff.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        ff.stdout.close()
        est = max(src.stat().st_size, 1)
        def progress():
            if bl.exists():
                job["percent"] = min(99, bl.stat().st_size / est * 100)
        detail = read_process_progress(job, dv, lambda line: None,
                                       extra_streams=(ff.stderr,), tick=progress)
        ff.wait(timeout=5)
        if dv.returncode != 0 or ff.returncode != 0:
            raise RuntimeError(f"dovi_tool 转换失败: {detail}")
    finally:
        if dv is not None:
            stop_local_process(dv)
            dv.stdout.close()
        stop_local_process(ff)
        ff.stdout.close()
        ff.stderr.close()
    job["percent"] = 100
    log(job, f"BL+P8 RPU 完成: {human(bl.stat().st_size)}")
    return bl


def rebuild_bl_timestamps(job, bl: Path, fps, jobdir: Path):
    """把无时间戳的 BL 裸流用 mkvmerge 包成带正确时间戳的临时 MKV。

    ffmpeg 直接 `-r fps -i 裸流 -c copy` 写 MP4 时不解析帧重排：每帧的显示时间
    被写成解码时间，B 帧顺序信息丢失，片头还会有帧被标成丢弃（ffmpeg 自己也警告
    “pts has no value / Timestamps are unset”）。mkvmerge 会解析 HEVC 码流的显示
    顺序并按固定帧率生成时间戳，之后 ffmpeg 再从这个 MKV copy 进 MP4 即可。
    成功后删除裸流，磁盘峰值不高于原流程。
    """
    out = jobdir / "BL_P8.mkv"
    set_phase(job, "DoVi P7→P8 转换", 99)
    cmd = ["mkvmerge", "--gui-mode", "-o", str(out),
           "--default-duration", f"0:{fps}fps", str(bl)]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            detail = read_process_progress(job, process, lambda line: None)
            if process.returncode not in (0, 1):
                raise RuntimeError("mkvmerge 重建 BL 时间戳失败: " + detail)
            if process.returncode == 1:
                add_warning(job, "mkvmerge 重建 BL 时间戳时有警告: " + detail[-600:])
        finally:
            stop_local_process(process)
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError("mkvmerge 没有产出带时间戳的 BL 文件")
    bl.unlink(missing_ok=True)
    log(job, f"BL 时间戳已重建: {human(out.stat().st_size)}")
    return out


# 成品里两条声音在电视音轨菜单里显示的名字：让人一眼知道该选哪条
COMPAT_TRACK_TITLE = "电视用"
ORIGINAL_TRACK_TITLE = "原始音质（电视播不了，留给功放或回音壁）"


def build_audio_args(probe, audio_indices, container, ain="0"):
    """为选中的音轨生成 -map 与编码参数；返回 (参数列表, 成品音轨条数)。

    电视能直接播的原样复制。解不了的加一条 EAC3 1536k 兼容轨放在前面，原轨（能放进
    该容器时）紧跟其后。成品第一条音轨设为默认，其余都不是默认。
    """
    args = []
    keep = set(kept_original_tracks(probe, container, audio_indices))
    out = 0
    def disposition():
        return [f"-disposition:a:{out}", "default" if out == 0 else "0"]
    for index in audio_indices:
        track = probe["audio"][index]
        args += ["-map", f"{ain}:a:{index}"]
        if audio_tv_direct(track["codec"], container, track.get("profile")):
            args += [f"-c:a:{out}", "copy"] + disposition()
            out += 1
            continue
        encoder, bitrate = audio_target(track["codec"], track.get("profile"))
        args += [f"-c:a:{out}", encoder, f"-b:a:{out}", bitrate]
        if int(track.get("channels") or 0) > 6:
            args += [f"-ac:a:{out}", "6"]
        if index in keep:
            args += [f"-metadata:s:a:{out}", f"title={COMPAT_TRACK_TITLE}"]
        args += disposition()
        out += 1
        if index in keep:
            args += ["-map", f"{ain}:a:{index}", f"-metadata:s:a:{out}", f"title={ORIGINAL_TRACK_TITLE}",
                     f"-c:a:{out}", "copy"] + disposition()
            out += 1
    return args, out


def sidecar_format(codec):
    """这种字幕做成外挂文件时用什么格式：(扩展名, ffmpeg 编码, ffmpeg 格式)；做不了返回 None。"""
    codec = (codec or "").lower()
    if codec == PGS_CODEC:
        return ("sup", "copy", "sup")
    if codec in ("ass", "ssa"):
        return ("ass", "copy", "ass")
    if codec in GRAPHIC_SUB_CODECS:          # DVD 字幕等：电视显示不了
        return None
    return ("srt", "srt", "srt")


def subtitle_sidecar_outputs(out: Path, probe, sub_indices, ain="0"):
    """把选中的字幕各存成一个与成品同名的外挂文件，和成品在同一次 ffmpeg 里写出。

    返回 (追加在主输出之后的 ffmpeg 参数, [外挂文件路径])。文件名形如 片名.chi.srt，
    同语言有多条时是 片名.chi.2.srt。
    """
    args, paths = [], []
    container = out.suffix.lstrip(".").lower() or "mkv"
    for index in sub_indices:
        sub = probe["subs"][index]
        fmt = sidecar_format(sub.get("codec"))
        if not fmt or subtitle_mode(sub.get("codec"), container) != "sidecar":
            continue
        ext, encoder, muxer = fmt
        language = re.sub(r"[^A-Za-z-]", "", str(sub.get("lang") or "")) or "und"
        sidecar = out.with_name(f"{out.stem}.{language}.{ext}")
        serial = 2
        while sidecar in paths:
            sidecar = out.with_name(f"{out.stem}.{language}.{serial}.{ext}")
            serial += 1
        paths.append(sidecar)
        args += ["-map", f"{ain}:s:{index}", "-c:s", encoder, "-f", muxer, str(sidecar)]
    return args, paths


def audio_sub_args(probe, selection=None):
    """MKV 成品的音频/章节参数；返回 (参数列表, 音轨条数, 内嵌字幕条数)。

    字幕默认不装进影片文件，由 subtitle_sidecar_outputs 另存为外挂文件；只有规则改成
    “留在影片文件里”的才在这里内嵌。
    """
    sel = normalize_selection(probe, selection)
    args, audio_count = build_audio_args(probe, sel["audio"], "mkv")
    # 规则选了“留在影片文件里”的字幕才内嵌（默认都是外挂，这里一条也没有）
    sub_count = 0
    needs_fonts = False
    for index in sel["subs"]:
        codec = (probe["subs"][index].get("codec") or "").lower()
        if subtitle_mode(codec, "mkv") != "embed":
            continue
        needs_fonts = needs_fonts or codec in ("ass", "ssa")
        args += ["-map", f"0:s:{index}",
                 f"-c:s:{sub_count}", "srt" if codec == "mov_text" else "copy",
                 f"-disposition:s:{sub_count}", "default" if sub_count == 0 else "0"]
        sub_count += 1
    if probe.get("attachments") and needs_fonts:
        args += ["-map", "0:t?", "-c:t", "copy"]
    args += ["-map_chapters", "0"]
    return args, audio_count, sub_count


def pgs_track_count(probe):
    """源里 PGS 图形字幕轨数量(要 OCR 的对象)"""
    return sum(1 for s in probe["subs"] if s["codec"] == "hdmv_pgs_subtitle")


def _pgs_tracks(payload: Path):
    """用 mkvmerge -J 拿到 PGS 字幕轨的真实 track id 与语言(track id 与 ffprobe 的流序号未必一致,以此为准)"""
    try:
        out = subprocess.run(["mkvmerge", "-J", str(payload)],
                             capture_output=True, timeout=120)
        data = json.loads(out.stdout.decode("utf-8", "replace"))
    except Exception:
        return []
    res = []
    for t in data.get("tracks", []):
        if t.get("type") != "subtitles":
            continue
        props = t.get("properties", {}) or {}
        if props.get("codec_id") == "S_HDMV/PGS":
            res.append({"id": t.get("id"),
                        "lang": (props.get("language") or "und")})
    return res


LANG_ALIASES = {
    "zho": "zho", "chi": "zho", "zh": "zho",
    "eng": "eng", "en": "eng",
    "jpn": "jpn", "ja": "jpn",
    "kor": "kor", "ko": "kor",
}
SRT_TIMECODE_RE = re.compile(
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
)
SRT_TIMECODE_CAPTURE_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def decode_subtitle_bytes(data):
    """把常见中文字幕编码统一为 UTF-8；拒绝无法合理解码的二进制文件。"""
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "big5"):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if SRT_TIMECODE_RE.search(text):
            return text
    raise ValueError("字幕不是可识别的 SRT，或字符编码不受支持")


def _srt_ms(parts):
    hour, minute, second, millis = (int(value) for value in parts)
    return ((hour * 60 + minute) * 60 + second) * 1000 + millis


def parse_srt_text(text):
    """解析 SRT 为 [{start,end,text}]，忽略损坏块但保留多行正文。"""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line.rstrip() for line in block.splitlines()]
        time_index = next(
            (idx for idx, line in enumerate(lines) if SRT_TIMECODE_CAPTURE_RE.search(line)),
            None,
        )
        if time_index is None:
            continue
        match = SRT_TIMECODE_CAPTURE_RE.search(lines[time_index])
        body = "\n".join(line.strip() for line in lines[time_index + 1:] if line.strip())
        body = re.sub(r"<[^>]+>", "", body).strip()
        if not body:
            continue
        start = _srt_ms(match.groups()[:4])
        end = _srt_ms(match.groups()[4:])
        if end <= start:
            continue
        cues.append({"start": start, "end": end, "text": body})
    cues.sort(key=lambda cue: (cue["start"], cue["end"]))
    return cues


def _format_srt_time(milliseconds):
    value = max(0, int(round(milliseconds)))
    hour, value = divmod(value, 3600000)
    minute, value = divmod(value, 60000)
    second, millis = divmod(value, 1000)
    return f"{hour:02d}:{minute:02d}:{second:02d},{millis:03d}"


def write_srt(path, cues):
    blocks = []
    for index, cue in enumerate(sorted(cues, key=lambda item: item["start"]), 1):
        blocks.append(
            f"{index}\n{_format_srt_time(cue['start'])} --> "
            f"{_format_srt_time(cue['end'])}\n{cue['text']}"
        )
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), "utf-8")


def subtitle_candidate_path(name):
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return WORK / "subtitle_candidates" / f"{digest}.zho.srt"


def subtitle_anchor_path(name):
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return WORK / "subtitle_anchors" / f"{digest}.jpn.srt"


def subtitle_sidecar_path(output):
    output = Path(output)
    return output.with_name(f"{output.stem}.zh-CN.srt")


def subtitle_audio_preview_paths(name):
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    root = WORK / "subtitle_sync_previews"
    return root / f"{digest}.srt", root / f"{digest}.json"


def subtitle_cues_digest(cues):
    normalized = [
        [int(cue["start"]), int(cue["end"]), str(cue["text"])]
        for cue in cues
    ]
    payload = json.dumps(
        normalized, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clear_subtitle_audio_preview(name):
    for path in subtitle_audio_preview_paths(name):
        path.unlink(missing_ok=True)


def save_subtitle_audio_preview(name, filename, output, aligned, cues, sync_info):
    preview, metadata = subtitle_audio_preview_paths(name)
    preview.parent.mkdir(parents=True, exist_ok=True)
    output_stat = Path(output).stat()
    staging = preview.with_name(f".{preview.name}.tmp")
    shutil.copy2(aligned, staging)
    os.replace(staging, preview)
    data = {
        "created": time.time(),
        "filename": filename,
        "candidate_digest": subtitle_cues_digest(cues),
        "output": str(Path(output).resolve()),
        "output_size": output_stat.st_size,
        "output_mtime_ns": output_stat.st_mtime_ns,
        "sync_info": sync_info,
    }
    temp = metadata.with_name(f".{metadata.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(temp, metadata)
    return preview


def load_subtitle_audio_preview(name, filename, output, cues):
    preview, metadata = subtitle_audio_preview_paths(name)
    try:
        data = json.loads(metadata.read_text("utf-8"))
        output_stat = Path(output).stat()
        valid = (
            preview.is_file()
            and time.time() - float(data.get("created", 0)) <= 10 * 60
            and data.get("filename") == filename
            and data.get("candidate_digest") == subtitle_cues_digest(cues)
            and data.get("output") == str(Path(output).resolve())
            and int(data.get("output_size", -1)) == output_stat.st_size
            and int(data.get("output_mtime_ns", -1)) == output_stat.st_mtime_ns
            and _inspect_srt(preview)
        )
    except (OSError, ValueError, TypeError):
        valid = False
        data = {}
    if not valid:
        clear_subtitle_audio_preview(name)
        return None
    return preview, dict(data.get("sync_info") or {})


def subtitle_preview_cues(path, limit=4):
    """返回少量安全的时间/文字预览，供人工确认偏移量。"""
    try:
        cues = parse_srt_text(Path(path).read_text("utf-8-sig"))
    except OSError:
        return []
    return [
        {
            "start_ms": int(cue["start"]),
            "end_ms": int(cue["end"]),
            "text": str(cue["text"])[:120],
        }
        for cue in cues[:limit]
    ]


def normalize_manual_subtitle_offset(value):
    """校验人工指定的最终总偏移；正数延后、负数提前。"""
    if value in (None, ""):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("手动字幕偏移必须是秒数") from exc
    if not math.isfinite(seconds):
        raise ValueError("手动字幕偏移必须是有限秒数")
    if abs(seconds) > 45 * 60:
        raise ValueError("手动字幕偏移不能超过正负 45 分钟")
    return round(seconds, 3)


def normalize_manual_first_start_ms(value, duration_ms=0):
    """校验用户指定的第一条字幕目标时间。"""
    if value in (None, ""):
        return None
    try:
        milliseconds = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("第一条字幕时间格式无效") from exc
    upper = int(duration_ms + 120_000) if duration_ms else 12 * 3600 * 1000
    if milliseconds < 0 or milliseconds > upper:
        raise ValueError("第一条字幕时间超出影片有效范围")
    return milliseconds


def shift_aligned_subtitle(path, delta_seconds, output):
    """在保留自动校轴细节的基础上整体平移字幕。"""
    cues = parse_srt_text(Path(path).read_text("utf-8-sig"))
    delta_ms = float(delta_seconds) * 1000
    shifted = []
    for cue in cues:
        start = cue["start"] + delta_ms
        end = cue["end"] + delta_ms
        if end <= 0:
            continue
        shifted.append({
            **cue,
            "start": max(0, start),
            "end": max(1, end),
        })
    if len(shifted) < 5:
        raise ValueError("手动偏移后有效字幕不足 5 条，请检查正负方向和秒数")
    write_srt(output, shifted)
    return output


def load_subtitle_candidate(name):
    path = subtitle_candidate_path(name)
    if not path.is_file():
        return None
    try:
        cues = parse_srt_text(path.read_text("utf-8-sig"))
    except OSError:
        return None
    return (path, cues) if cues else None


def persist_japanese_subtitle_anchor(name, ocr_srts):
    """保留首次 OCR 得到的日文时间轴，之后换字幕无需再读原盘。"""
    japanese = next((Path(path) for path, lang in ocr_srts if lang == "jpn"), None)
    if not japanese or not _inspect_srt(japanese):
        return None
    target = subtitle_anchor_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(japanese, target)
    return target


def extract_japanese_anchor_from_output(output, target):
    """兼容旧成品：从已内嵌的日文文本字幕提取时间轴作为校轴锚点。"""
    data = ffprobe(output)
    if not data:
        return None
    stream_index = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags", {}) or {}
        lang = LANG_ALIASES.get(str(tags.get("language", "")).lower(), "und")
        if lang == "jpn" and stream.get("codec_name") not in SUB_STRIP:
            stream_index = stream.get("index")
            break
    if stream_index is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(output),
            "-map", f"0:{stream_index}", "-c:s", "srt", str(target),
        ],
        capture_output=True, timeout=300,
    )
    if result.returncode != 0 or not _inspect_srt(target):
        target.unlink(missing_ok=True)
        return None
    return target


def _docker_exec_bytes(result):
    """兼容 Docker SDK demux/non-demux 两种 ExecResult 输出。"""
    output = result.output
    if isinstance(output, tuple):
        output = b"".join(part or b"" for part in output)
    return output or b""


def _parse_jellyfin_subtitle_listing(raw):
    """把 Jellyfin 容器内 find 的固定格式输出转成安全的前端选项。"""
    options = []
    seen = set()
    allowed_suffixes = {".srt", ".ass", ".ssa", ".vtt"}
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            mtime_text, size_text, path = line.split("\t", 2)
            mtime = float(mtime_text)
            size = int(size_text)
        except (ValueError, TypeError):
            continue
        if path in seen or Path(path).suffix.lower() not in allowed_suffixes:
            continue
        if not any(path.startswith(root + "/") for root in JELLYFIN_SUBTITLE_ROOTS):
            continue
        seen.add(path)
        subtitle_id = hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]
        options.append({
            "id": subtitle_id,
            "path": path,
            "filename": path.rsplit("/", 1)[-1],
            "size": size,
            "size_h": human(size),
            "mtime": mtime,
            "modified": time.strftime("%m-%d %H:%M", time.localtime(mtime)),
            "source": (
                "Jellyfin 元数据"
                if "/metadata/library/" in path else "Jellyfin 字幕缓存"
            ),
        })
    options.sort(key=lambda item: item["mtime"], reverse=True)
    return options


def jellyfin_subtitle_options(limit=100):
    """只读列出 Jellyfin 最近的文本字幕，不根据文件序号猜测字幕质量。"""
    import docker
    try:
        container = docker.from_env().containers.get(JELLYFIN_CONTAINER)
        command = (
            "find /config/metadata/library /config/data/subtitles -type f "
            "\\( -iname '*.srt' -o -iname '*.ass' -o -iname '*.ssa' "
            "-o -iname '*.vtt' \\) "
            "-printf '%T@\\t%s\\t%p\\n' 2>/dev/null"
        )
        result = container.exec_run(["sh", "-c", command], demux=True)
    except Exception as exc:
        raise RuntimeError("无法读取 Jellyfin 字幕，请确认 jellyfin 容器正在运行") from exc
    raw = _docker_exec_bytes(result)
    if result.exit_code not in (0, 1) and not raw:
        raise RuntimeError("Jellyfin 字幕目录读取失败")
    options = _parse_jellyfin_subtitle_listing(raw)[:max(1, min(int(limit), 200))]
    with LOCK:
        JELLYFIN_SUBTITLE_INDEX.clear()
        JELLYFIN_SUBTITLE_INDEX.update({
            item["id"]: item["path"] for item in options
        })
    return options


def jellyfin_subtitle_bytes(subtitle_id):
    """按发现列表中的不透明 ID 读取字幕，拒绝客户端直接提交容器路径。"""
    with LOCK:
        path = JELLYFIN_SUBTITLE_INDEX.get(subtitle_id)
    if not path:
        jellyfin_subtitle_options()
        with LOCK:
            path = JELLYFIN_SUBTITLE_INDEX.get(subtitle_id)
    if not path or not any(path.startswith(root + "/")
                           for root in JELLYFIN_SUBTITLE_ROOTS):
        raise ValueError("该 Jellyfin 字幕已经不存在，请刷新列表后重试")
    import docker
    try:
        container = docker.from_env().containers.get(JELLYFIN_CONTAINER)
        result = container.exec_run(["cat", path], demux=True)
    except Exception as exc:
        raise RuntimeError("读取 Jellyfin 字幕失败") from exc
    data = _docker_exec_bytes(result)
    if result.exit_code != 0 or not data:
        raise RuntimeError("Jellyfin 字幕为空或已经被清理")
    return path.rsplit("/", 1)[-1], data


def parse_subtitle_candidate_data(data):
    """校验上传内容并返回规范化所需的字幕段。"""
    if not data or len(data) > 4 * 1024 * 1024:
        raise ValueError("字幕为空或超过 4MB")
    text = decode_subtitle_bytes(data)
    cues = parse_srt_text(text)
    if len(cues) < 5:
        raise ValueError("有效字幕段少于 5 条，无法用于自动校轴")
    return cues


def save_subtitle_candidate(name, filename, data=None, cues=None):
    """校验、规范化并持久保存供后续校轴使用的中文字幕。"""
    cues = cues if cues is not None else parse_subtitle_candidate_data(data)
    path = subtitle_candidate_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_srt(path, cues)
        path.with_suffix(".name").write_text(filename or "中文字幕.srt", "utf-8")
    except OSError as exc:
        raise RuntimeError(f"保存字幕失败: {exc}") from exc
    return len(cues)


SUBTITLE_MATCH_MS = 500        # 一句字幕落在参照某一句前后半秒以内，才算“对上了”


def _sample_starts(cues, limit=180):
    starts = [int(cue["start"]) for cue in cues]
    if len(starts) <= limit:
        return starts
    step = max(1, len(starts) // limit)
    return starts[::step][:limit]


def _nearest_distance(sorted_values, value):
    pos = bisect_left(sorted_values, value)
    candidates = []
    if pos < len(sorted_values):
        candidates.append(abs(sorted_values[pos] - value))
    if pos:
        candidates.append(abs(sorted_values[pos - 1] - value))
    return min(candidates) if candidates else 10**12


def _alignment_score(anchor_starts, candidate_starts, scale, offset, tolerance=3500):
    if not anchor_starts or not candidate_starts:
        return 0.0
    credit = 0.0
    for start in candidate_starts:
        distance = _nearest_distance(anchor_starts, start * scale + offset)
        if distance <= tolerance:
            credit += 1.0 - 0.55 * (distance / tolerance)
    return credit / len(candidate_starts)


def estimate_subtitle_transform(anchor_cues, candidate_cues):
    """按“每句什么时候出现”把一份字幕对到参照上：算出整体偏移，以及要不要按帧率拉伸。

    只比时间，不比文字。参照可以比字幕密得多（比如带音效说明的原盘字幕），所以判断标准是
    “有多少句落在参照的某一句附近半秒以内”，而且要明显高于随便错开几秒时的水平——句子密的时候，
    随便怎么放都会离某一句不远，宽松的标准分不出对错。
    返回 scale / offset_ms / confidence；把握不够返回 None。
    """
    if len(anchor_cues) < 5 or len(candidate_cues) < 5:
        return None
    anchor_all = sorted(cue["start"] for cue in anchor_cues)
    candidate_all = sorted(cue["start"] for cue in candidate_cues)
    candidate_sample = _sample_starts(candidate_cues)
    scales = (1.0, 1000 / 1001, 1001 / 1000, 24 / 25, 25 / 24)

    def refine(scale, offset):
        # 先宽后窄修三轮：先把大致位置对上，再排除配到相邻句子上的错配
        for window in (1500, 700, 350):
            residuals = []
            for start in candidate_all:
                transformed = start * scale + offset
                pos = bisect_left(anchor_all, transformed)
                nearby = [anchor_all[k] for k in (pos - 1, pos) if 0 <= k < len(anchor_all)]
                nearest = min(nearby, key=lambda value: abs(value - transformed))
                if abs(nearest - transformed) <= window:
                    residuals.append(nearest - transformed)
            if len(residuals) >= max(3, len(candidate_all) // 4):
                offset += float(statistics.median(residuals))
        return offset

    best = None
    for scale in scales:
        offsets = {}
        for candidate_start in candidate_sample:
            scaled = candidate_start * scale
            for anchor_start in anchor_all:
                delta = anchor_start - scaled
                if abs(delta) > 45 * 60 * 1000:
                    continue
                bucket = int(round(delta / 500.0)) * 500
                offsets[bucket] = offsets.get(bucket, 0) + 1
        for offset in sorted(offsets, key=offsets.get, reverse=True)[:12]:
            refined = refine(scale, float(offset))
            score = _alignment_score(anchor_all, candidate_all, scale, refined, tolerance=SUBTITLE_MATCH_MS)
            # 不拉伸（速率 1.0）最常见：别的速率要明显更吻合才采用
            margin = 0.03 if (best is not None and best["scale"] == 1.0 and scale != 1.0) else 0.0
            if best is None or score > best["confidence"] + margin:
                best = {"scale": scale, "offset_ms": refined, "confidence": score}
    if not best:
        return None

    def tight(starts, shift=0.0):
        return _alignment_score(anchor_all, starts, best["scale"], best["offset_ms"] + shift,
                                tolerance=SUBTITLE_MATCH_MS)
    # 和“错开几秒”比：对的位置应该明显更吻合。再分前、中、后三段各看一遍，
    # 免得片头对上了、后半部分因为剪辑不同其实对不上。
    baseline = max(tight(candidate_all, shift) for shift in (-27000, -13000, -7000, 7000, 13000, 27000))
    sections = [candidate_all[k * len(candidate_all) // 3:(k + 1) * len(candidate_all) // 3] for k in range(3)]
    if (best["confidence"] < 0.45 or best["confidence"] - baseline < 0.18
            or any(tight(section) < 0.35 for section in sections if section)):
        return None
    return best


def transform_cues(cues, transform):
    scale = transform["scale"]
    offset = transform["offset_ms"]
    return [
        {
            "start": max(0, cue["start"] * scale + offset),
            "end": max(1, cue["end"] * scale + offset),
            "text": cue["text"],
            "origin": "human",
        }
        for cue in cues
        if cue["end"] * scale + offset > 0
    ]


def uncovered_anchor_cues(anchor_cues, aligned_cues):
    """找出成品中文字幕未覆盖的盘内日文对白，供 AI 只补这些缺口。"""
    starts = [cue["start"] for cue in aligned_cues]
    missing = []
    for anchor in anchor_cues:
        pos = bisect_left(starts, anchor["start"])
        nearby = aligned_cues[max(0, pos - 2):pos + 3]
        covered = any(
            min(anchor["end"], cue["end"]) - max(anchor["start"], cue["start"]) >= -900
            for cue in nearby
        )
        if not covered:
            missing.append(anchor)
    return missing


def subtitle_ai_config():
    """读取 OpenAI-compatible 字幕补译配置；没有配置时绝不发出网络请求。"""
    try:
        saved = json.loads((WORK / ".subtitle_ai.json").read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    endpoint = os.environ.get("SUBTITLE_AI_ENDPOINT", "").strip()
    api_key = os.environ.get("SUBTITLE_AI_API_KEY", "").strip()
    model = os.environ.get("SUBTITLE_AI_MODEL", "").strip()
    endpoint = str(saved.get("endpoint") or endpoint).strip()
    api_key = str(saved.get("api_key") or api_key).strip()
    model = str(saved.get("model") or model).strip()
    if not endpoint or not api_key or not model:
        return None
    return {"endpoint": endpoint, "api_key": api_key, "model": model}


def save_subtitle_ai_config(endpoint, model, api_key):
    parsed = urlparse.urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("接口地址必须是完整的 http/https URL")
    if not model.strip():
        raise ValueError("必须填写模型名称")
    previous = subtitle_ai_config() or {}
    key = api_key.strip() or previous.get("api_key", "")
    if not key:
        raise ValueError("必须填写 API Key")
    data = {"endpoint": endpoint.strip(), "model": model.strip(), "api_key": key}
    path = WORK / ".subtitle_ai.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


def ai_translate_missing(cues, config):
    """批量把 OCR 日文缺段翻成自然简中；返回 cue 副本，失败直接抛出。"""
    translated = []
    for batch_start in range(0, len(cues), 35):
        batch = cues[batch_start:batch_start + 35]
        lines = [{"id": idx, "text": cue["text"]} for idx, cue in enumerate(batch)]
        prompt = (
            "你是影视字幕译者。把下面 OCR 得到的日文对白翻译为自然、简洁的简体中文。"
            "修正常见 OCR 小错误；不要解释，不要添加原文中没有的信息。"
            "严格输出 JSON 数组，每项仅包含 id 和 text，id 必须与输入一致。\n"
            + json.dumps(lines, ensure_ascii=False)
        )
        payload = json.dumps({
            "model": config["model"],
            "messages": [
                {"role": "system", "content": "你只输出可解析的 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            config["endpoint"],
            data=payload,
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"].strip()
            match = re.search(r"\[[\s\S]*\]", content)
            items = json.loads(match.group(0) if match else content)
        except (OSError, ValueError, KeyError, IndexError, urlerror.URLError) as exc:
            raise RuntimeError(f"AI 补译请求失败: {exc}") from exc
        mapped = {
            int(item["id"]): str(item["text"]).strip()
            for item in items
            if isinstance(item, dict) and str(item.get("id", "")).isdigit()
        }
        for idx, cue in enumerate(batch):
            text = mapped.get(idx)
            if text:
                translated.append({**cue, "text": text, "origin": "ai"})
    return translated


def align_chinese_subtitle(job, anchor_cues, candidate_cues, output):
    """按日文 OCR 时间分布校准人工中文字幕，并可选补译缺段。"""
    transform = estimate_subtitle_transform(anchor_cues, candidate_cues)
    if not transform:
        add_warning(job, "成品中文字幕与盘内日文时间分布匹配度过低，未自动改写时间轴")
        return None
    aligned = transform_cues(candidate_cues, transform)
    missing = uncovered_anchor_cues(anchor_cues, aligned)
    log(job, "中文字幕校轴: "
        f"偏移 {transform['offset_ms']/1000:+.3f} 秒，"
        f"速率 {transform['scale']:.6f}，匹配度 {transform['confidence']:.0%}")
    additions = []
    if missing:
        config = subtitle_ai_config()
        if config:
            try:
                additions = ai_translate_missing(missing, config)
                log(job, f"AI补译: 已补 {len(additions)}/{len(missing)} 个缺失字幕段")
            except RuntimeError as exc:
                add_warning(job, str(exc))
        else:
            add_warning(
                job,
                f"成品中文字幕已按原盘校轴，但仍有 {len(missing)} 个盘内日文段落未覆盖；"
                "未配置字幕 AI，暂未机翻这些缺口",
            )
    write_srt(output, aligned + additions)
    stats = _inspect_srt(output)
    if not stats:
        return None
    log(job, f"中文字幕校轴完成: {stats['cue_count']} 个字幕段，人工译文优先")
    return output


def _run_ffsubsync(arguments):
    """延迟载入 ffsubsync，避免服务启动时因可选依赖异常导致整个面板不可用。"""
    try:
        from ffsubsync.ffsubsync import make_parser, run
    except ImportError as exc:
        raise RuntimeError("镜像中缺少 ffsubsync，请使用新版 Dockerfile 升级服务") from exc
    parser = make_parser()
    return run(parser.parse_args(arguments))


def align_chinese_subtitle_by_audio(job, video, candidate_cues, output):
    """没有 OCR 锚点时，用成品音频的对白活动安全校准外部中文字幕。

    只写入施工区的临时 SRT，不修改视频。允许最多 20 分钟的前贴片偏移，并让
    ffsubsync 在低质量结果时保持失败；调用方必须再让用户确认检测到的偏移量。
    """
    video = Path(video)
    output = Path(output)
    input_srt = output.with_name("candidate.input.zho.srt")
    write_srt(input_srt, candidate_cues)
    arguments = [
        str(video), "-i", str(input_srt), "-o", str(output),
        "--max-offset-seconds", "1200",
        "--quality-max-offset-seconds", "1200",
        "--skip-sync-on-low-quality",
        "--multi-segment-sync",
        "--skip-intro-outro",
        "--parallel-workers", "2",
        "--output-encoding", "utf-8",
    ]
    try:
        result = _run_ffsubsync(arguments)
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"音频自动校轴运行失败: {exc}") from exc
    finally:
        input_srt.unlink(missing_ok=True)

    offset = result.get("offset_seconds")
    scale = result.get("framerate_scale_factor")
    if (not result.get("sync_was_successful") or offset is None or scale is None
            or not _inspect_srt(output)):
        output.unlink(missing_ok=True)
        raise ValueError(
            "成品没有日文 OCR 时间轴，音频自动校轴的可信度也不足。"
            "未改动现有视频和字幕；请换一份更匹配的字幕重试"
        )
    if abs(float(offset)) > 1200 or abs(float(scale) - 1.0) > 0.05:
        output.unlink(missing_ok=True)
        raise ValueError("音频校轴检测结果超出安全范围，未改动现有字幕")

    aligned_cues = parse_srt_text(output.read_text("utf-8-sig"))
    media = summarize_probe(ffprobe(video))
    duration_ms = float(media.get("duration") or 0) * 1000 if media else 0
    if duration_ms:
        inside = sum(
            1 for cue in aligned_cues
            if cue["end"] > 0 and cue["start"] < duration_ms + 120_000
        )
        if inside < max(5, int(len(aligned_cues) * 0.9)):
            output.unlink(missing_ok=True)
            raise ValueError(
                "音频校轴后的大部分字幕落在影片时长之外，疑似选错发行版，未应用"
            )
    log(job, "中文字幕音频校轴: "
        f"偏移 {float(offset):+.3f} 秒，速率 {float(scale):.6f}，"
        f"{len(aligned_cues)} 个字幕段")
    return output, {
        "method": "audio",
        "offset_seconds": float(offset),
        "scale": float(scale),
        "cue_count": len(aligned_cues),
    }


def build_hybrid_chinese_subtitle(job, item_name, ocr_srts, jobdir):
    """人工成品中文优先校轴；只对未覆盖的盘内日文 cue 做可选 AI 补译。"""
    candidate = load_subtitle_candidate(item_name)
    if not candidate:
        return None
    if any(lang == "zho" for _path, lang in ocr_srts):
        log(job, "中文字幕校轴: 盘内已有中文 OCR 字幕，优先使用原盘中文字幕")
        return None
    japanese = next((path for path, lang in ocr_srts if lang == "jpn"), None)
    if not japanese:
        add_warning(job, "已提供成品中文字幕，但盘内日文 OCR 未成功，无法可靠自动校轴")
        return None
    anchor_cues = parse_srt_text(Path(japanese).read_text("utf-8-sig"))
    _candidate_path, candidate_cues = candidate
    return align_chinese_subtitle(
        job, anchor_cues, candidate_cues, jobdir / "hybrid.zho.srt"
    )


def tracked_movie_output(name):
    """返回状态文件中记录且真实位于 Movies 的单个成品。"""
    with LOCK:
        raw = PROCESSED.get(name, {}).get("output", "")
    if not raw:
        return None
    try:
        output = Path(raw).resolve(strict=True)
        movies_root = MOVIES.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not output.is_file() or not output.is_relative_to(movies_root):
        return None
    return output


def apply_candidate_as_sidecar(
    name, filename, candidate_cues, confirm_audio=False,
    manual_offset_seconds=None, manual_first_start_ms=None, auto=False, target=None,
):
    """给已入库影片的一份外挂字幕校准时间；绝不改写视频文件。

    target 是校准结果写到哪个文件：校准影片旁边已有的字幕时就写回它自己（可以反复校准）；
    不给时写到“片名.zh-CN.srt”（来源是 Jellyfin 资料目录里的字幕，面板改不了那里的文件）。
    """
    output = tracked_movie_output(name)
    if not output:
        raise ValueError("当前成品不存在，无法即时应用字幕")
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    jobdir = WORK / "sidecar_jobs" / digest
    shutil.rmtree(jobdir, ignore_errors=True)
    jobdir.mkdir(parents=True, exist_ok=True)
    job = {"name": name, "log": [], "warnings": [], "percent": 0}
    try:
        # 打开校准窗口时只显示字幕原来的时间，不自动算。用户点了“自动校准”（auto）才去算，
        # 算出来也只是填进窗口供核对，点“应用”才写文件。确认时用刚才那次的结果，不重算。
        cached = load_subtitle_audio_preview(name, filename, output, candidate_cues) if confirm_audio else None
        with LOCK:
            has_embedded = bool(((OUTPUT_INFO.get(Path(output)) or {}).get("probe") or {}).get("subs"))
        can_auto = load_subtitle_timing(output) is not None or has_embedded
        anchor_cues = timing_anchor_cues(name, output) if (auto and not confirm_audio and not cached) else None
        auto_result, transform = (align_by_timing(job, anchor_cues, candidate_cues, jobdir / "aligned.zh-CN.srt")
                           if anchor_cues else (None, None))
        no_auto_reason = ("与影片自带字幕不匹配（可能是不同剪辑版本）。请手动填写首句时间，或更换字幕"
                          if anchor_cues else "无可参照的字幕时间点，请手动填写首句时间")
        if cached:
            aligned, sync_info = cached
        elif auto_result:
            aligned = auto_result
            sync_info = {
                "method": "timing",
                "offset_seconds": transform["offset_ms"] / 1000,
                "scale": transform["scale"],
                "cue_count": len(candidate_cues),
                "automatic_available": True,
                "automatic_note": "已按影片自带字幕自动校准。确认后应用，可手动调整。",
            }
        else:
            # 以前这里靠分析声音来估偏移，配乐和音效多的片会估得很离谱，已停用
            aligned = jobdir / "manual-preview.zh-CN.srt"
            write_srt(aligned, candidate_cues)
            sync_info = {
                "method": "manual",
                "offset_seconds": 0.0,
                "scale": 1.0,
                "cue_count": len(candidate_cues),
                "automatic_available": False,
                "automatic_error": no_auto_reason if auto else "",
            }
        if not confirm_audio:
            save_subtitle_audio_preview(
                name, filename, output, aligned, candidate_cues, sync_info
            )
            return {
                "confirmation_required": True,
                "alignment_method": sync_info["method"],
                "offset_seconds": sync_info["offset_seconds"],
                "scale": sync_info["scale"],
                "cue_count": sync_info["cue_count"],
                "automatic_available": sync_info.get(
                    "automatic_available", True
                ),
                "automatic_error": sync_info.get("automatic_error", ""),
                "automatic_note": sync_info.get("automatic_note", ""),
                "can_auto": can_auto,
                "preview_cues": subtitle_preview_cues(aligned),
                "source_preview_cues": [
                    {
                        "start_ms": int(cue["start"]),
                        "end_ms": int(cue["end"]),
                        "text": str(cue["text"])[:120],
                    }
                    for cue in candidate_cues[:4]
                ],
                "source_first_start_ms": int(candidate_cues[0]["start"]),
                "automatic_first_start_ms": (
                    subtitle_preview_cues(aligned, limit=1)[0]["start_ms"]
                ),
                "automatic_suspicious": (
                    abs(float(sync_info.get("scale") or 1) - 1) > 0.01
                    or abs(float(sync_info.get("offset_seconds") or 0)) > 900
                ),
            }
        media = summarize_probe(ffprobe(output))
        duration_ms = (
            float(media.get("duration") or 0) * 1000 if media else 0
        )
        first_start = normalize_manual_first_start_ms(
            manual_first_start_ms, duration_ms
        )
        if first_start is not None:
            source_first = float(candidate_cues[0]["start"])
            # 人工首句校准从原始字幕重建：只整体平移，绝不采用可疑自动速率，
            # 因而不会因负偏移截掉字幕，也不会引入后半程额外漂移。
            aligned = jobdir / "first-cue-aligned.zh-CN.srt"
            keep_scale = float(sync_info.get("scale") or 1.0) if sync_info.get("method") == "timing" else 1.0
            manual_offset_ms = first_start - source_first * keep_scale
            write_srt(aligned, transform_cues(candidate_cues, {
                "scale": keep_scale,
                "offset_ms": manual_offset_ms,
            }))
            sync_info = {
                **sync_info,
                "method": "manual_first_cue",
                "automatic_offset_seconds": float(
                    sync_info.get("offset_seconds") or 0
                ),
                "automatic_scale": float(sync_info.get("scale") or 1),
                "offset_seconds": manual_offset_ms / 1000,
                "scale": keep_scale,
                "source_first_start_ms": int(source_first),
                "first_start_ms": first_start,
            }
        manual_offset = normalize_manual_subtitle_offset(
            manual_offset_seconds
        )
        if first_start is None and manual_offset is not None:
            automatic_offset = float(sync_info.get("offset_seconds") or 0)
            aligned = shift_aligned_subtitle(
                aligned,
                manual_offset - automatic_offset,
                jobdir / "manual-aligned.zh-CN.srt",
            )
            sync_info = {
                **sync_info,
                "method": "manual",
                "automatic_offset_seconds": automatic_offset,
                "offset_seconds": manual_offset,
                "manual_delta_seconds": manual_offset - automatic_offset,
            }
        if not aligned:
            raise ValueError(
                "所选中文字幕与该影片时间分布不匹配，未替换现有字幕"
            )
        # 校轴成功后才覆盖已选字幕记录和 Movies 外置字幕，避免误选破坏旧版本。
        save_subtitle_candidate(name, filename, cues=candidate_cues)
        in_place = target is not None
        target = Path(target) if in_place else subtitle_sidecar_path(output)
        staging = target.with_name(f".{target.name}.tmp")
        try:
            shutil.copy2(aligned, staging)
            os.replace(staging, target)
            hand_over(target)
        finally:
            staging.unlink(missing_ok=True)
        with LOCK:
            if name in PROCESSED and not in_place:
                PROCESSED[name]["subtitle_sidecar"] = str(target)
                save_state()
        clear_subtitle_audio_preview(name)
        stats = _inspect_srt(target) or {}
        return {
            "sidecar": target.name,
            "cue_count": stats.get("cue_count", len(candidate_cues)),
            "warnings": list(job.get("warnings", [])),
            "alignment_method": sync_info["method"],
            "offset_seconds": sync_info.get("offset_seconds"),
            "scale": sync_info.get("scale"),
        }
    finally:
        shutil.rmtree(jobdir, ignore_errors=True)


def store_candidate_for_item(
    name, filename, data, confirm_audio=False, manual_offset_seconds=None,
    manual_first_start_ms=None, auto=False, target=None,
):
    """待处理任务保存候选；已完成任务则即时校轴成外置字幕。"""
    candidate_cues = parse_subtitle_candidate_data(data)
    with LOCK:
        status = (ITEMS.get(name) or {}).get("status")
    if status == "done":
        applied = apply_candidate_as_sidecar(
            name, filename, candidate_cues, confirm_audio=confirm_audio,
            manual_offset_seconds=manual_offset_seconds,
            manual_first_start_ms=manual_first_start_ms, auto=auto, target=target,
        )
        if applied.get("confirmation_required"):
            return {
                "cue_count": len(candidate_cues),
                "applied": False,
                **applied,
            }
        return {
            "cue_count": len(candidate_cues),
            "applied": True,
            **applied,
        }
    cue_count = save_subtitle_candidate(name, filename, cues=candidate_cues)
    return {"cue_count": cue_count, "applied": False}


def remove_candidate_and_sidecar(name):
    """移除用户选择的候选字幕及处理台生成的外置字幕，不碰视频。"""
    candidate = subtitle_candidate_path(name)
    removed = []
    for path in (candidate, candidate.with_suffix(".name")):
        try:
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        except OSError as exc:
            raise RuntimeError(f"删除已选字幕失败: {exc}") from exc
    clear_subtitle_audio_preview(name)

    with LOCK:
        processed = PROCESSED.get(name, {})
        raw_output = processed.get("output", "")
        raw_sidecar = processed.get("subtitle_sidecar", "")
    sidecars = []
    if raw_sidecar:
        sidecars.append(Path(raw_sidecar))
    if raw_output:
        sidecars.append(subtitle_sidecar_path(Path(raw_output)))
    try:
        movies_root = MOVIES.resolve(strict=True)
    except (OSError, RuntimeError):
        movies_root = None
    for path in dict.fromkeys(sidecars):
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if (movies_root and resolved.is_file()
                and resolved.is_relative_to(movies_root)
                and resolved.name.endswith(".zh-CN.srt")):
            resolved.unlink()
            removed.append(resolved.name)
    with LOCK:
        if name in PROCESSED:
            PROCESSED[name].pop("subtitle_sidecar", None)
            save_state()
    return removed


def _inspect_srt(path: Path):
    """轻量校验 SRT 结构，允许只有一两句的“强制字幕”。

    旧版用“至少 20 个文字”判断是否损坏，会把片中招牌、外语对白等稀疏但合法
    的字幕误删。这里改成检查时间码和实际正文，只要至少有一个有效 cue 就保留。
    """
    try:
        txt = path.read_text("utf-8-sig", "ignore")
    except Exception:
        return None
    cue_count = len(SRT_TIMECODE_RE.findall(txt))
    text_lines = []
    for raw in txt.splitlines():
        line = raw.strip()
        if not line or line.isdigit() or SRT_TIMECODE_RE.search(line):
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            text_lines.append(line)
    body = "\n".join(text_lines)
    meaningful = sum(1 for char in body if char.isalnum())
    if cue_count < 1 or meaningful < 1:
        return None
    return {"text": body, "cue_count": cue_count, "meaningful": meaningful}


def _classify_srt(path: Path, fallback_lang):
    """校验 OCR SRT 并按内容定语言；稀疏字幕优先参考盘内语言标签。"""
    inspected = _inspect_srt(path)
    if not inspected:
        return None
    txt = inspected["text"]
    cjk = sum(1 for ch in txt if "\u4e00" <= ch <= "\u9fff")
    kana = sum(1 for ch in txt if "\u3040" <= ch <= "\u30ff")
    hangul = sum(1 for ch in txt if "\uac00" <= ch <= "\ud7a3")
    latin = sum(1 for ch in txt if ch.isascii() and ch.isalpha())
    fallback = LANG_ALIASES.get((fallback_lang or "").lower(), "und")
    if kana:
        return "jpn"
    if hangul:
        return "kor"
    if cjk:
        return fallback if fallback in ("zho", "jpn", "kor") else "zho"
    if latin:
        return fallback
    return fallback


def _run_pgsrip_progress(job, target: Path, base_pct, span_pct):
    """跑 pgsrip 并把它 tqdm 的百分比实时映射到 [base_pct, base_pct+span_pct]。
    pgsrip 进度用 \\r 刷同一行(不是换行),故逐字节读、遇 \\r 或 \\n 才解析,才抓得到百分比。
    返回 (退出码, 诊断输出尾部)，失败时不再把真正原因吞掉。"""
    with subprocess.Popen(
        ["pgsrip", "--debug", str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as p:
        buf = bytearray()
        output_lines = []
        while True:
            ch = p.stdout.read(1)
            if not ch:
                break
            if ch in (b"\r", b"\n"):
                line = buf.decode("utf-8", "replace").strip()
                if line:
                    output_lines.append(line)
                    if len(output_lines) > 80:
                        output_lines = output_lines[-80:]
                m = re.search(r"(\d+)%", line)
                if m:
                    within = min(100, int(m.group(1)))
                    job["percent"] = min(99, base_pct + span_pct * within / 100.0)
                buf.clear()
            else:
                buf += ch
        if buf:
            output_lines.append(buf.decode("utf-8", "replace").strip())
        p.wait()
        tail = "\n".join(line for line in output_lines if line)[-2400:]
        return p.returncode, tail


PGSRIP_CODE_FALLBACK = {
    "zho": "zh", "chi": "zh", "cmn": "zh", "jpn": "ja", "kor": "ko", "eng": "en",
    "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "spa": "es", "por": "pt",
    "ita": "it", "rus": "ru", "nld": "nl", "dut": "nl", "tha": "th", "vie": "vi",
}


def pgsrip_language_code(tag):
    """返回 pgsrip 能原样读回的文件名语言码。

    pgsrip 从 `名字.语言.sup` 里解析语言，再用解析结果重新拼出路径去读文件；
    它拼的是两字母码。文件若叫 `x.eng.sup`，它会去打开 `x.en.sup`，找不到文件，
    结果就是“未产出 SRT”。所以 SUP 必须直接用它会拼出来的那个写法命名。
    """
    tag = (tag or "und").lower()
    try:
        import cleanit  # noqa: F401  注册 pgsrip 使用的语言转换器
        from babelfish import Language
        return str(Language.fromcleanit(tag))
    except Exception:
        return PGSRIP_CODE_FALLBACK.get(tag, tag if len(tag) == 2 else "und")


TESSERACT_CODE_FALLBACK = {
    "chi": "zho", "cmn": "zho", "zh": "zho", "fre": "fra", "ger": "deu", "dut": "nld",
    "en": "eng", "ja": "jpn", "ko": "kor",
}


def tesseract_language_code(tag):
    """pgsrip 会向 Tesseract 请求的模型名（ISO 639-3）。"""
    tag = (tag or "und").lower()
    try:
        import cleanit  # noqa: F401
        from babelfish import Language
        return Language.fromcleanit(tag).alpha3
    except Exception:
        return TESSERACT_CODE_FALLBACK.get(tag, tag)


def installed_tesseract_languages():
    """镜像里已安装的识别模型；查询失败返回 None（此时不做预先跳过）。"""
    try:
        listed = subprocess.run(["tesseract", "--list-langs"],
                                capture_output=True, timeout=30)
    except Exception:
        return None
    if listed.returncode != 0:
        return None
    text = listed.stdout.decode("utf-8", "replace") + listed.stderr.decode("utf-8", "replace")
    names = {line.strip() for line in text.splitlines()[1:] if line.strip()}
    return names or None


def ocr_failure_reason(diagnostic):
    """从 pgsrip 的调试输出里取出真正的异常那一行。

    异常信息在回溯的最后；日志每行只留前 400 字，直接贴尾部 600 字会正好把
    最后这行截掉（日语轨失败时就只看到半截回溯）。
    """
    lines = [line.strip() for line in str(diagnostic or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if re.match(r"^[\w.]*(Error|Exception)\b", line) or "Error while trying to rip" in line:
            return line[:300]
    return lines[-1][:300] if lines else ""


PGS_CHUNK_SUBTITLES = 200   # 分段识别时每段包含的字幕条数


def split_sup(sup: Path, per_chunk=PGS_CHUNK_SUBTITLES):
    """把一条 PGS（.sup）按字幕条数切成若干小文件，返回它们的路径。

    pgsrip 会把整条轨的所有字幕拼成一张大图交给 Tesseract。日语这类大字符集模型
    处理这样一张图时内存占用很高，NAS 上实测进程被系统直接杀掉（退出码 -9）。
    切小以后每次只识别一段，内存有上限。

    只在“Epoch Start”处下刀：那是一条新字幕的起点，之前的画面对象不再被引用，
    所以每一段都能独立解码。PGS 的时间戳是绝对时间，各段识别结果直接合并即可。
    切不出两段以上（文件损坏或本来就很短）时返回空列表。
    """
    data = sup.read_bytes()
    starts = []          # 每个 Epoch Start 所在显示集的起始偏移
    pos = 0
    while pos + 13 <= len(data):
        if data[pos:pos + 2] != b"PG":
            return []    # 结构不对，不冒险切
        segment_type = data[pos + 10]
        size = int.from_bytes(data[pos + 11:pos + 13], "big")
        payload = pos + 13
        if segment_type == 0x16 and size >= 8 and data[payload + 7] == 0x80:
            starts.append(pos)
        pos = payload + size
    if pos != len(data) or len(starts) <= per_chunk:
        return []
    cuts = starts[::per_chunk]
    cuts[0] = 0
    cuts.append(len(data))
    stem, language = sup.name[:-len(".sup")].rsplit(".", 1)
    parts = []
    for index in range(len(cuts) - 1):
        part = sup.with_name(f"{stem}_part{index:03d}.{language}.sup")
        part.write_bytes(data[cuts[index]:cuts[index + 1]])
        parts.append(part)
    return parts


def ocr_sup_in_chunks(job, sup: Path, base_pct, span_pct, why=""):
    """整条识别失败后的退路：分段识别再合并。返回合并后的 SRT 路径，失败返回 None。"""
    parts = split_sup(sup)
    if not parts:
        return None
    log(job, "OCR字幕: 整条识别失败" + (f"（{why}）" if why else "")
             + f"，改为分 {len(parts)} 段识别…")
    cues = []
    failed_parts = 0
    last_diagnostic = ""
    try:
        for index, part in enumerate(parts):
            step = span_pct / len(parts)
            _rc, diagnostic = _run_pgsrip_progress(job, part, base_pct + index * step, step)
            srt = part.with_suffix(".srt")
            if srt.is_file():
                cues.extend(parse_srt_text(srt.read_text("utf-8-sig", "ignore")))
                srt.unlink(missing_ok=True)
            else:
                failed_parts += 1
                last_diagnostic = diagnostic
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
            part.with_suffix(".srt").unlink(missing_ok=True)
    if failed_parts:
        print(f"OCR字幕诊断(分段识别 {sup.name}):\n{last_diagnostic}", flush=True)
        log(job, f"OCR字幕: 分段识别时 {failed_parts}/{len(parts)} 段失败: "
                 f"{ocr_failure_reason(last_diagnostic)}")
    # 丢段意味着整段时间没有字幕，宁可判失败也不交付残缺的字幕轨
    if failed_parts or not cues:
        return None
    merged = sup.with_suffix(".srt")
    write_srt(merged, cues)
    return merged


def pgs_extract_timeout(source):
    """按文件大小给一次性抽取留足时间：按最慢约 15MB/s 估算，至少 30 分钟。"""
    try:
        size = Path(source).stat().st_size
    except OSError:
        size = 0
    return max(1800, int(size / (15 * 1024**2)))


def ocr_subs(job, payload: Path, jobdir: Path):
    """把源里所有 PGS 图形字幕逐条 OCR 成文本 SRT(不判断语言,全转)。
    逐条处理(而非一次性整片跑)是为了能显示"第几条/共几条 + 条内百分比"的实时进度,
    否则 pgsrip 闷头跑十几分钟看不出动静,分不清是在干活还是卡死了。
    先由本程序用真实轨道 ID 抽成 SUP，再把 SUP 交给 pgsrip。pgsrip 官方支持
    SUP 输入；这样避免它对临时 MKV 再次调用 mkvextract，也能保留完整错误信息。
    返回 [(srt路径, 语言)] 列表。全程 best-effort: 任何失败只记日志、跳过,绝不拖垮整个任务。"""
    # 注意：2026-11-17 起主流程不再调用本函数（电视能直接显示图形字幕）。
    # 代码保留，供以后需要把图形字幕转成文本时使用。
    extract_source = payload
    if payload.suffix.lower() != ".mkv":
        extract_source = jobdir / "pgs_source.mkv"
        log(job, f"OCR字幕: {payload.suffix.upper() or '非MKV'} 源先无损规范化为临时 MKV")
        try:
            normalized = subprocess.run(
                ["mkvmerge", "-o", str(extract_source), str(payload)],
                capture_output=True, timeout=1800,
            )
            normalized_detail = (
                normalized.stdout.decode("utf-8", "replace") + "\n"
                + normalized.stderr.decode("utf-8", "replace")
            ).strip()
            if (normalized.returncode >= 2 or not extract_source.is_file()
                    or extract_source.stat().st_size == 0):
                detail = normalized_detail[-700:]
                add_warning(
                    job,
                    "图形字幕规范化失败，成品将不含这些盘内字幕"
                    + (f": {detail}" if detail else ""),
                )
                return []
        except Exception as exc:
            add_warning(job, f"图形字幕规范化异常，成品将不含这些盘内字幕: {exc}")
            return []
    pgs = _pgs_tracks(extract_source)
    if not pgs:
        add_warning(job, "ffprobe 检测到 PGS，但 mkvmerge 无法读取字幕轨，成品将不含这些图形字幕")
        return []
    set_phase(job, "OCR字幕", 0)
    n = len(pgs)
    results = []   # (Path, lang)
    failures = []

    # 一次 mkvextract 抽出全部 PGS 轨。逐条抽取时每条都要把几十 GB 的正片从头读
    # 一遍，轨道多的 UHD 盘会读上十几遍，并且很容易撞上单条超时而整批丢字幕。
    tracks = []
    for idx, t in enumerate(pgs):
        tag = (t["lang"] or "und").lower()
        # 没标语言(und等)的强制按中文识别(覆盖'源没标语言的中文字幕');有标签的用其标签选模型
        forced = "zho" if tag in ("und", "mis", "zxx", "") else tag
        tracks.append((idx, t, forced,
                       jobdir / f"pgs_{t['id']}.{pgsrip_language_code(forced)}.sup"))
    extract_detail = ""
    extract_code = 0
    failed_languages = []
    available = installed_tesseract_languages()
    skipped = set()
    if available is not None:
        for idx, _t, forced, _sup in tracks:
            model = tesseract_language_code(forced)
            if model not in available:
                skipped.add(idx)
                failed_languages.append(forced)
                reason = f"第{idx+1}条（{forced}）镜像未安装 {model} 识别模型，已跳过"
                failures.append(reason)
                log(job, "OCR字幕: " + reason)
    wanted = [entry for entry in tracks if entry[0] not in skipped]
    try:
        if not wanted:
            raise LookupError("没有可识别语言的 PGS 轨")
        log(job, f"OCR字幕: 一次读取正片，抽出 {len(wanted)} 条 PGS…")
        extract = subprocess.run(
            ["mkvextract", str(extract_source), "tracks"]
            + [f"{t['id']}:{sup}" for _idx, t, _forced, sup in wanted],
            capture_output=True, timeout=pgs_extract_timeout(extract_source))
        extract_code = extract.returncode
        extract_detail = (
            extract.stdout.decode("utf-8", "replace") + "\n"
            + extract.stderr.decode("utf-8", "replace")
        ).strip()
        # mkvextract: 0=成功，1=有警告但完成，2=错误。只要 SUP 非空即可继续。
        if extract_code == 1 and extract_detail:
            log(job, f"OCR字幕抽取警告: {extract_detail[-500:]}")
    except FileNotFoundError:
        log(job, "OCR跳过: 容器内未安装 mkvextract(需重建镜像)")
        add_warning(job, f"{n}条 PGS 均未成功转成文本（容器内未安装 mkvextract），这些图形字幕不在成品里")
        return []
    except LookupError:
        pass
    except subprocess.TimeoutExpired:
        extract_code = 2
        extract_detail = "mkvextract 读取正片超时"
    except Exception as exc:
        extract_code = 2
        extract_detail = f"mkvextract 异常: {exc}"

    for idx, t, forced, sup in wanted:
        base = idx / n * 100.0
        span = 1.0 / n * 100.0
        try:
            if extract_code >= 2 or not sup.is_file() or sup.stat().st_size == 0:
                detail = extract_detail[-700:]
                reason = f"第{idx+1}条 PGS 抽取失败"
                if detail:
                    reason += f": {detail}"
                failures.append(reason)
                failed_languages.append(forced)
                log(job, "OCR字幕: " + reason)
                continue
            log(job, f"OCR字幕: 第{idx+1}/{n}条 (语言{forced}) 识别中…")
            before = set(jobdir.glob("*.srt"))
            rc, diagnostic = _run_pgsrip_progress(job, sup, base, span)
            new_srts = [x for x in jobdir.glob("*.srt") if x not in before]
            if not new_srts:
                merged = ocr_sup_in_chunks(job, sup, base, span,
                                           why=ocr_failure_reason(diagnostic))
                if merged:
                    new_srts = [merged]
            if not new_srts:
                # 完整诊断只进容器日志（docker logs），任务日志里留真正的异常行。
                print(f"OCR字幕诊断(第{idx+1}条 {forced}):\n{diagnostic}", flush=True)
                reason = f"第{idx+1}条（{forced}）pgsrip 未产出 SRT"
                if rc:
                    reason += f"（退出码 {rc}）"
                cause = ocr_failure_reason(diagnostic)
                if cause:
                    reason += f": {cause}"
                failures.append(reason)
                failed_languages.append(forced)
                log(job, "OCR字幕: " + reason)
            for f in new_srts:
                lang = _classify_srt(f, forced)
                if lang:
                    results.append((f, lang))
                    stats = _inspect_srt(f)
                    log(job, f"OCR字幕: 第{idx+1}/{n}条完成 → {lang}，"
                             f"{stats['cue_count']} 个字幕段")
                else:
                    f.unlink(missing_ok=True)
                    reason = f"第{idx+1}条 SRT 没有有效时间码或正文"
                    failures.append(reason)
                    failed_languages.append(forced)
                    log(job, "OCR字幕: " + reason + "，已丢弃")
        except FileNotFoundError:
            log(job, "OCR跳过: 容器内未安装 pgsrip/tesseract(需重建镜像)")
            failures.append("容器内未安装 pgsrip/tesseract")
            for _idx, _t, _forced, leftover in tracks:
                leftover.unlink(missing_ok=True)
            return results
        except Exception as e:
            reason = f"第{idx+1}条处理异常: {e}"
            failures.append(reason)
            failed_languages.append(forced)
            log(job, "OCR字幕: " + reason)
        finally:
            sup.unlink(missing_ok=True)
        job["percent"] = min(99, (idx + 1) / n * 100.0)

    job["percent"] = 100
    if not results:
        detail = failures[0] if failures else "未识别到有效文字"
        log(job, "OCR未产出可用字幕；盘内时间轴不会被伪造，外部字幕需确认发行版")
        add_warning(job, f"{n}条 PGS 均未成功转成文本（{detail[:160]}），"
                         "这些图形字幕不在成品里；有前贴片/分支版时外部字幕可能整体错轴")
    elif len(results) < n:
        missing = "、".join(dict.fromkeys(failed_languages)) or "未知"
        add_warning(job, f"{n}条 PGS 中仅 {len(results)} 条成功转成文本，其余未内嵌"
                         f"（未成功的语言: {missing}）")
    return results


def mkvmerge_mux(job, phase, video_src: Path, aux: Path, out: Path,
                 ocr_srts, default_duration_fps=None, video_only=False):
    """统一的 mkvmerge 封装: 视频来自 video_src,音频/字幕/章节来自 aux,再追加 OCR 出的文本 SRT。
    - default_duration_fps: P7路线的 BL 是无时间戳裸流,需指定帧率;从源直取视频轨时务必留空以免改时轴。
    - video_only: 从源(整个mkv)里只取视频轨时置 True。
    - ocr_srts: [(srt路径, 语言)] 列表。"""
    set_phase(job, phase, 0)
    cmd = ["mkvmerge", "--gui-mode", "-o", str(out)]
    if default_duration_fps:
        cmd += ["--default-duration", f"0:{default_duration_fps}fps"]
    if video_only:
        cmd += ["--no-audio", "--no-subtitles", "--no-chapters",
                "--no-attachments", "--no-track-tags"]
    cmd += [str(video_src), str(aux)]
    for s, lang in ocr_srts:
        is_cn = lang in ("zho", "chi", "zh")
        if Path(s).name.startswith("hybrid."):
            name = "中文(成品校轴)"
        else:
            name = ("中文" if is_cn else lang) + "(OCR)"
        cmd += ["--language", f"0:{lang}", "--track-name", f"0:{name}",
                "--default-track-flag", f"0:{'yes' if is_cn else 'no'}", str(s)]
    def progress(line):
        match = re.search(r'progress\s+(\d+)%', line)
        if match:
            job["percent"] = int(match.group(1))
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            detail = read_process_progress(job, process, progress)
            if process.returncode not in (0, 1):
                raise RuntimeError("mkvmerge 失败: " + detail)
            if process.returncode == 1:
                add_warning(job, "mkvmerge 完成但有警告: " + detail[-600:])
        finally:
            stop_local_process(process)
    job["percent"] = 100


def ffmpeg_mp4_mux(job, phase, source: Path, probe, out: Path, selection, dur,
                   bl_video: Path = None):
    """封装成 MP4（只用于杜比视界：MKV 里实测会丢杜比视界）。

    - 视频永远直通；HEVC 写 hvc1、H.264 写 avc1；-strict unofficial 允许把杜比视界配置写进 MP4。
      bl_video 给出时用 P7→P8 转好、且已由 rebuild_bl_timestamps 重建时间戳的 BL(MKV)。
    - 音频按 build_audio_args：电视能播的直通，解不了的加 EAC3 兼容轨并保留原轨。
    - 字幕按设置内嵌为定时文本或在同次封装时输出外挂文件，PGS 始终外挂。
    返回 (成品音轨数, 内嵌字幕数, [外挂字幕路径])。
    """
    set_phase(job, phase, 0)
    sel = normalize_selection(probe, selection if isinstance(selection, dict) else None)
    if bl_video:
        if Path(bl_video).suffix.lower() in (".hevc", ".h265", ".265"):
            raise ValueError("BL 裸流必须先重建时间戳再封装 MP4")
        inputs = ["-i", str(bl_video), "-i", str(source)]
        vin, ain = "0", "1"
    else:
        inputs = ["-i", str(source)]
        vin = ain = "0"

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1"] + inputs
    cmd += ["-map", f"{vin}:v:0", "-c:v", "copy"]
    video_codec = (probe.get("video") or {}).get("codec")
    if video_codec == "hevc":
        cmd += ["-tag:v", "hvc1"]
    elif video_codec == "h264":
        cmd += ["-tag:v", "avc1"]

    audio_args, audio_count = build_audio_args(probe, sel["audio"], "mp4", ain)
    cmd += audio_args
    keeps_original = bool(kept_original_tracks(probe, "mp4", sel["audio"]))

    text_count = 0
    for index in sel["subs"]:                       # 规则选了“留在影片文件里”的文字字幕
        sub = probe["subs"][index]
        if subtitle_mode(sub.get("codec"), "mp4") != "embed":
            continue
        cmd += ["-map", f"{ain}:s:{index}",
                f"-metadata:s:s:{text_count}", f"language={sub.get('lang') or 'und'}",
                f"-disposition:s:{text_count}", "default" if text_count == 0 else "0"]
        text_count += 1
    if text_count:
        cmd += ["-c:s", "mov_text"]
    sidecar_args, sidecars = subtitle_sidecar_outputs(out, probe, sel["subs"], ain)

    # 杜比视界配置需要 unofficial；MP4 里放 TrueHD 需要更宽松的 experimental（已包含前者）。
    copy_experimental = any(probe["audio"][i]["codec"] in MP4_ORIGINAL_AUDIO
                            and audio_action(probe["audio"][i]["codec"], "mp4", probe["audio"][i].get("profile")) == "keep"
                            for i in sel["audio"])
    strictness = "experimental" if keeps_original or copy_experimental else "unofficial"
    cmd += ["-map_chapters", ain, "-strict", strictness, "-movflags", "+faststart", str(out)]
    cmd += sidecar_args
    run_ffmpeg_progress(job, cmd, dur, phase)
    return audio_count, text_count, sidecars


def verify_dovi_p8(out: Path, src_duration):
    s = summarize_probe(ffprobe(out))
    if not s or not s["video"]:
        raise RuntimeError("成品探测失败")
    if s["video"]["dv_profile"] != 8:
        raise RuntimeError(f"自检失败: 成品 DoVi profile={s['video']['dv_profile']},应为8")
    video = s["video"]
    if (video.get("dv_compatibility_id") != 1 or video.get("dv_rpu_present") != 1
            or video.get("dv_bl_present") != 1 or video.get("dv_el_present") != 0):
        raise RuntimeError("自检失败: 未确认成品为 HDR10 兼容的单层 P8.1（BL+RPU）")
    if src_duration > 0 and abs(s["duration"] - src_duration) > 2.0:
        raise RuntimeError(f"自检失败: 时长偏差{abs(s['duration']-src_duration):.1f}s")


def verify_dv_preserved(out: Path, src_profile, src_duration, source_video=None):
    """保留源 Profile 的杜比视界封装必须通过配置、动态元数据标志与时长校验。"""
    s = summarize_probe(ffprobe(out))
    if not s or not s["video"]:
        raise RuntimeError("成品探测失败")
    if s["video"]["dv_profile"] != src_profile:
        raise RuntimeError(f"自检失败: 成品 DoVi profile={s['video']['dv_profile']},"
                           f"应保留为 {src_profile}(DV 配置丢失,会导致电视只有声音没画面)")
    for key in ("dv_compatibility_id", "dv_rpu_present", "dv_el_present", "dv_bl_present"):
        expected = (source_video or {}).get(key)
        if expected is not None and s["video"].get(key) != expected:
            raise RuntimeError(f"自检失败: 杜比视界配置 {key} 未保留")
    if src_duration > 0 and abs(s["duration"] - src_duration) > 2.0:
        raise RuntimeError(f"自检失败: 时长偏差{abs(s['duration']-src_duration):.1f}s")


def verify_media_preserved(out: Path, source_probe, expected_text_subtitles=None,
                           extra_audio=0, expected_audio=None):
    """成品自检：编码、画面规格、时长、音轨和预期文本字幕不得意外改变。"""
    result = summarize_probe(ffprobe(out))
    if not result or not result["video"]:
        raise RuntimeError("成品自检失败: ffprobe 无法解析视频")
    expected_video = source_probe["video"]["codec"]
    if result["video"]["codec"] != expected_video:
        raise RuntimeError(
            f"成品自检失败: 视频编码从 {expected_video} 变成 {result['video']['codec']}，"
            "违反视频直通规则"
        )
    for key, label in (
        ("width", "宽度"),
        ("height", "高度"),
        ("pix_fmt", "像素格式"),
        ("r_frame_rate", "帧率"),
        ("color_transfer", "动态范围传递函数"),
    ):
        expected = source_probe["video"].get(key)
        actual = result["video"].get(key)
        if key == "r_frame_rate" and expected and actual:
            try:
                expected, actual = Fraction(expected), Fraction(actual)
                if expected != actual and abs(actual / expected - 1) < Fraction(2, 1000):
                    # 换容器可能只改变 ffprobe 推断的标称帧率；两边实测平均帧率须完全相等。
                    src_avg = Fraction(source_probe["video"].get("avg_frame_rate") or "0")
                    dst_avg = Fraction(result["video"].get("avg_frame_rate") or "0")
                    if src_avg == dst_avg and src_avg in (expected, actual):
                        continue
            except (ValueError, ZeroDivisionError):
                pass
        if expected and expected != actual:
            raise RuntimeError(
                f"成品自检失败: {label}从 {expected} 变成 {actual}，违反视频直通规则"
            )
    # 比的是“画面有没有少”。文件总时长取的是所有轨道里最长的那条：只留一条音轨和一条字幕后，
    # 总时长会跟着被丢掉的轨道变短（NAS 实测：一条比画面长 2 秒的字幕被去掉，旧规则误判失败）。
    # 所以两边都读得到视频轨时长时只比视频轨；读不到才退回总时长，且只查成品是否变短。
    src_video = source_probe["video"].get("duration") or 0
    out_video = result["video"].get("duration") or 0
    if src_video > 0 and out_video > 0:
        if abs(out_video - src_video) > 2.0:
            raise RuntimeError(f"成品自检失败: 画面时长偏差 {abs(out_video - src_video):.1f}s")
    else:
        reference = src_video or source_probe["duration"]
        actual = out_video or result["duration"]
        if reference > 0 and (actual <= 0 or reference - actual > 5.0):
            raise RuntimeError(f"成品自检失败: 成品比源短 {reference - actual:.1f}s")
    if expected_audio is None:
        expected_audio = len(source_probe["audio"]) + extra_audio
    if len(result["audio"]) != expected_audio:
        raise RuntimeError(
            f"成品自检失败: 音轨由 {expected_audio} 条变成 {len(result['audio'])} 条"
        )
    if (expected_text_subtitles is not None
            and len(result["subs"]) != expected_text_subtitles):
        raise RuntimeError(
            f"成品自检失败: 应有 {expected_text_subtitles} 条内嵌字幕，"
            f"实际探测到 {len(result['subs'])} 条"
        )


def unique_dst(stem, ext="mkv"):
    MOVIES.mkdir(parents=True, exist_ok=True)
    dst = MOVIES / f"{stem}.{ext}"
    n = 1
    while dst.exists():
        dst = MOVIES / f"{stem}.v{n}.{ext}"
        n += 1
    return dst


def copy_media_file(job, source, target):
    total = max(source.stat().st_size, 1)
    copied = 0
    with source.open("rb") as reader, target.open("wb") as writer:
        while True:
            free = disk_free()
            if free is not None and free < MIN_FREE_ABORT:
                raise RuntimeError("复制期间磁盘空间不足，已停止入库")
            chunk = reader.read(8 * 1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            copied += len(chunk)
            job["percent"] = min(99, copied / total * 100)
    shutil.copystat(source, target)


def verify_source_unchanged(job, source):
    if source_snapshot(source) != job["source_snapshot"]:
        raise RuntimeError("处理期间源文件发生变化，可能仍在下载；已拒绝入库，请完成下载后重试")


def validate_p7_timing(video):
    """裸 HEVC 按固定帧率重建时间轴，拒绝明显不符合此前提的输入。"""
    try:
        rate = Fraction(video.get("r_frame_rate", "0/0"))
        if rate <= 0:
            raise ValueError
        average = video.get("avg_frame_rate")
        if average and average != "0/0" and abs(float(Fraction(average) / rate) - 1) > 0.001:
            raise ValueError
        if abs(float(video.get("start_time") or 0)) > 0.05:
            raise ValueError
    except (ValueError, ZeroDivisionError, TypeError):
        raise RuntimeError("P7 输入帧率或起始时间不适合裸流重建，无法保证音画同步；请先检查源时间轴")


def item_source_root(item):
    """这个条目对应的源文件或目录；必须真实位于 download 之内、且不是施工区。

    普通条目是 download 下的顶层文件/目录。多盘目录展开出来的子盘，源是其中一张盘
    自己的目录（或镜像文件），核对快照和删除源文件都只针对这一张盘。
    """
    root = Path(item.get("root") or (INBOX / item["name"]))
    inbox, work, resolved = INBOX.resolve(), WORK.resolve(), root.resolve()
    if resolved == inbox or not resolved.is_relative_to(inbox):
        raise ValueError("非法源路径")
    if resolved == work or resolved.is_relative_to(work):
        raise ValueError("非法源路径")
    return root


def resolve_selection(job, item, probe, before_probe=None):
    """把用户在页面上选的音轨/字幕落到真实轨道上；对不上时退回自动预选并留警告。"""
    chosen = item.get("selection")
    if chosen and item["kind"] in DISC_KINDS and before_probe is not None:
        mapped = remap_selection(before_probe, probe, chosen)
        if mapped is None:
            add_warning(job, "提取出的轨道和预读时不一致，没法对上你选的音轨/字幕，已改用自动预选，请验片")
            chosen = None
        else:
            chosen = mapped
    return normalize_selection(probe, chosen)


def direct_disc_single_pass(job, item, src: Path, jobdir: Path, stem, source_entry):
    """未加密原盘、且成品不需要改动任何轨道时，一遍直接出成品，省掉一整份临时空间。

    条件：能直接读盘、不是杜比视界（那要进 MP4）、选中的音轨电视都能直接播。
    这时成品就是“正片 + 选中的音轨和字幕”原样复制，没有第二遍可做。
    不满足条件时返回 None，由调用方走“先取出、再封装”的两遍流程。
    """
    if not disc_is_plain_folder(src):
        return None
    try:
        info = direct_disc_info(src)
    except DirectDiscUnsupported:
        return None
    selection = resolve_selection(job, item, info, item.get("probe"))
    plan = make_plan(item["kind"], info, src, selection)
    if plan["container"] != "mkv" or any(entry["compat"] for entry in plan["audio"]) or plan["subs"]:
        return None
    job["steps"] = ["取出原盘正片", "成品自检", "移入 Movies"]
    job["step_idx"] = 0
    job["plan"] = ["直接读取原盘正片"]
    job["verdict"] = "must"
    if info["disc_preview"].get("similar_titles", 0) > 1:
        add_warning(job, "原盘有多个时长接近的标题，本次只取最长的；它可能不是所需剪辑版，请验片")
    log(job, "保留音轨: " + ("；".join(audio_track_label(info["audio"][i]) for i in selection["audio"]) or "无")
             + " | 保留字幕: " + ("；".join(sub_track_label(info["subs"][j]) for j in selection["subs"]) or "无"))
    out = direct_disc_rip(job, info, jobdir / f"{stem}.mkv", selection)
    set_phase(job, "成品自检", 0)
    result = summarize_probe(ffprobe(out))
    for key, label in (("codec", "视频编码"), ("width", "宽度"), ("height", "高度")):
        if result["video"].get(key) != info["video"].get(key):
            raise RuntimeError(f"成品自检失败: {label}和原盘不一致")
    if info["video"].get("dv_profile"):
        verify_dv_preserved(out, info["video"]["dv_profile"], info["duration"], info["video"])
    if len(result["audio"]) != len(selection["audio"]) or len(result["subs"]) != len(selection["subs"]):
        raise RuntimeError("成品自检失败: 音轨或字幕条数和选择的不一致")
    with LOCK:
        item["probe"] = info
    job["format_preview"] = format_preview(item["kind"], result, out)
    job["percent"] = 100
    log(job, "自检通过: 视频编码 / 时长 / 音轨 / 字幕")
    verify_source_unchanged(job, source_entry)
    dst = unique_dst(stem, "mkv")
    if info.get("subs"):
        capture_disc_timing(job, info, jobdir, dst)
    set_phase(job, "移入 Movies", None)
    shutil.move(str(out), dst)
    return dst


def capture_disc_timing(job, info, jobdir: Path, dst: Path):
    """原盘一遍直出时成品里不带字幕：再把盘读一遍、只取字幕，存下时间点。失败只提示，不影响成品。"""
    set_phase(job, "保存字幕时间点", 0)
    try:
        identified = _mkvmerge_identify(info["disc_preview"]["playlist"]) or {}
        clips = ((identified.get("container") or {}).get("properties") or {}).get("playlist_file") or []
        tracks = disc_subtitle_time_points(clips, float(info.get("duration") or 0)) if clips else []
        kept = save_subtitle_timing(dst, [], tracks) if tracks else 0
        log(job, f"已存下 {kept} 条字幕的时间点，以后校准字幕用" if kept else "盘里的字幕没读出时间点")
    except Exception as exc:
        log(job, f"字幕时间点没存下来: {exc}")
    job["percent"] = 100


def disc_reads_directly(item):
    return ((item.get("probe") or {}).get("disc_preview") or {}).get("backend") == "direct"


def process_item(job, item):
    name = item["name"]
    source_entry = item_source_root(item)
    if item["kind"] == "dir" and multiple_video_files(source_entry):
        raise ValueError("目录包含多个视频，请先拆分后处理")
    job["source_snapshot"] = source_snapshot(source_entry)
    jobdir = WORK / ("job_" + uuid.uuid4().hex)
    jobdir.mkdir(parents=True, exist_ok=True)
    try:
        payload = Path(item["payload"])
        source_payload = payload
        stem = output_stem(name, item["kind"])
        before_probe = item.get("probe")
        plan_steps(job, item["kind"])

        # 1) 原盘先取出正片。未加密的蓝光目录直接读；其余交给 MakeMKV。
        if item["kind"] in DISC_KINDS:
            done = direct_disc_single_pass(job, item, payload, jobdir, stem, source_entry)
            if done is not None:
                return done
            payload, disc_info = rip_disc(job, payload, jobdir / "rip")
            if disc_info is not None:
                before_probe = disc_info

        # 2) 探测(原盘提取后必须重新探测)
        set_phase(job, "探测流信息", None)
        probe = summarize_probe(ffprobe(payload))
        if not probe or not probe["video"]:
            raise RuntimeError("ffprobe 无法解析该文件")
        with LOCK:
            item["probe"] = probe
        selection = resolve_selection(job, item, probe, before_probe)
        plan = make_plan(item["kind"], probe, source_payload, selection)
        as_is = bool(item.get("as_is"))
        if as_is and (plan["needs"] == "must" or item["kind"] in DISC_KINDS
                      or source_payload.suffix.lower() not in AS_IS_CONTAINERS):
            raise RuntimeError("这个文件必须转换后电视才能正常播放，不能原样入库，请改用“转换”")
        # 不需要转换的文件，点“转换”也只是原样复制入库
        direct = as_is or (plan["needs"] == "none" and item["kind"] not in DISC_KINDS)
        actions = [AS_IS_PLAN] if as_is else build_plan(item["kind"], probe, source_payload, selection)[0]
        job["plan"] = list(actions)
        job["verdict"] = "none" if direct else plan["needs"]
        log(job, f"视频{probe['video']['codec']} dv={probe['video']['dv_profile']} | "
                 f"音轨{[a['codec'] for a in probe['audio']]} | "
                 f"字幕{[x['codec'] for x in probe['subs']]}")
        log(job, "计划: " + ", ".join(actions))
        dur = probe["duration"]
        real_p7 = "p7" in plan["must"]
        is_dv = probe["video"]["dv_profile"] != 0
        container = plan["container"]
        preview = format_preview(item["kind"], probe, source_payload, as_is=direct, selection=selection)
        job["format_preview"] = preview
        plan_steps(job, item["kind"], real_p7, container, direct=direct)
        set_phase(job, "探测流信息")

        # 3) 不转换 → 原样复制入库
        if direct:
            original_ext = payload.suffix.lstrip(".").lower()
            dst = unique_dst(stem, original_ext if original_ext in {
                ext.lstrip(".") for ext in VIDEO_EXT
            } else "mkv")
            set_phase(job, "直接入库(复制中)", 0)
            staging = jobdir / dst.name
            copy_media_file(job, payload, staging)
            verify_source_unchanged(job, source_entry)
            set_phase(job, "移入 Movies", None)
            shutil.move(str(staging), dst)
            return dst

        chosen_audio = [audio_track_label(probe["audio"][i]) for i in selection["audio"]]
        chosen_subs = [sub_track_label(probe["subs"][e["src"]]) for e in plan["subs"]]
        log(job, "保留音轨: " + ("；".join(chosen_audio) or "无")
                 + " | 保留字幕: " + ("；".join(chosen_subs) or "无"))
        if real_p7:
            validate_p7_timing(probe["video"])
            add_warning(job, "P7→P8.1 保留基础层和动态元数据，但丢弃增强层（含 FEL）；不等同于完整原盘画质")
        if any(entry["lossy"] for entry in plan["audio"]):
            add_warning(job, "所选音轨中有原轨被兼容轨替换，详见处理计划")

        sidecars = []
        timing_files = []
        if is_dv and container == "mp4":
            # 4) 杜比视界 → MP4。P7 先转 P8.1 并重建时间戳，P5/P8 视频直通。
            out = jobdir / f"{stem}.mp4"
            bl = None
            if real_p7:
                bl = dovi_convert(job, payload, jobdir)
                bl = rebuild_bl_timestamps(job, bl, probe["video"]["r_frame_rate"], jobdir)
            audio_count, sub_count, sidecars = ffmpeg_mp4_mux(
                job, "封装MP4(保留DV)", payload, probe, out, selection, dur, bl_video=bl)
            set_phase(job, "成品自检", 0)
            if real_p7:
                verify_dovi_p8(out, dur)
            else:
                verify_dv_preserved(out, probe["video"]["dv_profile"], dur, probe["video"])
            verify_media_preserved(out, probe, sub_count, expected_audio=audio_count)
            for sidecar in sidecars:
                if not sidecar.is_file() or sidecar.stat().st_size == 0:
                    raise RuntimeError(f"成品自检失败: 外挂字幕 {sidecar.name} 没有生成")
            log(job, "自检通过: 杜比视界配置 / 时长 / 音轨 / 字幕")
        else:
            # 5) 其余一律 MKV：视频直通，音频/字幕按选择处理，图形字幕原样留在文件里
            out = jobdir / f"{stem}.mkv"
            args, audio_count, sub_count = audio_sub_args(probe, selection)
            sidecar_args, sidecars = subtitle_sidecar_outputs(out, probe, selection["subs"])
            anchor_args, timing_files = anchor_subtitle_outputs(jobdir, probe, selection["subs"])
            run_ffmpeg_progress(
                job,
                ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1",
                 "-i", str(payload), "-map", "0:v:0", "-c:v", "copy"]
                + args + [str(out)] + sidecar_args + anchor_args,
                dur, "封装MKV(视频直通)")
            set_phase(job, "成品自检", 0)
            if is_dv:
                verify_dv_preserved(out, probe["video"]["dv_profile"], dur, probe["video"])
            verify_media_preserved(out, probe, sub_count, expected_audio=audio_count)
            for sidecar in sidecars:
                if not sidecar.is_file() or sidecar.stat().st_size == 0:
                    raise RuntimeError(f"成品自检失败: 外挂字幕 {sidecar.name} 没有生成")
            log(job, "自检通过: 视频编码 / 时长 / 音轨 / 字幕")
        job["percent"] = 100

        # 6) 入库
        verify_source_unchanged(job, source_entry)
        dst = unique_dst(stem, container)
        # 字幕时间点：趁导出的字幕文件还在临时目录里读一遍存下来，以后删了源也能给字幕对时间
        try:
            kept = save_subtitle_timing(dst, list(sidecars) + list(timing_files))
            if kept:
                log(job, f"已存下 {kept} 条字幕的时间点，以后校准字幕用")
        except Exception as exc:                      # 存不下只影响以后的自动校准，不影响成品
            log(job, f"字幕时间点没存下来: {exc}")
        set_phase(job, "移入 Movies", None)
        shutil.move(str(out), dst)
        moved = []
        for sidecar in sidecars:
            target = dst.with_name(dst.stem + sidecar.name[len(out.stem):])
            shutil.move(str(sidecar), target)
            moved.append(str(target))
        if moved:
            job["sidecars"] = moved
            log(job, "外挂字幕: " + "、".join(Path(path).name for path in moved))
        return dst
    finally:
        # 中间文件自动清;成品已搬走;inbox源保留待人工验片后在面板删
        if not job.get("preserve_work"):
            shutil.rmtree(jobdir, ignore_errors=True)


def claim_next_job():
    """取队列与登记当前任务必须同锁完成，升级不能插入二者之间。"""
    global CURRENT_JOB
    with LOCK:
        if CURRENT_JOB or ACTIVE_OPERATIONS or processing_block_reason():
            return None
        while JOB_QUEUE:
            name = JOB_QUEUE.pop(0)
            item = ITEMS.get(name)
            if not item or item.get("status") != "queued":
                continue
            item["status"] = "processing"
            item["error"] = None
            CURRENT_JOB = {
                "name": name, "phase": "启动", "percent": 0,
                "log": [], "warnings": [], "started": time.time(),
            }
            return item
    return None


def attach_prepared_subtitle(job, name, dst):
    """转换前在卡片上备好的中文字幕，转完后原样放到影片旁边（片名.zh-CN.srt）。

    不做时间轴校准；对不上时在已入库的卡片上重新选它，走校准流程。出错只提示，不影响成品。
    """
    prepared = subtitle_candidate_path(name)
    if not prepared.is_file():
        return
    try:
        target = subtitle_sidecar_path(dst)
        shutil.copyfile(prepared, target)
        job["subtitle_sidecar"] = str(target)
        log(job, f"已放好转换前备的中文字幕: {target.name}")
    except OSError as exc:
        add_warning(job, f"转换前备好的中文字幕没能放到影片旁边（{exc}），请在卡片上重新添加")


def worker_loop():
    global CURRENT_JOB
    while True:
        item = claim_next_job()
        if not item:
            time.sleep(2)
            continue
        name = item["name"]
        job = CURRENT_JOB
        try:
            ok, reason = precheck_space(item)
            if not ok:
                raise RuntimeError(reason)
            log(job, f"开始处理: {name}")
            free = disk_free()
            if free is not None:
                log(job, f"当前剩余空间 {human(free)},预计峰值占用 {human(estimate_peak(item))}")
            dst = process_item(job, item)
            attach_prepared_subtitle(job, name, dst)
            hand_over(dst, *local_sidecars(dst))
            with LOCK:
                PROCESSED[name] = {
                    "output": str(dst),
                    "ts": time.strftime("%m-%d %H:%M"),
                    "warnings": list(job.get("warnings", [])),
                    "format_preview": job.get("format_preview"),
                    "subtitle_sidecar": job.get("subtitle_sidecar"),
                    "source_snapshot": job.get("source_snapshot"),
                    "source_size": item.get("size"),
                    "ocr": job.get("ocr"),
                    "plan": job.get("plan"),
                    "sidecars": job.get("sidecars") or [],
                    "verdict": job.get("verdict"),
                }
                save_state()
                item["status"] = "done"
            log(job, f"完成 → {dst}")
        except Exception as e:
            with LOCK:
                item["status"] = "error"
                item["error"] = str(e)
            log(job, "出错: " + str(e))
            traceback.print_exc()
        finally:
            time.sleep(3)  # 让前端拉到最后一屏日志
            with LOCK:
                CURRENT_JOB = None


OCR_PLANNED_DETAIL = "PGS 按原时间轴 OCR 为文本"
OCR_PLANNED_TAG_RE = re.compile(r"\d+条图形字幕OCR成文本")


def recorded_ocr_outcome(processed):
    """返回已完成任务实际的 (成功条数, 总条数)；没有 PGS 或无法判断时返回 None。

    旧版本的完成记录没有 ocr 字段，从当时留下的持久警告里还原。
    """
    ocr = processed.get("ocr")
    if isinstance(ocr, dict):
        try:
            return int(ocr["ok"]), int(ocr["total"])
        except (KeyError, TypeError, ValueError):
            return None
    for warning in processed.get("warnings") or []:
        failed = re.search(r"(\d+)条 PGS 均未成功转成文本", str(warning))
        if failed:
            return 0, int(failed.group(1))
        partial = re.search(r"(\d+)条 PGS 中仅 (\d+) 条成功转成文本", str(warning))
        if partial:
            return int(partial.group(2)), int(partial.group(1))
    return None


def apply_ocr_outcome(preview, plan, outcome):
    """把“预计 OCR”的说法换成实际结果；返回 (preview, plan) 的副本。"""
    preview = dict(preview) if preview else preview
    plan = list(plan or [])
    if not outcome:
        return preview, plan
    ok, total = outcome
    if ok >= total:
        detail = f"{total} 条 PGS 已按原时间轴 OCR 为文本"
        tag = f"{total}条图形字幕已OCR成文本"
    elif ok <= 0:
        detail = f"⚠ {total} 条 PGS 均未转成文本，这些图形字幕不在成品里"
        tag = f"⚠ 图形字幕OCR失败 0/{total}"
    else:
        detail = f"⚠ PGS 仅 {ok}/{total} 条转成文本，其余未内嵌"
        tag = f"⚠ 图形字幕OCR仅成功 {ok}/{total}"
    if preview and preview.get("target_detail"):
        preview["target_detail"] = preview["target_detail"].replace(
            OCR_PLANNED_DETAIL, detail
        )
    replaced = [tag if OCR_PLANNED_TAG_RE.fullmatch(str(entry)) else entry for entry in plan]
    if replaced == plan and ok < total and tag not in plan:
        replaced.append(tag)   # 旧记录没存计划时，至少把字幕缺失标出来
    return preview, replaced


# ---------- API ----------
# 面板没有登录，且升级接口能在宿主机上构建并运行上传的代码。只校验“来源 IP 是
# 内网”挡不住浏览器：局域网里的浏览器打开任意外部网页，该网页都能向面板提交
# 表单（跨站请求），或用 DNS 重绑定把自己的域名指到 NAS。因此：
#   1. Host 必须是 IP、localhost、单标签主机名或常见内网后缀，挡 DNS 重绑定；
#      经自定义域名/反向代理访问时，用环境变量 MEDIAPANEL_ALLOWED_HOSTS 放行
#      （逗号分隔的主机名，不含端口）。
#   2. 有副作用的请求若带 Origin/Referer，必须与 Host 同源，挡跨站表单。
#      两者都没有的是 curl 等非浏览器客户端，不属于跨站请求，放行。
LOCAL_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localhost")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname_of(netloc):
    """从 Host 头或 URL 的 netloc 中取出小写主机名（去端口、去 IPv6 方括号）。"""
    try:
        return (urlparse.urlsplit("//" + str(netloc or "")).hostname or "").lower()
    except ValueError:
        return ""


def host_is_trusted(netloc):
    hostname = _hostname_of(netloc)
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    extra = {
        value.strip().lower()
        for value in os.environ.get("MEDIAPANEL_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    }
    return (hostname in extra or hostname == "localhost" or "." not in hostname
            or hostname.endswith(LOCAL_HOST_SUFFIXES))


def request_is_same_origin(host, origin, referer):
    """浏览器发来的修改请求必须来自面板自身页面。"""
    source = origin or referer
    if not source:
        return True
    try:
        parsed = urlparse.urlsplit(source)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc.lower() == str(host or "").lower()


@app.before_request
def reject_cross_site_requests():
    if not host_is_trusted(request.host):
        return jsonify({
            "error": "不受信任的访问地址；请用 NAS 的内网 IP 打开面板，"
                     "或把该域名加入环境变量 MEDIAPANEL_ALLOWED_HOSTS"
        }), 403
    if request.method not in SAFE_METHODS and not request_is_same_origin(
            request.host, request.headers.get("Origin"), request.headers.get("Referer")):
        return jsonify({"error": "已拒绝来自其他网站的请求"}), 403
    return None


@app.get("/api/state")
def api_state():
    with LOCK:
        items = []
        block_reason = processing_block_reason()
        SELECTABLE = {"ready", "error"}
        for it in ITEMS.values():
            d = {k: it[k] for k in ("name", "kind", "size", "status", "plan", "clean", "error")}
            # 只有“已完成”的条目才展示完成记录；同名新下载期间不套用旧成品的信息。
            processed = PROCESSED.get(it["name"], {}) if it["status"] == "done" else {}
            d["size_h"] = human(it["size"])
            d["preview_status"] = it.get("preview_status")
            d["preview_error"] = it.get("preview_error")
            d["done_ts"] = processed.get("ts")
            d["warnings"] = list(processed.get("warnings", []))
            if it.get("source_changed"):
                d["warnings"].append(
                    "源文件大小与处理时不同，可能是后来下载的同名内容；"
                    "当前成品对应的是旧源，如需处理新内容请点“删除当前成品 · 重新生成”"
                )
            output = Path(processed.get("output", "")) if processed.get("output") else None
            d["output_name"] = output.name if output else None
            cached_output = OUTPUT_INFO.get(output)
            d["output_size_h"] = human(cached_output["stamp"][0]) if cached_output else None
            d["info_unavailable"] = bool(cached_output and not cached_output["probe"] and cached_output["tries"] >= 3)
            d["format_preview"] = (
                processed.get("format_preview")
                or format_preview(
                    it["kind"], it.get("probe"), it.get("payload", ""),
                    it.get("preview_status"), it.get("preview_error"),
                )
            )
            if it["status"] == "done":
                # 已完成条目不会再被探测；服务重启后内存里的计划是空的，
                # 所以优先用完成时存下的计划，而不是让页面显示“等待探测”。
                d["format_preview"], d["plan"] = apply_ocr_outcome(
                    d["format_preview"], processed.get("plan") or d["plan"],
                    recorded_ocr_outcome(processed),
                )
            preview_allows_processing = (
                it["kind"] not in DISC_KINDS
                or it.get("preview_status") in {"ready", "failed"}
            )
            d["selectable"] = (it["status"] in SELECTABLE and preview_allows_processing
                               and not block_reason and not it.get("discarding"))
            d["discardable"] = (it["status"] in DISCARDABLE and not it.get("discarding")
                                and it.get("preview_status") not in ("queued", "analyzing")
                                and not block_reason)
            # 卡片：要不要转、得失、可选的音轨和字幕。已完成的条目用完成时的结论。
            d["card"] = (conversion_card(it["kind"], it.get("probe"), it.get("payload", ""),
                                         it["size"], it.get("selection"))
                         if it["status"] in ("ready", "error", "queued", "processing") else None)
            d["done_verdict"] = processed.get("verdict")
            d["sidecar_count"] = len(processed.get("sidecars") or [])
            d["info"] = output_info_lines(output) if (output and it["status"] == "done") else None
            # 小标签：入库的按成品算（电视上实际播的），还没转换的按片源算
            d["badges"] = (d["info"] or {}).get("badges") if it["status"] == "done" else media_badges(it.get("probe"))
            d["deletable"] = (it["status"] == "done" and bool(processed.get("source_snapshot"))
                              and not block_reason)
            d["delete_block_reason"] = (
                "旧任务缺少源文件核对记录，源文件需手动核对后清理"
                if it["status"] == "done" and not processed.get("source_snapshot")
                and it["kind"] != LIBRARY_KIND else ""
            )
            d["retryable"] = it["status"] == "done" and not block_reason and it["kind"] != LIBRARY_KIND
            d["library"] = it["kind"] == LIBRARY_KIND
            d["optional"] = bool(it.get("optional")) and d["selectable"]
            if d["optional"]:
                d["plan"] = [OPTIONAL_TAG] + list(d["plan"])
            candidate = subtitle_candidate_path(it["name"])
            try:
                # 转换前备下的字幕只对还没入库的影片有意义；入库后以影片旁边实际存在的字幕文件为准
                d["subtitle_candidate"] = it["status"] != "done" and candidate.is_file()
                name_file = candidate.with_suffix(".name")
                d["subtitle_candidate_name"] = (
                    name_file.read_text("utf-8", "replace")
                    if d["subtitle_candidate"] and name_file.is_file() else None
                )
            except OSError:
                d["subtitle_candidate"] = False
                d["subtitle_candidate_name"] = None
            raw_sidecar = processed.get("subtitle_sidecar", "")
            sidecar = Path(raw_sidecar) if raw_sidecar else None
            try:
                d["subtitle_sidecar"] = bool(sidecar and sidecar.is_file())
                d["subtitle_sidecar_name"] = sidecar.name if d["subtitle_sidecar"] else None
            except OSError:
                d["subtitle_sidecar"] = False
                d["subtitle_sidecar_name"] = None
            items.append(d)
        job = dict(CURRENT_JOB) if CURRENT_JOB else None
        if job and job.get("started"):
            elapsed = int(time.time() - job["started"])
            job["elapsed"] = elapsed
            # 用"当前阶段的百分比+已用时间"外推剩余时间
            pct = job.get("percent") or 0
            phase_start = job.get("phase_start") or job["started"]
            phase_elapsed = time.time() - phase_start
            if pct >= 3 and pct < 100 and phase_elapsed > 5:
                total_est = phase_elapsed / (pct / 100.0)
                remain = max(0, int(total_est - phase_elapsed))
                job["eta_sec"] = remain
                job["eta_clock"] = time.strftime("%H:%M", time.localtime(time.time() + remain))
        free = disk_free()
        total = disk_total()
        disk = None
        if free is not None and total:
            disk = {"free": free, "total": total, "free_h": human(free),
                    "total_h": human(total), "pct_used": round((total - free) / total * 100, 1),
                    "low": free < (SAFETY_MARGIN + MIN_FREE_ABORT)}
        # 队列名单(排队中的条目名,按顺序)
        queue_names = list(JOB_QUEUE)
        ai_config = subtitle_ai_config()
        return jsonify({
            "items": items,
            "job": job,
            "queue": queue_names,
            "disk": disk,
            "upgrade": read_upgrade_status(),
            "processing_block_reason": block_reason,
            "makemkv_update": dict(MAKEMKV_UPDATE),
            "app_version": globals().get("APP_VERSION", ""),
            "help_url": f"https://github.com/{PROJECT_REPO}/blob/main/使用说明.md",
            "jellyfin_link": env_text("JELLYFIN_PUBLIC_URL"), "jellyfin_port": JELLYFIN_PORT,
            "folders": {"inbox": INBOX.name, "library": MOVIES.name, **{key + "_path": value for key, value in current_folders().items()}},
            "release": globals().get("APP_RELEASE", ""),
            "subtitle_ai": {
                "configured": bool(ai_config),
                "endpoint": ai_config.get("endpoint", "") if ai_config else "",
                "model": ai_config.get("model", "") if ai_config else "",
            },
        })


@app.post("/api/process")
@media_mutation
def api_process():
    body = request.get_json(silent=True) or {}
    names = body.get("names", [])
    as_is = body.get("as_is") is True
    selections = body.get("selection") if isinstance(body.get("selection"), dict) else {}
    added = []
    warnings = []
    with LOCK:
        for n in names:
            it = ITEMS.get(n)
            if as_is and it and not it.get("optional"):
                warnings.append(f"{n}: 这个文件必须处理后才能正常播放，不能原样入库。")
                continue
            preview_allows_processing = (
                it and (
                    it.get("kind") not in DISC_KINDS
                    or it.get("preview_status") in {"ready", "failed"}
                )
            )
            if (it and preview_allows_processing
                    and it["status"] in ("ready", "error") and n not in JOB_QUEUE
                    and not it.get("discarding")):
                it["status"] = "queued"
                it["as_is"] = as_is
                chosen = selections.get(n)
                it["selection"] = chosen if isinstance(chosen, dict) else None
                JOB_QUEUE.append(n)
                added.append(n)
            elif it and it.get("preview_status") in {"queued", "analyzing"}:
                warnings.append(f"{n}: 正在预读原盘规格，完成后才能开始处理。")
        # 粗略提示: 队列里最大单个任务的峰值 vs 当前剩余(串行处理,峰值看最大那个)
        free = disk_free()
        if free is not None and JOB_QUEUE:
            max_peak = max((estimate_peak(ITEMS[n]) for n in JOB_QUEUE if n in ITEMS), default=0)
            if free < max_peak + SAFETY_MARGIN:
                warnings.append(f"注意: 剩余空间 {human(free)},队列中最大任务预计需 "
                                f"{human(max_peak)} 临时空间,可能不足。空间不够的任务会被自动跳过并标记,"
                                f"不影响其他任务。")
    return jsonify({"queued": added, "warnings": warnings})


@app.get("/api/makemkv_diag")
def api_makemkv_diag():
    """只读诊断：在 makemkv 容器里跑一组写死的查看命令和读盘实验，返回完整输出。

    不接受调用方传入任何命令或参数；name 只用来从现有条目里挑一张盘，
    only 用来从下面写死的实验名里挑几个。用于排查只在 NAS 上出现的读盘问题。
    """
    name = request.args.get("name", "")
    only = set(filter(None, request.args.get("only", "").split(",")))
    with LOCK:
        it = ITEMS.get(name)
        if not it or it.get("kind") not in DISC_KINDS:
            return jsonify({"error": "没有找到这张盘"}), 404
        if CURRENT_JOB or DISC_PREVIEW_ACTIVE:
            return jsonify({"error": "有任务或读盘正在进行，稍后再试"}), 409
        disc = Path(it["payload"])
    import docker
    source = makemkv_source_spec(disc)
    info = [MAKEMKVCON, "-r", "--minlength=2400"]
    shell = ["/bin/sh", "-c"]
    experiments = [
        ("环境", shell + [
            "id; uname -r; echo ---settings; sed -E 's/(app_Key *= *\"?).{6}.*/\\1******/' /config/settings.conf 2>&1; "
            "echo ---key-env; env | grep -c '^MAKEMKV_KEY=' ; env | grep '^MAKEMKV_KEY=' | cut -c1-16; "
            "echo ---version; ls -la /opt/makemkv/bin /opt/makemkv/lib 2>&1 | head -40; "
            "echo ---limits; ulimit -a; echo ---dmesg; dmesg 2>&1 | tail -15; "
            "echo ---disc; ls -la \"$1\" \"$1/BDMV\" 2>&1 | head -30; ls -la \"$1/BDMV/PLAYLIST\" | head -12",
            "diag", str(disc)], None, None),
        ("默认（HOME=/config）", info + ["info", source], MAKEMKV_ENV, None),
        ("关闭直接读写", info + ["--directio=false", "info", source], MAKEMKV_ENV, None),
        ("不带缓存参数、关闭直接读写、只读第一个长标题", info + ["--directio=false", "--noscan", "info", source], MAKEMKV_ENV, None),
        ("换成不含方括号和空格的路径", shell + [
            "ln -sfn \"$1\" /tmp/mpdisc && exec \"$2\" -r --minlength=2400 info file:/tmp/mpdisc",
            "diag", str(disc), MAKEMKVCON], MAKEMKV_ENV, None),
        ("以容器自己的 app 用户运行", info + ["info", source], MAKEMKV_ENV, "1000:1000"),
        ("放开栈大小限制", shell + [
            "ulimit -s unlimited 2>/dev/null; exec \"$2\" -r --minlength=2400 info \"$1\"",
            "diag", source, MAKEMKVCON], MAKEMKV_ENV, None),
        ("老办法（HOME=/root）", info + ["info", source], {"HOME": "/root"}, None),
    ]
    report = []
    client = docker.from_env()
    try:
        container = client.containers.get(MAKEMKV_CONTAINER)
        container.reload()
        images = []
        for image in client.images.list(all=True):
            digests = " ".join(image.attrs.get("RepoDigests") or []) + " ".join(image.tags or [])
            if "makemkv" in digests.lower():
                images.append({"id": image.id[:19], "tags": image.tags,
                               "created": str(image.attrs.get("Created"))[:19],
                               "in_use": image.id == container.image.id})
        with MAKEMKV_LOCK:
            for title, command, environment, user in experiments:
                if only and title not in only:
                    continue
                started = time.time()
                try:
                    kwargs = {"demux": True, "environment": environment}
                    if user:
                        kwargs["user"] = user
                    result = container.exec_run(command, **kwargs)
                    out = result.output
                    if isinstance(out, tuple):
                        out = b"\n".join(part for part in out if part)
                    text = (out or b"").decode("utf-8", "replace")
                    lines = [line for line in text.splitlines() if not line.startswith("DRV:")]
                    report.append({"step": title, "exit_code": result.exit_code,
                                   "seconds": round(time.time() - started, 1),
                                   "lines": len(lines), "output": "\n".join(lines)[-5000:]})
                except Exception as exc:
                    report.append({"step": title, "error": str(exc)[:500]})
    finally:
        client.close()
    return jsonify({"disc": name, "images": images, "report": report})


@app.post("/api/makemkv_update")
def api_makemkv_update():
    """更新 makemkv 容器到最新版（会下载镜像并重建那个容器）。空闲时才允许。"""
    remote = getattr(request, "remote_addr", "") or ""
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return jsonify({"error": "无法确认请求来自本机局域网"}), 403
    if not (address.is_private or address.is_loopback):
        return jsonify({"error": "只允许在局域网内操作"}), 403
    with LOCK:
        if makemkv_update_running():
            return jsonify({"ok": True, "message": "已经在更新了"})
        if CURRENT_JOB or JOB_QUEUE or DISC_PREVIEW_ACTIVE or ACTIVE_OPERATIONS:
            return jsonify({"error": "还有任务在运行或排队，等它们结束后再更新"}), 409
        if read_upgrade_status().get("status") in UPGRADE_BUSY:
            return jsonify({"error": "服务正在升级"}), 409
        _set_makemkv_update("running", "正在准备更新读盘工具")
        threading.Thread(target=update_makemkv_container, daemon=True).start()
    return jsonify({"ok": True, "message": "开始更新读盘工具，完成后会自动重新读取原盘"}), 202


@app.post("/api/repreview")
def api_repreview():
    """原盘预读失败后重新读一次（比如重启了 makemkv 容器之后）。不改动任何文件。"""
    name = (request.get_json(silent=True) or {}).get("name", "")
    with LOCK:
        it = ITEMS.get(name)
        if not it or it.get("kind") not in DISC_KINDS:
            return jsonify({"error": "没有找到这张盘"}), 404
        if it["status"] in ("processing", "queued"):
            return jsonify({"error": "任务正在运行或排队"}), 409
        if it.get("preview_status") in ("queued", "analyzing"):
            return jsonify({"ok": True})
        it["preview_status"] = None       # 下一轮扫描会重新排队预读
        it["preview_error"] = None
    return jsonify({"ok": True})


@app.post("/api/cancel")
def api_cancel():
    """取消一个还在排队(未开始)的任务"""
    name = (request.get_json(silent=True) or {}).get("name", "")
    with LOCK:
        if name in JOB_QUEUE:
            JOB_QUEUE.remove(name)
            it = ITEMS.get(name)
            if it and it["status"] == "queued":
                it["status"] = "ready"
                it.pop("as_is", None)
                it.pop("selection", None)
            return jsonify({"ok": True})
    return jsonify({"error": "该任务不在排队中(可能已开始或已完成)"}), 400


@app.post("/api/delete_source")
@media_mutation
def api_delete_source():
    name = (request.get_json(silent=True) or {}).get("name", "")
    with LOCK:
        it = ITEMS.get(name)
        if not it:
            return jsonify({"error": "该条目不存在"}), 404
        if it["status"] != "done" or it.get("kind") == LIBRARY_KIND:
            return jsonify({"error": "仅允许删除已完成且已经验片的任务源文件"}), 409
        output = tracked_movie_output(name)
        if output is None:
            return jsonify({
                "error": "Movies 成品不存在或记录已失效，为保护源文件已拒绝删除"
            }), 409
        try:
            target = item_source_root(it)
        except ValueError:
            return jsonify({"error": "源路径不安全，已拒绝删除"}), 409
        try:
            if target.is_symlink():
                return jsonify({"error": "源路径不安全，已拒绝删除"}), 409
            if it.get("kind") == "dir" and multiple_video_files(target):
                return jsonify({"error": "目录中还有其他视频，已拒绝整目录删除"}), 409
            recorded = PROCESSED.get(name, {}).get("source_snapshot")
            if not source_matches_snapshot(target, recorded):
                return jsonify({"error": "缺少源核对记录或源内容已经改变，已拒绝删除；旧任务请手动核对后清理"}), 409
            with lock_released():
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        ITEMS.pop(name, None)
        SIZE_HISTORY.pop(name, None)
        PROCESSED.pop(name, None)
        save_state()
    return jsonify({"ok": True})


@app.post("/api/discard_source")
@media_mutation
def api_discard_source():
    """删除一个不打算转换的源（花絮盘、多盘里不要的那张）。页面确认后才会调用。

    只针对还没处理的条目；已入库的条目走 delete_source，那边要先核对成品。
    下载中、排队、处理中、正在读盘的都拒绝，避免删掉正在被读写的东西。
    """
    name = (request.get_json(silent=True) or {}).get("name", "")
    with LOCK:
        it = ITEMS.get(name)
        if not it:
            return jsonify({"error": "该条目不存在"}), 404
        if it["status"] not in DISCARDABLE or it.get("discarding"):
            return jsonify({"error": "下载中、排队、处理中或已入库的条目不能在这里删除"}), 409
        if it.get("preview_status") in ("queued", "analyzing"):
            return jsonify({"error": "正在读取这张盘，读完后再删除"}), 409
        try:
            target = item_source_root(it)
        except ValueError:
            return jsonify({"error": "源路径不安全，已拒绝删除"}), 409
        if target.is_symlink():
            return jsonify({"error": "源路径不安全，已拒绝删除"}), 409
        it["discarding"] = True           # 删除期间不让它被排进队列
        try:
            with lock_released():
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                # 多盘目录里最后一张也删掉后，顺手收掉空的外层目录（非空则保留）
                parent = target.parent
                if parent.resolve() != INBOX.resolve() and parent.resolve().is_relative_to(INBOX.resolve()):
                    for junk in (".DS_Store", "Thumbs.db"):
                        if (parent / junk).is_file() and len(list(parent.iterdir())) == 1:
                            (parent / junk).unlink()
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
        except Exception as e:
            it.pop("discarding", None)
            return jsonify({"error": str(e)}), 500
        ITEMS.pop(name, None)
        SIZE_HISTORY.pop(name, None)
        save_state()
    return jsonify({"ok": True})


@app.post("/api/retry")
@media_mutation
def api_retry():
    """把已完成任务安全地放回待处理；旧成品保留，新成品会用 .vN 命名。"""
    name = (request.get_json(silent=True) or {}).get("name", "")
    with LOCK:
        it = ITEMS.get(name)
        if not it or it["status"] != "done" or it.get("kind") == LIBRARY_KIND:
            return jsonify({"error": "只有已完成任务可以重新处理"}), 409
        source = Path(it.get("root") or (INBOX / name))
        if not source.exists():
            return jsonify({"error": "源文件已经不存在，无法重新处理"}), 409
        PROCESSED.pop(name, None)
        it["status"] = "ready"
        it["error"] = None
        save_state()
    return jsonify({"ok": True, "message": "已恢复为待处理；旧成品不会被覆盖"})


def rename_problem(title):
    """新片名能不能用；能用返回 None。只允许普通文件名，不能带路径。"""
    if not title or len(title) > 180:
        return "片名不能为空，也不能太长"
    if title in (".", "..") or title.startswith(".") or re.search(r'[\\/:*?"<>|\x00-\x1f]', title):
        return "片名里不能有 / \\ : * ? \" < > | 这些符号，也不能以点开头"
    return None


@app.post("/api/rename_output")
@media_mutation
def api_rename_output():
    """给已入库的影片改名：影片文件、同名外挂字幕和面板记录一起改，Jellyfin 才能按新名字认出它。

    只在影片文件夹内原地改名，不移动、不覆盖已有文件。
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))
    title = re.sub(r"\s+", " ", str(data.get("title", ""))).strip()
    with LOCK:
        it = ITEMS.get(name)
        if not it or it["status"] != "done":
            return jsonify({"error": "只有已入库的影片可以改名"}), 409
        output = tracked_movie_output(name)
        if output is None:
            return jsonify({"error": "影片文件夹里找不到这部片"}), 409
        problem = rename_problem(title)
        if problem:
            return jsonify({"error": problem}), 400
        if title == output.stem:
            return jsonify({"ok": True, "message": "片名没有变化"})
        # 跟着一起改的外挂文件：片名.xxx.srt / .ass / .sup
        prefix = output.stem + "."
        companions = [f for f in output.parent.iterdir()
                      if f.is_file() and f != output and f.name.startswith(prefix) and f.suffix.lower() in SIDECAR_EXT]
        moves = [(output, output.with_name(title + output.suffix))]
        moves += [(f, f.with_name(title + f.name[len(output.stem):])) for f in companions]
        if any(target.exists() for _source, target in moves):
            return jsonify({"error": "影片文件夹里已经有叫这个名字的文件了"}), 409
        done = []
        try:
            for source, target in moves:
                source.rename(target)
                done.append((source, target))
        except OSError as exc:
            for source, target in reversed(done):          # 改到一半出错：改回去，不留半截
                try:
                    target.rename(source)
                except OSError:
                    pass
            return jsonify({"error": f"改名失败：{exc}"}), 500
        new_names = {str(source): target.name for source, target in moves}
        def moved(old):
            """记录里的旧路径换成新名字；目录写法保持记录里原来的样子。"""
            old = Path(old)
            key = str(old.parent.resolve() / old.name)
            return str(old.with_name(new_names[key])) if key in new_names else str(old)
        record = PROCESSED.get(name) or {}
        if it.get("kind") == LIBRARY_KIND:
            PROCESSED.pop(name, None)                      # 影片库条目按文件名登记，下一轮扫描会按新名字重新出现
            ITEMS.pop(name, None)
        else:
            record["output"] = moved(record.get("output") or output)
            record["sidecars"] = [moved(x) for x in record.get("sidecars") or []]
            if record.get("subtitle_sidecar"):
                record["subtitle_sidecar"] = moved(record["subtitle_sidecar"])
            PROCESSED[name] = record
        OUTPUT_INFO.pop(str(output), None)
        old_timing, new_timing = subtitle_timing_path(output), subtitle_timing_path(moves[0][1])
        try:
            if old_timing.is_file():
                old_timing.replace(new_timing)
        except OSError:
            pass
        save_state()
    return jsonify({"ok": True, "message": f"已改名为 {title}{output.suffix}，Jellyfin 稍后会重新识别"})


@app.post("/api/rebuild")
@media_mutation
def api_rebuild():
    """删除当前记录的 Movies 成品并恢复为待处理；原盘绝不删除。

    这是用户明确选择的“废弃当前成品重新生成”。只允许删除 PROCESSED 中记录、
    且真实位于 Movies 目录内的单个文件，避免名称拼接或过期记录误删其他内容。
    """
    name = (request.get_json(silent=True) or {}).get("name", "")
    with LOCK:
        it = ITEMS.get(name)
        if not it or it["status"] != "done" or it.get("kind") == LIBRARY_KIND:
            return jsonify({"error": "只有已完成任务可以删除成品后重做"}), 409
        source = Path(it.get("root") or (INBOX / name))
        if not source.exists():
            return jsonify({"error": "原盘已经不存在，不能删除唯一成品"}), 409
        processed = PROCESSED.get(name, {})
        raw_output = processed.get("output", "")
        if not raw_output:
            return jsonify({"error": "没有找到该任务记录的成品"}), 409
        try:
            output = Path(raw_output).resolve(strict=True)
            movies_root = MOVIES.resolve(strict=True)
        except (OSError, RuntimeError):
            return jsonify({"error": "当前成品不存在，已拒绝删除"}), 409
        if not output.is_file() or not output.is_relative_to(movies_root):
            return jsonify({"error": "成品路径不在 Movies 内，已拒绝删除"}), 409
        sidecar_paths = []
        raw_sidecar = processed.get("subtitle_sidecar", "")
        if raw_sidecar:
            sidecar_paths.append(Path(raw_sidecar))
        sidecar_paths.append(subtitle_sidecar_path(output))
        recorded_sups = {str(Path(path)) for path in processed.get("sidecars") or []}
        sidecar_paths += [Path(path) for path in recorded_sups]
        try:
            reclaimed = output.stat().st_size
            with lock_released():
                output.unlink()
            for sidecar in dict.fromkeys(sidecar_paths):
                try:
                    resolved_sidecar = sidecar.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                # 只删本工具生成的：校轴出的 .zh-CN.srt，以及完成记录里登记过、
                # 且以成品文件名开头的外挂 .sup
                ours = resolved_sidecar.name.endswith(".zh-CN.srt") or (
                    str(sidecar) in recorded_sups and resolved_sidecar.suffix == ".sup"
                    and resolved_sidecar.name.startswith(output.stem + "."))
                if (resolved_sidecar.is_file()
                        and resolved_sidecar.is_relative_to(movies_root) and ours):
                    try:
                        reclaimed += resolved_sidecar.stat().st_size
                        resolved_sidecar.unlink()
                    except OSError:
                        pass
        except OSError as e:
            return jsonify({"error": f"删除成品失败: {e}"}), 500
        PROCESSED.pop(name, None)
        it["status"] = "ready"
        it["error"] = None
        save_state()
    return jsonify({
        "ok": True,
        "deleted": output.name,
        "reclaimed": reclaimed,
        "reclaimed_h": human(reclaimed),
        "message": f"已删除当前成品并恢复为待处理，回收 {human(reclaimed)}",
    })


@app.post("/api/upgrade")
def api_upgrade():
    """上传 app.py + Dockerfile，空闲时构建并安全切换当前容器。"""
    remote = getattr(request, "remote_addr", "") or ""
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return jsonify({"error": "无法确认请求来自本机局域网"}), 403
    if not (address.is_private or address.is_loopback):
        return jsonify({"error": "升级接口仅允许在局域网内使用"}), 403
    app_file = request.files.get("app_py")
    dockerfile = request.files.get("dockerfile")   # 可选；不传则用 app.py 内嵌的那份
    if not app_file:
        return jsonify({"error": "请选择新版 app.py"}), 400
    app_bytes = app_file.read(2 * 1024 * 1024 + 1)
    if dockerfile:
        dockerfile_bytes = dockerfile.read(128 * 1024 + 1)
    else:
        dockerfile_bytes = extract_embedded_dockerfile(app_bytes)
        if not dockerfile_bytes:
            return jsonify({
                "error": "这个 app.py 里没有内嵌的 Dockerfile（旧版本或不是本项目的文件），无法只凭它升级"
            }), 400
    try:
        validate_upgrade_files(app_bytes, dockerfile_bytes)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with LOCK:
        if CURRENT_JOB or JOB_QUEUE or DISC_PREVIEW_ACTIVE or ACTIVE_OPERATIONS or pipeline_block_reason():
            return jsonify({"error": "还有任务正在运行或排队（含预读和字幕操作），请等待全部完成后升级"}), 409
        status = read_upgrade_status().get("status")
        if status in {"accepted", "building", "ready", "switching"}:
            return jsonify({"error": "已经有一次升级正在进行"}), 409
        upgrade_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        write_upgrade_status("accepted", "升级文件校验通过，等待开始构建",
                             upgrade_id=upgrade_id)
        threading.Thread(
            target=build_and_schedule_upgrade,
            args=(app_bytes, dockerfile_bytes, upgrade_id),
            daemon=True,
        ).start()
    return jsonify({
        "ok": True,
        "upgrade_id": upgrade_id,
        "message": "文件校验通过，已开始后台构建；页面会自动恢复连接",
    }), 202


@app.post("/api/subtitle_candidate")
@media_mutation
def api_subtitle_candidate():
    """为某个原盘保存一份人工翻译的中文 SRT，供下次处理时自动校轴。"""
    name = request.form.get("name", "")
    subtitle = request.files.get("subtitle")
    with LOCK:
        it = ITEMS.get(name)
        if not it:
            return jsonify({"error": "没有找到对应的媒体任务"}), 404
        if it["status"] in {"processing", "queued"}:
            return jsonify({"error": "任务正在运行或排队，暂时不能替换字幕"}), 409
    if not subtitle:
        return jsonify({"error": "请选择一个 SRT 字幕文件"}), 400
    data = subtitle.read(4 * 1024 * 1024 + 1)
    try:
        result = store_candidate_for_item(
            name, subtitle.filename or "中文字幕.srt", data,
            confirm_audio=request.form.get("confirm_audio") == "1",
            manual_offset_seconds=request.form.get("manual_offset_seconds"),
            manual_first_start_ms=request.form.get("manual_first_start_ms"),
            auto=request.form.get("auto") == "1",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    cue_count = result["cue_count"]
    if result.get("confirmation_required"):
        message = (
            result.get("automatic_error") or "请手动填第一句出现的时间"
        )
    elif result.get("applied") and (refresh_jellyfin_for(name) or True):
        method = (
            "手动首句校准"
            if str(result.get("alignment_method", "")).startswith("manual")
            else "影片自带字幕的时间点"
        )
        message = (
            f"已按{method}生成外置中文字幕 {result['sidecar']}；"
            "无需重新转换视频"
        )
    else:
        message = f"已保存 {cue_count} 个中文字幕段；首次处理时会按盘内日文时间轴校准"
    return jsonify({
        "ok": True,
        "cue_count": cue_count,
        **result,
        "message": message,
    })


@app.get("/api/jellyfin_subtitles")
def api_jellyfin_subtitles():
    """列出 Jellyfin 最近下载的文本字幕，供用户明确选择。"""
    name = request.args.get("name", "")
    with LOCK:
        it = ITEMS.get(name)
        if not it:
            return jsonify({"error": "没有找到对应的媒体任务"}), 404
        if it["status"] in {"processing", "queued"}:
            return jsonify({"error": "任务正在运行或排队，暂时不能替换字幕"}), 409
    try:
        options = jellyfin_subtitle_options()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({
        "ok": True,
        "subtitles": [
            {key: item[key] for key in (
                "id", "filename", "size_h", "modified", "source"
            )}
            for item in options
        ],
    })


@app.post("/api/subtitle_candidate/jellyfin")
@media_mutation
def api_jellyfin_subtitle_candidate():
    """把用户选定的 Jellyfin 字幕复制到处理台持久字幕区。"""
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "")
    subtitle_id = payload.get("subtitle_id", "")
    confirm_audio = payload.get("confirm_audio") is True
    manual_offset_seconds = payload.get("manual_offset_seconds")
    manual_first_start_ms = payload.get("manual_first_start_ms")
    with LOCK:
        it = ITEMS.get(name)
        if not it:
            return jsonify({"error": "没有找到对应的媒体任务"}), 404
        if it["status"] in {"processing", "queued"}:
            return jsonify({"error": "任务正在运行或排队，暂时不能替换字幕"}), 409
    if not subtitle_id:
        return jsonify({"error": "请选择一份字幕"}), 400
    target = jellyfin_path = None
    try:
        if str(subtitle_id).startswith(LOCAL_SUBTITLE_PREFIX):
            # 影片旁边的字幕：读它、校准后写回它自己，所以可以一遍遍校准
            output = tracked_movie_output(name)
            beside = {f.name: f for f in local_sidecars(output)} if output else {}
            target = beside.get(str(subtitle_id)[len(LOCAL_SUBTITLE_PREFIX):])
            if target is None or target.suffix.lower() != ".srt":
                return jsonify({"error": "这份字幕已经不在了，请重新打开窗口"}), 409
            filename, data = target.name, target.read_bytes()
        else:
            filename, data = jellyfin_subtitle_bytes(subtitle_id)
            with LOCK:
                jellyfin_path = JELLYFIN_SUBTITLE_INDEX.get(subtitle_id)
        result = store_candidate_for_item(
            name, filename, data, confirm_audio=confirm_audio,
            manual_offset_seconds=manual_offset_seconds,
            manual_first_start_ms=manual_first_start_ms,
            auto=payload.get("auto") is True, target=target,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (RuntimeError, OSError) as exc:
        return jsonify({"error": str(exc)}), 503
    if result.get("applied") and jellyfin_path:
        # 校准好的那份已经放在影片旁边了：把 Jellyfin 里没校准的原件删掉，免得列表里有两份、电视上选错
        remove_jellyfin_subtitle(name, jellyfin_path)
    cue_count = result["cue_count"]
    if result.get("confirmation_required"):
        message = (
            result.get("automatic_error") or "请手动填第一句出现的时间"
        )
    elif result.get("applied") and (refresh_jellyfin_for(name) or True):
        method = (
            "手动首句校准"
            if str(result.get("alignment_method", "")).startswith("manual")
            else "影片自带字幕的时间点"
        )
        message = f"已按{method}校准：{result['sidecar']}"
    else:
        message = (
            f"已从 Jellyfin 保存 {cue_count} 个中文字幕段；"
            "首次处理时会按盘内日文时间轴校准"
        )
    return jsonify({
        "ok": True,
        "cue_count": cue_count,
        "filename": filename,
        **result,
        "message": message,
    })


@app.post("/api/subtitle_candidate/remove")
@media_mutation
def api_remove_subtitle_candidate():
    """清除误选候选和处理台生成的外置字幕；视频与 OCR 锚点保留。"""
    name = (request.get_json(silent=True) or {}).get("name", "")
    with LOCK:
        it = ITEMS.get(name)
        if not it:
            return jsonify({"error": "没有找到对应的媒体任务"}), 404
        if it["status"] in {"processing", "queued"}:
            return jsonify({"error": "任务正在运行或排队，暂时不能移除字幕"}), 409
    try:
        removed = remove_candidate_and_sidecar(name)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "removed": removed,
        "message": "已移除所选中文字幕；视频文件没有改动",
    })


@app.post("/api/subtitle_ai")
@media_mutation
def api_subtitle_ai():
    """保存字幕缺段补译所用的 OpenAI-compatible 接口；永不向前端返回密钥。"""
    remote = getattr(request, "remote_addr", "") or ""
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return jsonify({"error": "无法确认请求来自本机局域网"}), 403
    if not (address.is_private or address.is_loopback):
        return jsonify({"error": "字幕 AI 设置仅允许在局域网内修改"}), 403
    data = request.get_json(silent=True) or {}
    try:
        saved = save_subtitle_ai_config(
            str(data.get("endpoint", "")),
            str(data.get("model", "")),
            str(data.get("api_key", "")),
        )
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "ok": True,
        "endpoint": saved["endpoint"],
        "model": saved["model"],
        "message": "字幕 AI 已配置；只会补译成品中文字幕没有覆盖的盘内日文段落",
    })


BROWSER_CONTAINER = "tvtranscode-browser"
BROWSER_SNIPPET = (
    "import json,os,sys\n"
    "try:\n"
    "    print(json.dumps({'names': sorted(e.name for e in os.scandir('/host' + sys.argv[1]) if e.is_dir(follow_symlinks=False))}))\n"
    "except OSError as exc:\n"
    "    print(json.dumps({'error': exc.strerror or str(exc)}))\n"
)


def host_folder_names(path):
    """列出 NAS 上某个文件夹里的子文件夹名。任何位置都能看，不限于面板已经挂上的那几个。

    面板容器自己只看得到挂进来的文件夹，所以临时起一个只读的小容器（把 NAS 的根目录只读挂进去）来看；
    它 15 分钟后自己退出。没接 Docker 时退回去看面板自己能看到的文件夹。
    """
    try:
        import docker
        client = docker.from_env()
        me = current_panel_container(client)
    except Exception:
        try:
            return sorted(entry.name for entry in os.scandir(path) if entry.is_dir(follow_symlinks=False))
        except OSError as exc:
            raise FileNotFoundError(exc.strerror or str(exc)) from exc
    try:
        try:
            helper = client.containers.get(BROWSER_CONTAINER)
            if helper.status != "running":
                helper.remove(force=True)
                helper = None
        except Exception:
            helper = None
        if helper is None:
            helper = client.containers.run(
                me.image.id, ["sleep", "900"], name=BROWSER_CONTAINER, detach=True, auto_remove=True,
                network_mode="none", volumes={"/": {"bind": "/host", "mode": "ro"}})
        result = helper.exec_run(["python", "-c", BROWSER_SNIPPET, path])
        data = json.loads(result.output.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"读不了 NAS 上的文件夹：{exc}") from exc
    finally:
        client.close()
    if "error" in data:
        raise FileNotFoundError(data["error"])
    return data["names"]


def pickable_folders(path):
    """某个位置下面可以选的文件夹：不列隐藏目录、回收站、工作目录，最顶层不列系统目录。"""
    top = path == "/"
    return [name for name in host_folder_names(path)
            if not name.startswith((".", "#", "@")) and name != WORK_NAME
            and not (top and (name in SYSTEM_DIRS or name.endswith(".defaults")))]


@app.get("/api/folder_list")
def api_folder_list():
    """选文件夹用：列出 NAS 上某一层的子文件夹。只读。"""
    parts = [part for part in str(request.args.get("path", "")).strip().split("/") if part]
    path = "/" + "/".join(parts)
    if parts and folder_name_problem(path):
        return jsonify({"error": "这个位置不能选"}), 400
    try:
        names = pickable_folders(path)
    except FileNotFoundError:
        return jsonify({"error": "文件夹不存在"}), 404
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"path": path, "parent": ("/" + "/".join(parts[:-1])) if parts else None, "folders": names[:500]})


PROJECT_REPO = env_text("PROJECT_REPO", "terminaterfxy/tv-transcode")     # 到哪个 GitHub 仓库查新版本


def version_key(text):
    """把 0.10.0、v0.10.0 这样的版本号变成可以比大小的数字组；不是版本号返回 None。"""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


@app.get("/api/latest_version")
def api_latest_version():
    """查一下项目仓库里最新的版本号。只在用户打开升级窗口时查，不会自己定时联网。"""
    try:
        req = urlrequest.Request(f"https://api.github.com/repos/{PROJECT_REPO}/tags?per_page=100",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "tv-transcode"})
        with urlrequest.urlopen(req, timeout=8) as response:
            tags = json.loads(response.read() or b"[]")
    except Exception as exc:
        return jsonify({"error": f"查不到新版本：{str(exc)[:120]}"}), 502
    versions = sorted(key for key in (version_key(tag.get("name")) for tag in tags if isinstance(tag, dict)) if key)
    if not versions:
        return jsonify({"error": "仓库里还没有发布版本"}), 404
    latest = ".".join(str(part) for part in versions[-1])
    current = version_key(APP_RELEASE)
    return jsonify({"latest": latest, "current": APP_RELEASE, "newer": bool(current and versions[-1] > current),
                    "url": f"https://github.com/{PROJECT_REPO}/releases"})


def tool_version(command):
    """跑一下工具的版本命令，返回第一行；跑不起来返回 None。"""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=10)
        lines = (done.stdout or done.stderr or "").strip().splitlines()
        return lines[0][:120] if done.returncode == 0 and lines else None
    except (OSError, subprocess.SubprocessError):
        return None


@app.get("/api/selfcheck")
def api_selfcheck():
    """运行环境检查：面板依赖的工具和服务在不在、能不能用。只读。"""
    checks = []

    def add(name, state, detail):
        checks.append({"name": name, "state": state, "detail": detail})      # state: ok / missing（必需的缺了）/ optional（可选的没有）

    for name, command, need in (
            ("FFmpeg", ["ffmpeg", "-version"], "转换和读取影片信息"),
            ("MKVToolNix", ["mkvmerge", "--version"], "封装 MKV"),
            ("dovi_tool", [DOVI_TOOL, "--version"], "转换双层杜比视界")):
        version = tool_version(command)
        where = f" · {command[0]}" if "/" in command[0] else ""
        add(name, "ok" if version else "missing", (version + where) if version else f"没有找到，{need}会失败")
    docker_ok = False
    try:
        import docker
        client = docker.from_env()
        try:
            me = current_panel_container(client)
            docker_ok = True
            add("Docker", "ok", f"面板容器：{me.name}")
            try:
                makemkv = client.containers.get(MAKEMKV_CONTAINER)
                add("读盘工具（MakeMKV）", "ok" if makemkv.status == "running" else "optional",
                    f"容器 {MAKEMKV_CONTAINER}：{makemkv.status}")
            except Exception:
                add("读盘工具（MakeMKV）", "optional", f"没有名为 {MAKEMKV_CONTAINER} 的容器；部分蓝光原盘读不了")
        finally:
            client.close()
    except Exception as exc:
        add("Docker", "optional", f"连不上 Docker：{str(exc)[:120]}。升级、选择未挂载的文件夹、读盘工具不可用")
    try:
        base = jellyfin_base_url(getattr(request, "host", ""))
        with urlrequest.urlopen(base + "/System/Info/Public", timeout=5) as response:
            info = json.loads(response.read() or b"{}")
        add("Jellyfin", "ok", f"{base} · 版本 {info.get('Version', '未知')}")
    except Exception as exc:
        add("Jellyfin", "optional", f"连不上：{str(exc)[:120]}。在线字幕不可用；地址不对时设置环境变量 JELLYFIN_URL")
    for label, folder in (("下载文件夹", INBOX), ("影片文件夹", MOVIES)):
        usable = folder.is_dir() and os.access(str(folder), os.W_OK)
        add(label, "ok" if usable else "missing", host_path(folder) + ("" if usable else " · 不存在或不可写"))
    ids = owner_ids()
    add("成品文件归属", "ok", f"用户 {ids[0]}，用户组 {ids[1]}（PUID / PGID）" if ids else "root（未设置 PUID / PGID）")
    return jsonify({"checks": checks, "release": APP_RELEASE, "app_version": APP_VERSION, "docker": docker_ok})


def current_folders():
    """现在用的两个文件夹，按 NAS 上的真实位置给出。"""
    return {"inbox": host_path(INBOX), "library": host_path(MOVIES)}


def folder_rows():
    """“文件夹”这一组设置，和其他规则用同一种行来展示。"""
    current = current_folders()
    rows = []
    for key, label, note in (
            ("inbox", "下载文件夹", "扫描此文件夹中的影片和蓝光原盘。"),
            ("library", "影片文件夹", "成品写入此文件夹。Jellyfin 媒体库须指向同一位置。")):
        rows.append({"id": "folder." + key, "group": "文件夹", "label": label, "action": current[key],
                     "default": host_path(SHARE / DEFAULT_FOLDERS[key]), "note": note, "note_fixed": True, "options": []})
    return rows


def wanted_folders_problem(inbox, library):
    """用户新选的两个位置（NAS 上的路径）能不能用；能用返回 None。"""
    for key, value in (("下载文件夹", inbox), ("影片文件夹", library)):
        if not str(value).startswith("/"):
            return f"{key}的位置无效"
        problem = folder_name_problem(value)
        if problem:
            return key + problem
        if len(Path(value).parts) < 3 and panel_mounts():
            return f"{key}不能是整块硬盘，请选里面的一个文件夹"
        try:
            host_folder_names(value)
        except FileNotFoundError:
            return f"{key}不存在：{value}"
        except RuntimeError as exc:
            return str(exc)
    if folders_overlap(inbox, library):
        return "下载文件夹和影片文件夹不能是同一个，也不能一个套着另一个"
    return None


def restart_service():
    """原地重启面板（换文件夹后要重新读一遍）。测试里会被替换掉。"""
    global FOLDER_RESTARTING
    FOLDER_RESTARTING = True
    def later():
        global FOLDER_RESTARTING
        time.sleep(1.2)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        finally:
            FOLDER_RESTARTING = False
    threading.Thread(target=later, daemon=True).start()


def apply_folders(inbox, library, rules, notes=None):
    """换到面板已经看得到的两个文件夹（面板里的路径）：规则和密钥带过去，记下设置，原地重启。"""
    new_work = folder_path(inbox) / WORK_NAME
    if new_work != WORK and (WORK / JELLYFIN_KEY_FILE).is_file() and not (new_work / JELLYFIN_KEY_FILE).exists():
        new_work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WORK / JELLYFIN_KEY_FILE, new_work / JELLYFIN_KEY_FILE)
    save_rules(rules, new_work, saved_table_notes() if notes is None else notes)
    write_settings_json(SHARE / FOLDERS_FILE_NAME, {"inbox": inbox, "library": library})
    restart_service()


def remount_and_apply_folders(inbox, library, rules, notes=None):
    """选的文件夹（NAS 上的路径）面板还看不到：重建面板容器把它挂进来，镜像不变；失败自动恢复原样。

    实际动手的是升级用的那个一次性助手：它在面板容器之外，才能把面板容器停掉重建。
    """
    global FOLDER_RESTARTING
    import docker
    save_rules(rules, notes=saved_table_notes() if notes is None else notes)     # 先存在现在的工作目录，助手再带过去
    binds = [host for host in (inbox, library) if panel_path(host) is None]
    settings = {"inbox": panel_path(inbox) or inbox, "library": panel_path(library) or library}
    upgrade_id = time.strftime("%Y%m%d-%H%M%S") + "-folders"
    client = docker.from_env()
    try:
        me = current_panel_container(client)
        me.reload()
        share_mount = next((mount for mount in me.attrs.get("Mounts", [])
                            if mount.get("Destination") == "/share" and mount.get("Type") == "bind"), None)
        if not share_mount:
            raise RuntimeError("当前容器没有 /share 绑定，无法切换")
        status_volumes, status_path = upgrade_status_binding()
        if not status_volumes:
            raise RuntimeError("认不出面板容器的挂载，无法切换")
        write_upgrade_status("ready", "正在把新文件夹接入面板", upgrade_id=upgrade_id)
        client.containers.run(
            me.image.id, command=["python", "-c", UPGRADE_HELPER], name=f"mediapanel-upgrader-{upgrade_id}",
            detach=True, auto_remove=True, network_mode="host",
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                share_mount["Source"]: {"bind": "/share", "mode": "rw"},
                inbox: {"bind": "/newinbox", "mode": "rw"},
                **status_volumes,
            },
            environment={
                "UPGRADE_STATUS_PATH": status_path, "UPGRADE_OLD_ID": me.id, "UPGRADE_NEW_IMAGE": me.image.id,
                "UPGRADE_ID": upgrade_id, "UPGRADE_EXTRA_BINDS": json.dumps(binds),
                "UPGRADE_FOLDERS": json.dumps(settings, ensure_ascii=False),
                "UPGRADE_FOLDERS_PATH": "/share/" + FOLDERS_FILE_NAME,
                "UPGRADE_DONE_MESSAGE": "媒体目录已切换",
                "UPGRADE_PORT": str(PANEL_PORT),
            },
        )
        FOLDER_RESTARTING = True
    except Exception as exc:
        write_upgrade_status("failed", f"切换媒体目录没有开始，当前设置未变: {exc}", upgrade_id=upgrade_id)
        raise OSError(f"切换媒体目录失败：{exc}") from exc
    finally:
        client.close()


def selection_example():
    audio = [
        {"lang": "jpn", "codec": "aac", "title": "日语 AAC"},
        {"lang": "eng", "codec": "ac3", "title": "英语 AC-3", "default": True},
        {"lang": "eng", "codec": "truehd", "title": "英语 TrueHD"},
    ]
    subs = [{"lang": "chi", "codec": "subrip", "title": "中文 SRT"},
            {"lang": "eng", "codec": "subrip", "title": "英语 SRT"}]
    media = {"audio": audio, "subs": subs}
    audio_outputs = {mode: audio[default_selection(media, mode, "none")["audio"][0]]["title"]
                     for mode in RULE_OPTIONS["pick.audio"]}
    sub_outputs = {}
    for lang in RULE_OPTIONS["pick.subs"]:
        chosen = default_selection(media, "first", lang)["subs"]
        sub_outputs[lang] = subs[chosen[0]]["title"] if chosen else "不选字幕"
    return {"audio_source": "、".join(t["title"] + ("（默认）" if t.get("default") else "") for t in audio),
            "subs_source": "、".join(t["title"] for t in subs), "audio": audio_outputs, "subs": sub_outputs}


def settings_view():
    view = rules_view()
    view["selection_example"] = selection_example()
    view["rows"] = view["rows"] + folder_rows()
    view["groups"] = view["groups"] + ["文件夹"]
    return view


@app.get("/api/rules")
def api_rules():
    return jsonify(settings_view())


@app.post("/api/rules")
def api_rules_save():
    """保存转换规则。转换进行中不让改，免得一部片前后按两套规则处理。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请提交有效的设置"}), 400
    with LOCK:
        if CURRENT_JOB or JOB_QUEUE:
            return jsonify({"error": "有影片正在转换或排队，任务完成后可保存设置"}), 409
        if FOLDER_RESTARTING:
            return jsonify({"error": "面板正在切换媒体目录"}), 409
        resetting = data.get("reset") is True
        changes = {} if resetting else data.get("rules")
        if not isinstance(changes, dict):
            return jsonify({"error": "请提交有效的规则"}), 400
        current = current_folders()
        wanted = dict(current)
        view = settings_view()
        merged = {r["id"]: {"action": r["action"], "note": r["note"]} for r in view["rows"]
                  if not r.get("note_fixed") and not r.get("note_only")}
        notes = saved_table_notes()
        try:
            for key, entry in changes.items():
                if not isinstance(entry, dict) or not entry:
                    raise ValueError("选项格式无效")
                if key in TABLE_NOTE_IDS:                      # 表格里某一行的备注
                    if set(entry) - {"note"} or not isinstance(entry.get("note"), str):
                        raise ValueError("请输入文本备注")
                    notes[key] = entry["note"][:300]
                    continue
                if key.startswith("folder."):
                    name = key[len("folder."):]
                    if name not in wanted or set(entry) - {"action", "note"} or not isinstance(entry.get("action"), str) or not entry["action"]:
                        raise ValueError("目录选项无效")
                    wanted[name] = entry["action"]
                else:
                    if key not in merged or set(entry) - {"action", "note"}:
                        raise ValueError("无法识别这条规则")
                    if "action" in entry and (not isinstance(entry["action"], str) or entry["action"] not in RULE_OPTIONS[key]):
                        raise ValueError("处理方式无效")
                    if "note" in entry and not isinstance(entry["note"], str):
                        raise ValueError("请输入文本备注")
                    merged[key].update(entry)
            moving = wanted != current
            if moving:
                problem = wanted_folders_problem(wanted["inbox"], wanted["library"])
                if problem:
                    raise ValueError(problem)
                if processing_block_reason() or ACTIVE_OPERATIONS or DISC_PREVIEW_ACTIVE:
                    return jsonify({"error": "有任务正在使用媒体目录，请稍后再保存"}), 409
                inside = {key: panel_path(value) for key, value in wanted.items()}
                if all(inside.values()):                     # 面板已经看得到：记下来，原地重启
                    problem = folder_choice_problem(inside["inbox"], inside["library"])
                    if problem:
                        raise ValueError(problem)
                    apply_folders(inside["inbox"], inside["library"], merged, notes)
                else:                                        # 还看不到：重建容器把它挂进来
                    remount_and_apply_folders(wanted["inbox"], wanted["library"], merged, notes)
                for row in view["rows"]:
                    if row.get("note_only"):
                        if row["id"] in notes:
                            row.update(note=notes[row["id"]], has_note=True)
                    elif row.get("note_fixed"):
                        row["action"] = wanted[row["id"][len("folder."):]]
                    else:
                        row.update(merged[row["id"]])
                        row["note"] = row["note"][:300]
                view["customized"] = any(r["action"] != r["default"] for r in view["rows"]
                                         if not r.get("note_fixed") and not r.get("note_only"))
            else:
                save_rules(None if resetting else merged, notes=notes)
                for item in ITEMS.values():
                    if item["status"] in ("ready", "error") and item.get("probe"):
                        item["plan"], item["clean"] = build_plan(item["kind"], item["probe"], item["payload"])
                        item["optional"] = processing_optional(item["kind"], item["probe"], item["payload"])
                view = settings_view()
        except (ValueError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "restarting": moving, **view})


# ---------- 字幕时间点：用影片自带字幕“每句什么时候出现”来给下载的字幕对时间 ----------
# 只用时间点，不需要认出字幕写了什么，所以图片字幕（PGS）也能当参照，不用 OCR。
# 转换时顺手存一份到工作目录，源文件删掉以后照样能校准。
SUBTITLE_TIMING_DIR = "subtitle_timing"
GRAPHIC_SHOW_BYTES = 200          # 图片字幕里，比这小的数据包只是“清屏”，不是一句新字幕


def subtitle_timing_path(output):
    digest = hashlib.sha256(Path(output).name.encode("utf-8")).hexdigest()[:24]
    return WORK / SUBTITLE_TIMING_DIR / f"{digest}.json"


def _graphic_cues(packets):
    """图片字幕的数据包 → 每句的 (开始, 结束) 毫秒。

    一句字幕由时间挨在一起的几个包组成，其中有一个大的（画面）；只有小包的一组是清屏。
    蓝光为了方便快进，会每隔一秒把同一句重复发一遍——同样大小、中间没清屏的算同一句。
    """
    groups = []
    for pts, size in sorted(packets):
        if groups and pts - groups[-1]["last"] <= 150:
            groups[-1]["last"] = pts
            groups[-1]["time"] = max(groups[-1]["time"], pts)
            groups[-1]["big"] = max(groups[-1]["big"], size)
        else:
            groups.append({"time": pts, "last": pts, "big": size})
    cues, showing = [], None
    for group in groups:
        if group["big"] < GRAPHIC_SHOW_BYTES:                 # 清屏
            if showing is not None:
                cues.append([showing["time"], group["time"]])
                showing = None
        elif showing is None or showing["big"] != group["big"]:
            if showing is not None:
                cues.append([showing["time"], group["time"]])
            showing = group
    if showing is not None:
        cues.append([showing["time"], showing["time"] + 3000])
    return [[start, min(end, start + 12000)] for start, end in cues if end > start]


def _subtitle_packets(path, timeout=7200):
    """读一个文件里所有字幕轨的数据包。返回 {"start": 文件起始秒, "duration": 时长秒, "streams": {...}}；读不了返回 None。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "format=start_time,duration:stream=index,id,codec_name:stream_tags=language"
                              ":packet=stream_index,pts_time,duration_time,size",
             "-of", "json", str(path)], capture_output=True, timeout=timeout)
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    streams = {}
    for stream in data.get("streams") or []:
        streams[stream.get("index")] = {
            "key": str(stream.get("id") or stream.get("index")), "codec": str(stream.get("codec_name") or "").lower(),
            "lang": str((stream.get("tags") or {}).get("language") or ""), "rows": []}
    for packet in data.get("packets") or []:
        stream = streams.get(packet.get("stream_index"))
        if stream is None or packet.get("pts_time") in (None, "N/A"):
            continue
        stream["rows"].append((number(packet.get("pts_time")) * 1000, int(number(packet.get("size"))),
                               number(packet.get("duration_time")) * 1000))
    fmt = data.get("format") or {}
    return {"start": number(fmt.get("start_time")), "duration": number(fmt.get("duration")), "streams": streams}


def _timing_tracks(streams):
    """每条字幕轨的数据包 → 每句的 (开始, 结束) 毫秒。少于 5 句的轨不要。"""
    tracks = []
    for stream in streams:
        rows = stream["rows"]
        if stream["codec"] in GRAPHIC_SUB_CODECS:
            points = _graphic_cues([(pts, size) for pts, size, _duration in rows])
        else:
            points = [[pts, pts + (duration if duration > 0 else 2000)] for pts, _size, duration in sorted(rows)]
        if len(points) >= 5:
            tracks.append({"codec": stream["codec"], "lang": stream["lang"],
                           "points": [[int(start), int(end)] for start, end in points]})
    return tracks


def subtitle_time_points(path, timeout=1800):
    """读出一个文件里每条字幕轨的时间点（文件的时间线从 0 开始，比如我们自己导出的字幕或成品）。"""
    data = _subtitle_packets(path, timeout)
    return _timing_tracks(data["streams"].values()) if data else []


def disc_subtitle_time_points(clips, playlist_seconds):
    """原盘正片由几个片段接起来：逐个片段读字幕，再按“前面片段的总时长”接到同一条时间线上。

    NAS 实测过的坑：用 mkvmerge 只取字幕（不带画面）时，下一个片段会紧贴着上一段最后一句字幕接上，
    而不是接在上一段画面结束的位置，后面所有字幕都会早十几秒。所以这里自己按片段时长来接。
    片段总时长和播放列表对不上（比如只用了片段的一部分）时放弃，宁可没有参照也不要错的。
    """
    merged, elapsed = {}, 0.0
    for clip in clips:
        data = _subtitle_packets(clip)
        if not data or data["duration"] <= 0:
            return []
        for stream in data["streams"].values():
            target = merged.setdefault(stream["key"], {"codec": stream["codec"], "lang": stream["lang"], "rows": []})
            base = elapsed - data["start"] * 1000
            target["rows"] += [(pts + base, size, duration) for pts, size, duration in stream["rows"]]
        elapsed += data["duration"] * 1000
    if abs(elapsed / 1000 - playlist_seconds) > 1.5:
        return []
    return _timing_tracks(merged.values())


def save_subtitle_timing(output, files, tracks=None):
    """把这些文件里的字幕时间点存成这部影片的参照。files 是转换时顺手导出的字幕文件。"""
    tracks = list(tracks or [])
    for path in files:
        tracks += subtitle_time_points(path)
    if not tracks:
        return 0
    try:
        write_settings_json(subtitle_timing_path(output), {"movie": Path(output).name, "tracks": tracks})
    except OSError:
        return 0
    return len(tracks)


def load_subtitle_timing(output):
    """这部影片存下的参照时间点；挑句子最多的那条字幕轨。没有返回 None。"""
    try:
        data = json.loads(subtitle_timing_path(output).read_text("utf-8"))
        best = max(data.get("tracks") or [], key=lambda track: len(track.get("points") or []))
        cues = [{"start": float(start), "end": float(end), "text": ""} for start, end in best["points"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return cues if len(cues) >= 5 else None


def timing_anchor_cues(name, output):
    """给这部影片找参照时间点：先用转换时存下的；没有的话，现读影片里装着的字幕（要把影片读一遍，慢）。"""
    saved = load_subtitle_timing(output)
    if saved:
        return saved
    with LOCK:
        probe = (OUTPUT_INFO.get(Path(output)) or {}).get("probe") or {}
    if probe.get("subs"):
        tracks = subtitle_time_points(output)
        if tracks:
            try:
                write_settings_json(subtitle_timing_path(output), {"movie": Path(output).name, "tracks": tracks})
            except OSError:
                pass
            return load_subtitle_timing(output)
    return None


def anchor_subtitle_outputs(jobdir: Path, probe, selected, ain="0", limit=2):
    """转换时顺手把没选中的字幕也导出一两条到临时目录，只为取时间点；用完随临时目录一起删。"""
    args, paths = [], []
    for index, sub in enumerate(probe.get("subs") or []):
        if index in selected or sub.get("forced") or len(paths) >= limit:
            continue
        fmt = sidecar_format(sub.get("codec"))
        if not fmt:
            continue
        ext, encoder, muxer = fmt
        target = jobdir / f"timing-anchor.{index}.{ext}"
        paths.append(target)
        args += ["-map", f"{ain}:s:{index}", "-c:s", encoder, "-f", muxer, str(target)]
    return args, paths


def align_by_timing(job, anchor_cues, candidate_cues, output):
    """按参照时间点给字幕整体对时间（平移，必要时按帧率拉伸）。对不上返回 None。"""
    transform = estimate_subtitle_transform(anchor_cues, candidate_cues)
    if not transform:
        return None, None
    write_srt(output, transform_cues(candidate_cues, transform))
    if not _inspect_srt(output):
        return None, None
    log(job, f"字幕对时间: 偏移 {transform['offset_ms']/1000:+.3f} 秒，速率 {transform['scale']:.6f}，"
             f"吻合度 {transform['confidence']:.0%}")
    return output, transform


# ---------- 一键找中文字幕：让 Jellyfin 自己去搜并下载 ----------
JELLYFIN_PORT = env_number("JELLYFIN_PORT", 8096)
JELLYFIN_KEY_FILE = ".jellyfin.json"
SIMPLIFIED_HINT = re.compile(r"简|chs|zh[-_.]?cn|zh[-_.]?hans|\bsc\b|\bgb\b", re.I)
TRADITIONAL_HINT = re.compile(r"繁|cht|big5|zh[-_.]?tw|zh[-_.]?hant|\btc\b", re.I)


class JellyfinError(RuntimeError):
    def __init__(self, message, needs_key=False):
        super().__init__(message)
        self.needs_key = needs_key


def jellyfin_api_key():
    key = os.environ.get("JELLYFIN_API_KEY", "").strip()
    try:
        saved = json.loads((WORK / JELLYFIN_KEY_FILE).read_text("utf-8"))
        key = str(saved.get("api_key") or key).strip()
    except (OSError, ValueError):
        pass
    return key


def save_jellyfin_api_key(key):
    key = key.strip()
    if not re.fullmatch(r"[0-9A-Za-z]{16,128}", key):
        raise ValueError("这不像 Jellyfin 的 API 密钥（应是一串字母和数字）")
    path = WORK / JELLYFIN_KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": key}), "utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def jellyfin_base_url(request_host=""):
    """Jellyfin 的地址：优先用环境变量；否则就是你打开面板用的那台机器的 8096 端口。"""
    configured = os.environ.get("JELLYFIN_URL", "").strip().rstrip("/")
    if configured:
        return configured
    host = (request_host or "").strip()
    if host.startswith("["):                      # IPv6 字面量
        host = host[:host.index("]") + 1] if "]" in host else host
    else:
        host = host.split(":")[0]
    if not host:
        raise JellyfinError("不知道 Jellyfin 的地址")
    return f"http://{host}:{JELLYFIN_PORT}"


def jellyfin_call(base, key, method, path, query=None, timeout=90):
    url = base + path + ("?" + urlparse.urlencode(query) if query else "")
    req = urlrequest.Request(url, method=method, data=b"" if method == "POST" else None, headers={
        "Authorization": f'MediaBrowser Token="{key}"', "Accept": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urlerror.HTTPError as exc:
        if exc.code in (401, 403):
            raise JellyfinError("Jellyfin 不认这个密钥，请重新填一个", needs_key=True) from exc
        raise JellyfinError(f"Jellyfin 返回了错误（{exc.code}）") from exc
    except (urlerror.URLError, OSError) as exc:
        raise JellyfinError("连不上 Jellyfin，请确认它正在运行") from exc
    try:
        return json.loads(raw.decode("utf-8")) if raw.strip() else None
    except ValueError:
        return None


def release_kind(text):
    """从名字看片源是哪一类：disc（蓝光原盘或其压制）/ web（流媒体）/ None（看不出）。"""
    text = str(text or "").lower()
    if re.search(r"blu-?ray|bdmv|bdrip|bdrom|bd-?remux|remux|uhd|(?<![a-z])bd(?![a-z])", text):
        return "disc"
    if re.search(r"web-?dl|web-?rip|(?<![a-z])web(?![a-z])|amzn|netflix|(?<![a-z])nf(?![a-z])|hdtv|itunes", text):
        return "web"
    return None


def pick_chinese_subtitle(results, source_hint=""):
    """从 Jellyfin 搜到的字幕里挑一份。

    和影片文件完全对得上的优先；其次是片源类型相同的（蓝光配蓝光、流媒体配流媒体——
    NAS 实测同一部片的流媒体版和蓝光版长度不同，字幕时间轴对不上）；再看简体和下载次数。
    """
    wanted = release_kind(source_hint)
    def score(item):
        text = " ".join(str(item.get(k) or "") for k in ("Name", "Comment", "Author"))
        value = 1000 if item.get("IsHashMatch") else 0
        kind = release_kind(item.get("Name"))
        if wanted and kind:
            value += 150 if kind == wanted else -80
        value += 100 if SIMPLIFIED_HINT.search(text) else (20 if TRADITIONAL_HINT.search(text) else 50)
        value += 10 if str(item.get("Format") or "").lower() in ("srt", "ass", "ssa") else 0
        value += min(math.log10((item.get("DownloadCount") or 0) + 1) * 10, 60)
        value -= 60 if (item.get("AiTranslated") or item.get("MachineTranslated")) else 0   # 机翻的排后面
        value -= 300 if item.get("Forced") else 0                                           # 只翻外语片段的不算
        return value
    usable = [item for item in results or [] if item.get("Id")]
    return max(usable, key=score) if usable else None


def jellyfin_find_movie(base, key, output):
    found = jellyfin_call(base, key, "GET", "/Items", {
        "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path,ProviderIds", "Limit": "5000"}) or {}
    for item in found.get("Items") or []:
        if Path(str(item.get("Path") or "")).name == output.name:
            return item
    return None


def jellyfin_download_chinese_subtitle(base, key, output, source_hint=""):
    movie = jellyfin_find_movie(base, key, output)
    if not movie:
        raise JellyfinError("Jellyfin 里还没有这部片，先在 Jellyfin 里刷新一次媒体库再试")
    results = []
    for language in ("chi", "zho"):
        results = jellyfin_call(base, key, "GET",
                                f"/Items/{movie['Id']}/RemoteSearch/Subtitles/{language}") or []
        if results:
            break
    chosen = pick_chinese_subtitle(results, f"{source_hint} {output.name}")
    if not chosen:
        plugins = jellyfin_call(base, key, "GET", "/Plugins") or []
        if not any("subtitle" in str(plugin.get("Name") or "").lower() for plugin in plugins):
            raise JellyfinError("Jellyfin 还没装字幕下载插件：在 Jellyfin 控制台 → 插件 → 目录 里装 Open Subtitles，"
                                "填上账号后重启 Jellyfin 再试")
        ids = movie.get("ProviderIds") or {}
        if not (ids.get("Tmdb") or ids.get("Imdb")):
            shown = str(movie.get("Name") or output.stem)
            raise JellyfinError(f"Jellyfin 没认出这是哪部片（它显示的片名是“{shown}”），所以搜不到字幕。"
                                "在 Jellyfin 里打开这部片，点右上角 ⋮ → 识别，选对影片后再回来点一次")
        raise JellyfinError("未找到中文字幕，可从电脑上传")
    jellyfin_call(base, key, "POST",
                  f"/Items/{movie['Id']}/RemoteSearch/Subtitles/{urlparse.quote(str(chosen['Id']), safe='')}")
    return {"name": str(chosen.get("Name") or "中文字幕"), "exact": bool(chosen.get("IsHashMatch")),
            "candidates": len(results)}


@app.post("/api/subtitle_auto")
def api_subtitle_auto():
    """一键找中文字幕：让 Jellyfin 搜索并下载到它自己的库里，电视上直接能选。"""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", ""))
    with LOCK:
        it = ITEMS.get(name)
        if not it or it["status"] != "done":
            return jsonify({"error": "只有已经入库的影片才能找字幕"}), 409
    output = tracked_movie_output(name)
    if output is None:
        return jsonify({"error": "影片库里找不到这部片的成品"}), 409
    typed = str(data.get("api_key", "")).strip()
    if typed:
        try:
            save_jellyfin_api_key(typed)
        except (ValueError, OSError) as exc:
            return jsonify({"error": str(exc), "needs_key": True}), 400
    key = jellyfin_api_key()
    if not key:
        return jsonify({"error": "第一次用要填一个 Jellyfin 的 API 密钥", "needs_key": True}), 428
    try:
        base = jellyfin_base_url(getattr(request, "host", ""))
        hint = name + (" bdmv" if it.get("kind") in DISC_KINDS else "")
        result = jellyfin_download_chinese_subtitle(base, key, output, hint)
    except JellyfinError as exc:
        return jsonify({"error": str(exc), "needs_key": exc.needs_key}), 502
    tail = "" if result["exact"] else "这份不是专门对着这个版本做的，时间对不上的话，在下面选它来校准。"
    return jsonify({"ok": True, "message": f"已下载：{result['name']}。电视上选中文字幕就能看。{tail}"})



# ---------- 字幕弹窗：现有字幕、网上搜到的字幕，由用户自己选 ----------
SUBTITLE_SETTLE_SECONDS = 20


def subtitle_request_context(data):
    """字幕相关接口的共同开头：确认影片已入库、拿到密钥和 Jellyfin 地址。出错返回 (None, 响应)。"""
    name = str(data.get("name", ""))
    with LOCK:
        it = ITEMS.get(name)
        if not it or it["status"] != "done":
            return None, (jsonify({"error": "影片入库后才能管理字幕"}), 409)
        kind = it.get("kind")
    output = tracked_movie_output(name)
    if output is None:
        return None, (jsonify({"error": "影片文件夹里找不到这部片"}), 409)
    typed = str(data.get("api_key", "")).strip()
    if typed:
        try:
            save_jellyfin_api_key(typed)
        except (ValueError, OSError) as exc:
            return None, (jsonify({"error": str(exc), "needs_key": True}), 400)
    key = jellyfin_api_key()
    if not key:
        return None, (jsonify({"error": "第一次用要填一个 Jellyfin 的 API 密钥", "needs_key": True}), 428)
    try:
        base = jellyfin_base_url(getattr(request, "host", ""))
    except JellyfinError as exc:
        return None, (jsonify({"error": str(exc)}), 502)
    hint = name + (" bdmv" if kind in DISC_KINDS else "")
    return {"name": name, "output": output, "key": key, "base": base, "hint": hint}, None


def jellyfin_movie_or_error(ctx):
    movie = jellyfin_find_movie(ctx["base"], ctx["key"], ctx["output"])
    if not movie:
        raise JellyfinError("Jellyfin 里还没有这部片，先在 Jellyfin 里扫描一次媒体库再试")
    return movie


def jellyfin_subtitle_streams(ctx, movie):
    """这部片在 Jellyfin 里的字幕轨。只属于这一部，不会混进别的片的字幕。"""
    found = jellyfin_call(ctx["base"], ctx["key"], "GET", "/Items", {
        "Ids": movie["Id"], "Fields": "MediaStreams"}) or {}
    items = found.get("Items") or []
    streams = (items[0].get("MediaStreams") or []) if items else []
    return [stream for stream in streams if stream.get("Type") == "Subtitle"]


LOCAL_SUBTITLE_PREFIX = "local:"     # “校准时间”认的编号：影片旁边的字幕用“local:文件名”


def remove_jellyfin_subtitle(name, path):
    """让 Jellyfin 删掉它资料目录里的一份外挂字幕。删不掉不算错（只是列表里多一份）。"""
    try:
        ctx, error = subtitle_request_context({"name": name})
        if error:
            return False
        movie = jellyfin_movie_or_error(ctx)
        stream = next((s for s in jellyfin_subtitle_streams(ctx, movie)
                       if s.get("IsExternal") and str(s.get("Path") or "") == path), None)
        if not stream:
            return False
        jellyfin_call(ctx["base"], ctx["key"], "DELETE", f"/Videos/{movie['Id']}/Subtitles/{stream['Index']}")
        return True
    except Exception as exc:
        print(f"删除 Jellyfin 里未校准的字幕失败: {exc}", flush=True)
        return False


def subtitle_sync_id(path):
    """这份字幕能不能交给“校准时间”用：要是 Jellyfin 资料目录里的文字字幕。返回旧校准流程认的编号。"""
    path = str(path or "")
    if Path(path).suffix.lower() not in (".srt", ".ass", ".ssa", ".vtt"):
        return None
    if not any(path.startswith(root + "/") for root in JELLYFIN_SUBTITLE_ROOTS):
        return None
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:24]


def local_sidecars(output):
    """影片旁边、名字以片名开头的外挂字幕文件。"""
    output = Path(output)
    try:
        return sorted(f for f in output.parent.iterdir()
                      if f.is_file() and f.suffix.lower() in SIDECAR_EXT and f.name.startswith(output.stem + "."))
    except OSError:
        return []


def jellyfin_refresh(ctx, movie):
    """让 Jellyfin 重新看一眼这部片（影片旁边多了或少了字幕文件时）。失败不要紧，它自己过一阵也会发现。"""
    try:
        jellyfin_call(ctx["base"], ctx["key"], "POST", f"/Items/{movie['Id']}/Refresh", {
            "MetadataRefreshMode": "Default", "ImageRefreshMode": "None",
            "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"}, timeout=20)
    except JellyfinError:
        pass


def refresh_jellyfin_for(name):
    """面板在影片旁边写了或删了字幕之后，顺手通知 Jellyfin。没配密钥、连不上都直接算了。"""
    output = tracked_movie_output(name)
    key = jellyfin_api_key()
    if not output or not key:
        return
    try:
        ctx = {"base": jellyfin_base_url(getattr(request, "host", "")), "key": key, "output": output}
        movie = jellyfin_find_movie(ctx["base"], key, output)
        if movie:
            jellyfin_refresh(ctx, movie)
    except JellyfinError:
        pass


@app.post("/api/subtitles/list")
def api_subtitles_list():
    ctx, error = subtitle_request_context(request.get_json(silent=True) or {})
    if error:
        return error
    try:
        movie = jellyfin_movie_or_error(ctx)
        streams = jellyfin_subtitle_streams(ctx, movie)
    except JellyfinError as exc:
        return jsonify({"error": str(exc), "needs_key": exc.needs_key}), 502
    output = ctx["output"]
    with LOCK:
        record = dict(PROCESSED.get(ctx["name"]) or {})
    exported = {Path(x).name for x in record.get("sidecars") or []}
    calibrated = subtitle_sidecar_path(output).name
    try:                                   # Jellyfin 自己下载的字幕：什么时候下的、多大，用来区分同名的几份
        downloaded = {item["path"]: item for item in jellyfin_subtitle_options(200)}
    except Exception:                      # 读不到只是少显示时间和大小，不影响列表
        downloaded = {}
    beside = {f.name: f for f in local_sidecars(output)}
    existing, seen_local = [], set()
    def describe(filename, language, index, path=""):
        entry = {"index": index, "filename": filename, "language": language, "sync_id": subtitle_sync_id(path),
                 "beside_movie": filename in beside, "when": "", "size_h": ""}
        if filename in beside:
            stat = beside[filename].stat()
            if filename.lower().endswith(".srt"):
                entry["sync_id"] = LOCAL_SUBTITLE_PREFIX + filename
            entry.update(when=time.strftime("%m-%d %H:%M", time.localtime(stat.st_mtime)), size_h=human(stat.st_size),
                         origin="校准后的" if filename == calibrated else
                         ("转换时从影片里取出的" if filename in exported else "放在影片旁边的"))
        else:
            info = downloaded.get(path) or {}
            entry.update(when=info.get("modified", ""), size_h=info.get("size_h", ""), origin="从网上下载的")
        return entry
    for stream in streams:
        if not stream.get("IsExternal"):
            continue
        path = str(stream.get("Path") or "")
        filename = path.rsplit("/", 1)[-1] or "外挂字幕"
        inside_jellyfin = any(path.startswith(root + "/") for root in JELLYFIN_SUBTITLE_ROOTS)
        if not inside_jellyfin and filename not in beside:
            continue                       # 影片旁边的文件已经删了，只是 Jellyfin 还没刷新
        if not inside_jellyfin:
            seen_local.add(filename)
        language = LANGUAGE_LABELS.get(str(stream.get("Language") or "").lower(), stream.get("Language") or "未标语言")
        existing.append(describe(filename, language, stream.get("Index"), path if inside_jellyfin else ""))
    for filename in beside:                # 刚放到影片旁边、Jellyfin 还没发现的
        if filename not in seen_local:
            existing.append(describe(filename, "Jellyfin 还没读到", None))
    embedded = sum(1 for stream in streams if not stream.get("IsExternal"))
    with LOCK:
        JELLYFIN_EXTERNAL_SUBS[str(Path(output).resolve())] = len(existing)
    ids = movie.get("ProviderIds") or {}
    return jsonify({"ok": True, "existing": existing, "embedded": embedded,
                    "title": str(movie.get("Name") or ""), "recognised": bool(ids.get("Tmdb") or ids.get("Imdb"))})


@app.post("/api/subtitles/search")
def api_subtitles_search():
    """上网搜这部片的中文字幕，把结果列出来让用户自己选；不自动下载。"""
    ctx, error = subtitle_request_context(request.get_json(silent=True) or {})
    if error:
        return error
    try:
        movie = jellyfin_movie_or_error(ctx)
        results = []
        for language in ("chi", "zho"):
            results = jellyfin_call(ctx["base"], ctx["key"], "GET",
                                    f"/Items/{movie['Id']}/RemoteSearch/Subtitles/{language}") or []
            if results:
                break
        if not results:
            plugins = jellyfin_call(ctx["base"], ctx["key"], "GET", "/Plugins") or []
            if not any("subtitle" in str(plugin.get("Name") or "").lower() for plugin in plugins):
                raise JellyfinError("Jellyfin 还没装字幕下载插件：在 Jellyfin 控制台 → 插件 → 目录 里装 Open Subtitles，"
                                    "填上账号后重启 Jellyfin 再试")
            ids = movie.get("ProviderIds") or {}
            if not (ids.get("Tmdb") or ids.get("Imdb")):
                shown = str(movie.get("Name") or ctx["output"].stem)
                raise JellyfinError(f"Jellyfin 没认出这是哪部片（它显示的片名是“{shown}”），所以搜不到字幕。"
                                    "在 Jellyfin 里打开这部片，点右上角 ⋮ → 识别，选对影片后再回来搜")
    except JellyfinError as exc:
        return jsonify({"error": str(exc), "needs_key": exc.needs_key}), 502
    wanted = release_kind(f"{ctx['hint']} {ctx['output'].name}")
    best = pick_chinese_subtitle(results, f"{ctx['hint']} {ctx['output'].name}")
    listed = []
    for item in results:
        if not item.get("Id"):
            continue
        kind = release_kind(item.get("Name"))
        text = " ".join(str(item.get(k) or "") for k in ("Name", "Comment", "Author"))
        tags = []
        if item.get("IsHashMatch"):
            tags.append("和影片文件完全对应")
        elif wanted and kind:
            tags.append("片源版本一致" if kind == wanted else "片源版本不同，时间轴可能不符")
        if SIMPLIFIED_HINT.search(text):
            tags.append("简体")
        elif TRADITIONAL_HINT.search(text):
            tags.append("繁体")
        if item.get("AiTranslated") or item.get("MachineTranslated"):
            tags.append("机器翻译")
        if item.get("Forced"):
            tags.append("只翻外语片段")
        listed.append({"id": str(item["Id"]), "name": str(item.get("Name") or "字幕"),
                       "downloads": int(item.get("DownloadCount") or 0), "format": str(item.get("Format") or ""),
                       "tags": tags, "recommended": best is not None and item.get("Id") == best.get("Id")})
    listed.sort(key=lambda entry: (not entry["recommended"], -entry["downloads"]))
    return jsonify({"ok": True, "results": listed})


@app.post("/api/subtitles/download")
def api_subtitles_download():
    data = request.get_json(silent=True) or {}
    ctx, error = subtitle_request_context(data)
    if error:
        return error
    subtitle_id = str(data.get("subtitle_id", ""))
    if not subtitle_id or len(subtitle_id) > 500:
        return jsonify({"error": "请选择一份字幕"}), 400
    def external_count():
        return sum(1 for stream in jellyfin_subtitle_streams(ctx, movie) if stream.get("IsExternal"))
    try:
        movie = jellyfin_movie_or_error(ctx)
        before = external_count()
        jellyfin_call(ctx["base"], ctx["key"], "POST",
                      f"/Items/{movie['Id']}/RemoteSearch/Subtitles/{urlparse.quote(subtitle_id, safe='')}")
        # NAS 实测：Jellyfin 下载完要过几秒才把新字幕登记到影片上，马上去列会看不到。
        # 这里等它登记好再返回，最多等 SUBTITLE_SETTLE_SECONDS 秒。
        deadline = time.time() + SUBTITLE_SETTLE_SECONDS
        registered = external_count() > before
        while not registered and time.time() < deadline:
            time.sleep(1)
            registered = external_count() > before
    except JellyfinError as exc:
        return jsonify({"error": str(exc), "needs_key": exc.needs_key}), 502
    if not registered:
        return jsonify({"ok": True, "message": "Jellyfin 正在处理，稍后重新打开此窗口查看"})
    return jsonify({"ok": True, "message": "已下载，可在电视上选择"})


@app.post("/api/subtitles/delete")
def api_subtitles_delete():
    """删掉这部片的一份外挂字幕。只删外挂的；装在影片文件里的删不了，也不会去动。"""
    data = request.get_json(silent=True) or {}
    ctx, error = subtitle_request_context(data)
    if error:
        return error
    index, filename = data.get("index"), str(data.get("filename") or "")
    local = {f.name: f for f in local_sidecars(ctx["output"])}
    if filename not in local and (isinstance(index, bool) or not isinstance(index, int)):
        return jsonify({"error": "请选择要删除的字幕"}), 400
    try:
        movie = jellyfin_movie_or_error(ctx)
        if filename in local:
            # 影片旁边的文件由面板自己删：NAS 实测让 Jellyfin 去删会报 500（它对影片文件夹没有写权限）
            try:
                local[filename].unlink()
            except OSError as exc:
                return jsonify({"error": f"删除失败：{exc}"}), 500
            removed = str(local[filename])
            with LOCK:
                record = PROCESSED.get(ctx["name"])
                if record:
                    if record.get("subtitle_sidecar") and Path(record["subtitle_sidecar"]).name == filename:
                        record["subtitle_sidecar"] = None
                    record["sidecars"] = [x for x in record.get("sidecars") or [] if Path(x).name != filename]
                    save_state()
            if len(local) == 1:
                # 影片旁边已经没有字幕了：把转换前备下的那份也清掉，免得重做时又被放回去
                stale = subtitle_candidate_path(ctx["name"])
                for leftover in (stale, stale.with_suffix(".name")):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
            jellyfin_refresh(ctx, movie)
        else:
            target = next((s for s in jellyfin_subtitle_streams(ctx, movie) if s.get("Index") == index), None)
            if not target or not target.get("IsExternal"):
                return jsonify({"error": "这份字幕已经不在了，或者是装在影片文件里的，删不了"}), 409
            jellyfin_call(ctx["base"], ctx["key"], "DELETE", f"/Videos/{movie['Id']}/Subtitles/{index}")
    except JellyfinError as exc:
        return jsonify({"error": str(exc), "needs_key": exc.needs_key}), 502
    with LOCK:
        OUTPUT_INFO.pop(Path(ctx["output"]), None)       # 卡片上的字幕条数重新读
    return jsonify({"ok": True, "message": "已删除"})

# ---------- 英语界面：中文 → 英语对照表。页面切到英语时，按这张表替换界面上的文字 ----------
# 先整句匹配，再按片段替换（长的优先），最后换标点。没收进表里的文字会保持中文。
EN_TEXT = {
    # 页头、筛选、通用按钮
    "TV转码": "TV Transcode", "影片": "Movies", "设置": "Settings", "浅色": "Light", "深色": "Dark",
    "打开 Jellyfin": "Open Jellyfin", "升级": "Upgrade", "正在读取…": "Loading…", "空间紧张": "low space",
    "全部": "All", "待处理": "Pending", "进行中": "Active", "取消": "Cancel", "删除": "Delete", "保存": "Save",
    "关闭": "Close", "应用": "Apply", "搜索": "Search", "下载": "Download", "改名": "Rename", "备注": "Note",
    "转换": "Convert", "不转换": "Keep", "排队转换": "Queue", "直接入库": "Import as is", "排队入库": "Queue import",
    "取消排队": "Unqueue", "选择文件夹": "Choose folder", "上一级": "Up", "选这个文件夹": "Use this folder",
    "撤销修改": "Discard", "保存设置": "Save settings", "恢复本页默认设置": "Reset this page",
    "开始升级": "Start upgrade", "新版 app.py": "New app.py", "从电脑上传": "Upload file", "校准时间": "Sync timing",
    "删除成品重做": "Delete output & redo", "删除成品": "Delete output", "删除源文件": "Delete source",
    "外挂中文字幕": "Chinese subtitles", "更换外挂中文字幕": "Change subtitles", "更新读盘工具": "Update disc tool",
    "重新读取": "Read again", "查看实时日志": "Live log", "打开": "Open",
    # 状态标记
    "必须转换": "Must convert", "建议转换": "Should convert", "不用转换": "No conversion needed", "已入库": "In library",
    "处理中": "Processing", "排队中": "Queued", "下载中": "Downloading", "无法处理": "Unsupported", "上次失败": "Failed",
    "读取失败": "Read failed", "读取中": "Reading",
    # 卡片
    "现在": "Now", "转换后": "After", "代价": "Cost", "入库": "Import", "音轨": "Audio", "字幕": "Subtitles",
    "全部保留": "Keep all", "不要字幕": "No subtitles", "声音": "Audio", "成品": "Output", "原样入库": "Imported as is",
    "蓝光原盘，电视无法直接播放": "Blu-ray disc: the TV cannot play it directly",
    "转为电视可直接播放的影片": "Becomes a file the TV plays directly",
    "双层杜比视界，电视不支持，无法点亮": "Dual-layer Dolby Vision: not supported by the TV",
    "杜比视界可以点亮": "Dolby Vision works",
    "移除电视用不到的数据，画面观感不变": "Drops data the TV cannot use; picture looks the same",
    "杜比视界在当前文件格式下可能无法点亮": "Dolby Vision may not activate in this file format",
    "杜比视界稳定点亮": "Dolby Vision activates reliably",
    "文件格式与设置中的杜比视界格式不一致": "File format differs from the Dolby Vision format in Settings",
    "转为设置的格式，杜比视界不变": "Converted to the configured format; Dolby Vision unchanged",
    "该文件格式未经电视验证，播放可能卡顿": "File format not verified on the TV; playback may stutter",
    "转为已验证的文件格式": "Converted to a verified file format",
    "电视不支持此音轨，由 NAS 临时转换：音质下降，起播和快进变慢":
        "The TV cannot play this audio; the NAS converts it on the fly: lower quality, slower start and seeking",
    "音质提升，即点即播": "Better audio, instant playback",
    "新增电视可直接播放的音轨": "Adds an audio track the TV plays directly",
    "保留原始音轨，供功放或回音壁使用": "Original audio kept for a receiver or soundbar",
    "部分音轨仅保留压缩版本": "Some audio kept only in compressed form",
    "字幕内嵌在影片中，电视加载需数分钟": "Subtitles are embedded; the TV needs minutes to load them",
    "字幕即时显示": "Subtitles appear instantly",
    "仅保留所选音轨和字幕，画面不变": "Keeps only the selected audio and subtitles; picture unchanged",
    "电视可直接播放，画面和声音保持原样": "Plays directly on the TV; picture and audio untouched",
    "无中文字幕，可在入库后添加": "No Chinese subtitles; add them after import",
    "未经电视验证；转换不改变编码": "not verified on the TV; conversion does not change the codec",
    "未经电视验证": "not verified on the TV",
    "下载中，完成后自动分析。": "Downloading. Analysed automatically when complete.",
    "已排队，等待前序任务。": "Queued behind earlier jobs.",
    "正在读取光盘信息（只读）。": "Reading disc info (read-only).",
    "正在读取影片信息…": "Reading media info…", "正在读取文件信息…": "Reading file info…",
    "暂时读不到影片信息": "Media info unavailable", "光盘读取失败，原因未知": "Disc read failed: unknown reason",
    "这张盘没有正片": "No main feature on this disc",
    "没有符合条件的影片": "No matching movies", "切换筛选查看其他影片": "Change the filter to see others",
    "还没有影片": "No movies yet", "正在载入媒体": "Loading media", "请稍候": "Please wait",
    "正在处理": "Processing", "正在估算剩余时间…": "Estimating time left…", "正在估算本阶段剩余时间": "Estimating time left",
    "本阶段已完成": "Stage complete", "读取影片信息": "Read media info", "读取光盘": "Read disc",
    "调整杜比视界": "Convert Dolby Vision", "复制到影片库": "Copy to library", "检查成品": "Verify output",
    "放进影片库": "Move to library", "识别字幕": "OCR subtitles",
    # 音轨、字幕的名字
    "简体中文": "Simplified Chinese", "繁体中文": "Traditional Chinese", "中文": "Chinese", "日语": "Japanese",
    "英语": "English", "韩语": "Korean", "未标语言": "Unknown language", "环绕声": "Surround", "立体声": "Stereo",
    "单声道": "Mono", "最高音质": "Lossless", "普通音质": "Standard", "全景声": "Atmos", "（推荐）": " (recommended)",
    "评论解说（不是正片声音）": "Commentary", "含音效说明": "SDH", "只翻译外语片段": "Forced",
    "（电视播不了）": " (TV cannot play)", "没有声音": "No audio", "没有字幕": "No subtitles",
    # 弹窗
    "删除源文件？": "Delete the source?", "删除未转换的源文件？": "Delete the unconverted source?",
    "删除后无法恢复。": "This cannot be undone.",
    "请先在电视上确认成品正常。删除后无法恢复，成品不受影响。":
        "Check the output on the TV first. This cannot be undone; the output is not affected.",
    "删除成品重做？": "Delete the output and redo?",
    "仅删除影片库中的成品，源文件保留。删除后无法恢复。": "Deletes only the library output; the source is kept. This cannot be undone.",
    "选择新版 app.py。升级期间页面短暂断开；失败自动回退。":
        "Choose the new app.py. The page disconnects briefly; a failed upgrade rolls back automatically.",
    "使用标准片名（建议带年份）以便 Jellyfin 识别。外挂字幕同步改名。":
        "Use a standard title (ideally with year) so Jellyfin can identify it. Sidecar subtitles are renamed too.",
    "例如：Evangelion 3.0+1.0 Thrice Upon a Time (2021)": "e.g. Evangelion 3.0+1.0 Thrice Upon a Time (2021)",
    "现有字幕": "Current subtitles", "在线字幕": "Online subtitles",
    "点击“搜索”列出可下载的中文字幕。": "Click Search to list downloadable Chinese subtitles.",
    "在 Jellyfin 控制台 → API 密钥 中新建，粘贴到此处并保存。仅需一次。":
        "Create one in Jellyfin Dashboard → API Keys, paste it here and save. Needed once.",
    "Jellyfin API 密钥": "Jellyfin API key", "Jellyfin 里的片名：": "Title in Jellyfin: ",
    "Jellyfin 未识别此影片（显示为“": "Jellyfin has not identified this movie (shown as \"",
    "”）。请先在 Jellyfin 中执行 ⋮ → 识别。": "\"). Run ⋮ → Identify in Jellyfin first.",
    "内嵌字幕": "Embedded subtitles", "加载慢，无法在此删除": "slow to load; cannot be deleted here",
    "暂无外挂字幕": "No sidecar subtitles", "搜索中…": "Searching…",
    "未找到中文字幕，可从电脑上传": "No Chinese subtitles found; you can upload a file",
    "影片尚未转换。可先上传字幕，转换后自动放到影片旁；在线搜索需入库后使用。":
        "Not converted yet. Upload a subtitle now and it is placed next to the movie after conversion; online search works once it is in the library.",
    "从网上下载的": "Downloaded", "校准后的": "Synced", "转换时从影片里取出的": "Extracted during conversion",
    "放在影片旁边的": "Next to the movie", "Jellyfin 还没读到": "not yet seen by Jellyfin",
    "推荐": "Recommended", "片源版本一致": "Same release type", "片源版本不同，时间轴可能不符": "Different release; timing may differ",
    "和影片文件完全对应": "Exact match for this file", "简体": "Simplified", "繁体": "Traditional",
    "机器翻译": "Machine translated", "只翻外语片段": "Forced only", "确认删除": "Confirm", "正在下载…": "Downloading…",
    "已下载": "Downloaded",
    "校准字幕时间": "Sync subtitle timing", "首句出现时间": "First line appears at",
    "填入首句台词在影片中的出现时间，整份字幕随之平移。": "Enter when the first line is spoken; the whole subtitle shifts with it.",
    "时间格式不正确": "Invalid time", "没有可预览的字幕段": "Nothing to preview",
    "已按影片自带字幕自动校准。确认后应用，可手动调整。": "Synced automatically against the movie's own subtitles. Apply to confirm, or adjust.",
    "与影片自带字幕不匹配（可能是不同剪辑版本）。请手动填写首句时间，或更换字幕":
        "Does not match the movie's own subtitles (possibly a different cut). Enter the first-line time manually or pick another subtitle",
    "无可参照的字幕时间点，请手动填写首句时间": "No reference timing available; enter the first-line time manually",
    "共享文件夹根目录": "Share root", "没有子文件夹": "No subfolders", "当前位置：": "Location: ",
    # 设置页
    "视频封装": "Video", "音轨处理": "Audio", "字幕封装": "Subtitles", "默认选轨": "Defaults", "媒体目录": "Folders",
    "视频流原样复制，不重新编码。": "The video stream is copied as is, never re-encoded.",
    "新增音轨排在首位并设为默认。": "An added track goes first and becomes the default.",
    "外挂文件与影片同目录，文件名以片名开头。": "Sidecar files sit next to the movie and start with its name.",
    "转换前可在影片卡片上改选。": "Can be changed per movie before converting.",
    "可选 NAS 上的任意文件夹。两个文件夹不能相同或互相包含。保存后面板重启。":
        "Any folder on the NAS can be chosen. The two folders must differ and not contain each other. Saving restarts the panel.",
    "杜比视界影片的格式": "Dolby Vision container", "双层杜比视界（Profile 7）": "Dual-layer Dolby Vision (Profile 7)",
    "电视可点亮 MKV 中的杜比视界时选 MKV。": "Choose MKV if your TV activates Dolby Vision from MKV.",
    "电视支持双层杜比视界时选保留。转换尚未经真实片源验证。":
        "Choose Keep if your TV supports dual-layer Dolby Vision. Conversion is not yet verified with a real source.",
    "转换为 Profile 8.1": "Convert to Profile 8.1", "保留 Profile 7": "Keep Profile 7",
    "文件格式": "Container", "画面编码": "Video codec", "片源": "Source", "处理": "Action", "成品格式": "Output",
    "成品编码": "Output", "其他杜比视界": "Other Dolby Vision", "HDR10、SDR": "HDR10, SDR", "所有影片": "All movies",
    "蓝光原盘（BDMV 文件夹、ISO、IMG）": "Blu-ray disc (BDMV folder, ISO, IMG)",
    "H.264、HEVC 之外的所有编码": "Any codec other than H.264, HEVC", "H.264、HEVC": "H.264, HEVC",
    "和片源相同": "Same as source", "杜比视界转为 Profile 8.1": "Dolby Vision converted to Profile 8.1",
    "因音轨或字幕转换时，成品为 MKV": "Becomes MKV when converted for audio or subtitles",
    "LG G5 实测可播放": "Verified on LG G5", "未在 LG G5 上验证": "Not verified on LG G5",
    "LG G5 实测：MKV 中的杜比视界偶尔无法点亮，MP4 稳定。": "Measured on LG G5: Dolby Vision in MKV sometimes fails to activate; MP4 is reliable.",
    "LG G5 不支持双层杜比视界。转换仅移除增强层。": "LG G5 does not support dual-layer Dolby Vision. Conversion only removes the enhancement layer.",
    "源音轨编码": "Source audio", "成品音轨": "Output audio", "源字幕": "Source subtitles", "成品字幕": "Output subtitles",
    "转换：新增 E-AC-3，保留原音轨": "Convert: add E-AC-3, keep original",
    "转换：替换为 E-AC-3": "Convert: replace with E-AC-3",
    "转换：新增 AC-3，保留原音轨": "Convert: add AC-3, keep original",
    "转换：替换为 AC-3": "Convert: replace with AC-3", " + 原音轨": " + original",
    "其他音频编码": "Other audio codecs",
    "LG G5 不支持，直接播放时由 Jellyfin 实时转码，音质下降。预先生成兼容音轨，原始音轨留给功放或回音壁。":
        "Not supported by LG G5: Jellyfin transcodes it on the fly at lower quality. A compatible track is made in advance; the original stays for a receiver or soundbar.",
    "LG G5 实测可直接播放。": "Plays directly on LG G5.", "未单独实测，通用支持。": "Not tested individually; widely supported.",
    "与 TrueHD 同类，未单独实测，按不支持处理。": "Same family as TrueHD; untested, treated as unsupported.",
    "未实测，按不支持处理。": "Untested; treated as unsupported.",
    "新增音轨最多 5.1 声道。E-AC-3 1536 kb/s，AC-3 640 kb/s。": "Added tracks are 5.1 at most. E-AC-3 1536 kb/s, AC-3 640 kb/s.",
    "文字字幕（SRT、ASS、SSA）": "Text subtitles (SRT, ASS, SSA)", "图片字幕（PGS）": "Image subtitles (PGS)",
    "图片字幕（DVD、DVB、XSUB）": "Image subtitles (DVD, DVB, XSUB)",
    "转换：导出为外挂文件": "Convert: export as sidecar file", "不转换：保留内嵌": "Keep: leave embedded",
    "外挂 .srt（SRT）或 .ass（ASS、SSA）": "Sidecar .srt (SRT) or .ass (ASS, SSA)", "外挂 .sup": "Sidecar .sup",
    "内嵌（ASS、SSA 样式丢失）": "Embedded (ASS, SSA styling lost)", "内嵌": "Embedded", "丢弃": "Dropped", "无": "None",
    "电视不支持": "Not supported by the TV",
    "内嵌字幕需 Jellyfin 读完整个文件才显示（24GB 实测 2 分 34 秒）；外挂即时显示。":
        "Embedded subtitles appear only after Jellyfin reads the whole file (2 min 34 s for 24 GB); sidecar files appear instantly.",
    "同上。需在电视端 Jellyfin 开启“实验性 PGS 字幕渲染”。": "Same as above. Enable \"Experimental PGS subtitle rendering\" in Jellyfin on the TV.",
    "默认音轨": "Default audio", "默认字幕语言": "Default subtitle language",
    "原语言中音质最高": "Best quality in the original language", "片源标记的默认音轨": "The track flagged default by the source",
    "第一条正片音轨": "The first main track", "不选字幕": "None",
    "原语言取自片源标记的默认音轨，无标记时取第一条。": "The original language is that of the track flagged default, or of the first track if none is flagged.",
    "音质排序：TrueHD > DTS-HD MA > LPCM > FLAC > DTS-HD HRA > DTS > E-AC-3 > AC-3 > AAC，同级取声道数多者。不选评论音轨。":
        "Quality order: TrueHD > DTS-HD MA > LPCM > FLAC > DTS-HD HRA > DTS > E-AC-3 > AC-3 > AAC; more channels win a tie. Commentary tracks are skipped.",
    "选择一条匹配语言的字幕，没有匹配时留空。": "Picks one subtitle in this language, or none if there is no match.",
    "每部影片保留一条音轨。": "One audio track is kept per movie.", "每部影片保留一条字幕。": "One subtitle is kept per movie.",
    "选轨示例": "Example", "轨道": "Track", "示例片源中的轨道": "Tracks in the sample", "当前规则预选": "Selected by these rules",
    "（默认）": " (default)", "日语 AAC": "Japanese AAC", "英语 AC-3": "English AC-3", "英语 TrueHD": "English TrueHD",
    "中文 SRT": "Chinese SRT", "英语 SRT": "English SRT",
    "下载文件夹": "Download folder", "影片文件夹": "Library folder",
    "扫描此文件夹中的影片和蓝光原盘。": "Scanned for movies and Blu-ray discs.",
    "成品写入此文件夹。Jellyfin 媒体库须指向同一位置。": "Outputs are written here. Point the Jellyfin library at the same folder.",
    "已保存 · 用于后续任务": "Saved · applies to later jobs", "设置已保存 · 用于后续任务": "Saved · applies to later jobs",
    "任务进行中，队列清空后可保存": "Jobs are running; save when the queue is empty",
    "保存后将重启面板": "saving restarts the panel", "正在保存…": "Saving…", "保存失败，请重试": "Save failed; try again",
    "正在读取设置…": "Loading settings…", "读取设置失败": "Could not load settings",
    "下载目录和影片目录须独立，不能相同或相互包含": "The two folders must differ and not contain each other",
    "已保存 · 正在切换目录": "Saved · switching folders", "媒体目录已切换": "Folders switched",
    # 提示
    "无法连接服务，正在重试": "Cannot reach the service; retrying", "无法连接服务，请稍后重试": "Cannot reach the service; try again later",
    "影片状态已变化": "Movie status changed", "已加入队列：转换": "Queued: convert", "已加入队列：直接入库": "Queued: import as is",
    "已取消排队": "Removed from queue", "正在应用…": "Applying…", "正在计算字幕时间…": "Calculating timing…",
    "正在删除…": "Deleting…", "已改名": "Renamed", "已开始后台升级": "Upgrade started", "请选择新版 app.py": "Choose the new app.py",
    "正在构建…": "Building…", "正在升级…": "Upgrading…", "已下载，可在电视上选择": "Downloaded; select it on the TV",
    "Jellyfin 正在处理，稍后重新打开此窗口查看": "Jellyfin is still processing; reopen this window later",
    "已删除": "Deleted", "中文字幕已下载": "Subtitles downloaded",
    # 补充：卡片上的规格、进度步骤、提示
    "杜比视界": "Dolby Vision", "+1 秒": "+1 s", "+10 秒": "+10 s", "−1 秒": "−1 s", "−10 秒": "−10 s",
    "BDMV 原盘": "BDMV disc", "光盘镜像": "Disc image", "多盘目录": "Multi-disc folder", "媒体文件": "Media file",
    "文件夹": "Folder", "目录资源": "Folder", "原盘画面": "Disc video", "原盘预读": "Disc pre-read", "转换前预读": "Pre-read",
    "正在预读规格": "Reading specs", "MakeMKV 只读预分析中": "MakeMKV read-only analysis", "LG G5 播放结果": "LG G5 playback",
    "预计 LG G5 成品": "Expected LG G5 output", "当前记录成品": "Recorded output", "可直接入库": "Ready to import",
    "需要拆分": "Needs splitting", "合规": "OK", "默认保留": "Kept by default", "画面": "Video", "当前": "Current",
    "已完成": "Done", "失败": "Failed", "等待下载完成": "Waiting for download", "等待处理": "Waiting", "等待探测": "Waiting to probe",
    "上次更新：": "Last update: ", "提前": "earlier", "延后": "later", "微调": "Nudge", "不同": "differs",
    "保留哪条音轨": "Audio track to keep", "保留哪条字幕": "Subtitle to keep", "处理步骤": "Steps", "页面": "Page",
    "设置分类": "Settings sections", "媒体状态筛选": "Status filter", "切换深色或浅色": "Toggle dark or light",
    "改完后前几句的时间": "First lines after the change",
    "正在转换": "Converting", "检查空间": "Check space", "转换并封装": "Convert & mux", "自检": "Verify",
    "当前已经是运行中的版本": "Already the running version",
    "中文字幕已准备": "Subtitles ready", "已备好中文字幕：": "Subtitles ready: ",
    "已删除当前成品并恢复待处理": "Output deleted; back to pending", "已重新排队读取光盘信息": "Disc read queued again",
    "开始更新读盘工具": "Updating disc tool", "正在开始更新…": "Starting update…", "正在排队读取…": "Queuing read…",
    "正在更新读盘工具…": "Updating disc tool…",
    "保存 Jellyfin 字幕失败": "Could not save Jellyfin subtitle", "保存失败": "Save failed", "删除失败": "Delete failed",
    "删除成品失败": "Could not delete output", "升级启动失败": "Upgrade could not start", "取消失败": "Cancel failed",
    "处理失败": "Processing failed", "字幕保存失败": "Could not save subtitle", "提交失败": "Submit failed",
    "读盘工具（MakeMKV）的版本过期了，更新之后才能读原盘。点“更新读盘工具”即可":
        "The disc tool (MakeMKV) is out of date and cannot read discs until updated. Click \"Update disc tool\".",
    "这张盘里没有 40 分钟以上的正片（多半是花絮盘），不处理": "No feature of 40 minutes or more on this disc (probably extras); skipped",
    "开始更新读盘工具，完成后会自动重新读取原盘": "Updating the disc tool; discs are read again when it finishes",
    "⚠ 提取出的轨道和预读时不一致，已改用自动预选，请验片":
        "⚠ Extracted tracks differ from the pre-read; switched to automatic selection. Check the output.",
    "包括 DTS Core 和 DTS-ES。": "Includes DTS Core and DTS-ES.", "包括 DTS-HD MA 和 DTS-HD HRA。": "Includes DTS-HD MA and DTS-HD HRA.",
    "包括带 Atmos 元数据的 TrueHD 音轨。": "Includes TrueHD tracks with Atmos metadata.",
    "包括 Dolby Digital Plus Atmos。": "Includes Dolby Digital Plus Atmos.", "MP4 输出只保留兼容轨。": "MP4 output keeps only the compatible track.",
    "匹配上方未列出的编码。": "Matches codecs not listed above.",
    "切换语言": "Switch language", "杜比全景声": "Dolby Atmos", "无损音质": "Lossless", "使用说明": "Guide", "入库之后": "After import", "电视播放不正常": "If playback is wrong on your TV",
    "Jellyfin 的媒体库指向“影片文件夹”；下载工具的保存位置设为“下载文件夹”。两个文件夹在“媒体目录”里查看和修改。":
        "Point the Jellyfin library at the Library folder and your download tool at the Download folder. Both are shown and changed under Folders.",
    "把影片下载到“下载文件夹”。下载完成后，“影片”页自动出现一张卡片。": "Download a movie into the Download folder. When it finishes, a card appears on the Movies page.",
    "看卡片": "Read the card",
    "右上角是结论：必须转换、建议转换或不用转换。中间三块是现在直接播会怎样、转换后会怎样、转换的代价。":
        "The badge is the verdict: must convert, should convert, or no conversion needed. The three tiles say what happens now, after converting, and what it costs.",
    "选音轨和字幕": "Pick audio and subtitles", "卡片下方已按“默认选轨”预选好，一般不用改。": "Already preselected by the Defaults rules; usually nothing to change.",
    "点“排队转换”，成品放进“影片文件夹”。不用转换的点“直接入库”。": "Click Queue; the output goes to the Library folder. If no conversion is needed, click Import as is.",
    "在电视上确认": "Check on the TV", "等 Jellyfin 扫描到成品，在电视上播放，确认画面、声音、字幕正常。": "Wait for Jellyfin to scan the output, then play it on the TV and check picture, audio and subtitles.",
    "确认无误后，在卡片上点“删除源文件”。删除后无法恢复。成品有问题时点“删除成品重做”，源文件保留。":
        "When it is fine, click Delete source on the card. This cannot be undone. If the output is wrong, click Delete output & redo; the source is kept.",
    "卡片上可以改名、搜索或上传中文字幕、校准字幕时间。字幕对不上时点“校准时间”，可以反复校准。":
        "On the card you can rename, search or upload Chinese subtitles, and sync subtitle timing. Sync timing can be repeated.",
    "默认规则来自 LG G5 的实测，其他电视可能不同。到“视频封装”“音轨处理”“字幕封装”里改规则，再“删除成品重做”。":
        "Default rules come from tests on an LG G5; other TVs may differ. Change the rules under Video, Audio and Subtitles, then Delete output & redo.",
    "完整说明（安装、升级、常见问题）": "Full guide (install, upgrade, FAQ; in Chinese)", "正在检查新版本…": "Checking for a new version…", "已是最新版本": "Up to date",
    "有新版本 ": "New version ", "查看": "View", "查不到新版本": "Could not check for a new version", "运行环境": "Environment", "面板依赖的工具和服务。": "Tools and services the panel depends on.",
    "正在检查…": "Checking…", "项目": "Item", "状态": "Status", "详情": "Details", "正常": "OK", "缺失": "Missing", "不可用": "Unavailable",
    "读盘工具（MakeMKV）": "Disc tool (MakeMKV)", "成品文件归属": "Output file owner", "检查失败": "Check failed",
    "操作失败": "Action failed", "改名失败": "Rename failed", "更新失败": "Update failed", "状态接口异常": "Status API error",
}
# 带数字的句子：正则 → 英语（$1、$2 是括号里匹配到的内容）
EN_PATTERNS = [
    [r"预计耗时 ([\d.]+) 小时", "about $1 h"], [r"预计耗时 ([\d.]+) 分钟", "about $1 min"],
    [r"成品仅含所选的 (\d+) 条音轨和 (\d+) 条字幕", "Output keeps only the $1 selected audio track(s) and $2 subtitle(s)"],
    [r"成品仅含所选的 (\d+) 条音轨，不含字幕", "Output keeps only the $1 selected audio track(s), no subtitles"],
    [r"画面是 (\S+) 编码，", "Video codec $1: "], [r"画面是 (\S+) 编码", "Video codec $1"],
    [r"外挂 (\d+) 条", "$1 sidecar"], [r"影片里 (\d+) 条", "$1 embedded"], [r" 等 (\d+) 条", " ($1 total)"],
    [r"内嵌字幕 (\d+) 条", "$1 embedded subtitle(s)"], [r"（第 (\d+) 条）", " (#$1)"],
    [r"后面还有 (\d+) 部", "$1 more queued"], [r"(\d+) 项待保存", "$1 unsaved"], [r"待存 (\d+)", "$1 unsaved"],
    [r"下载 (\d+) 次", "$1 downloads"], [r"排队中 #(\d+)", "Queued #$1"],
    [r"(\d+)小时(\d+)分", "$1h $2m"], [r"(\d+)分(\d+)秒", "$1m $2s"], [r"(\d+)秒", "$1s"], [r"(\d+)分钟", "$1 min"],
    [r"本阶段约剩 ", "About "], [r" · 预计 ", " left · ETA "], [r"已用 ", "Elapsed "],
    [r"^剩余 ", "Free "], [r"^共 ", "of "], [r"版本 ", "Version "], [r" · 指纹 ", " · build "], [r" · 升级于 ", " · upgraded "],
    [r"^当前Version", "Current version"], [r" · 可回收 ", " · frees "], [r"已删除，回收 ", "Deleted, freed "],
    [r"排队 #(\d+)", "Queue #$1"], [r"只翻外语", "Forced"],
    [r"下载到“(.+)”后自动出现", "Download into \"$1\" and it appears here"], [r"的备注$", " note"],
]


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="darkreader-lock">
<meta name="color-scheme" content="only dark">
<meta name="theme-color" content="#0b0e11">
<title>TV转码</title><style>
/* 配色：深色、浅色各一套，其余样式只用这些变量 */
:root{color-scheme:only dark;
  --bg:#000;--panel:#161617;--tile:#1f1f21;--field:#1f1f21;--ctl:rgba(255,255,255,.1);--ctl-h:rgba(255,255,255,.16);
  --seg:#48484a;--line:rgba(255,255,255,.1);--line2:rgba(255,255,255,.18);
  --text:#f5f5f7;--text2:#d2d2d7;--muted:#a1a1a6;--faint:#6e6e73;
  --accent:#2997ff;--btn:#0071e3;--btn-h:#0077ed;--ok:#30d158;--warn:#ff9f0a;--danger:#ff453a;
  --card:none;--card-h:0 0 0 1px rgba(255,255,255,.1);--nav:rgba(22,22,23,.78);--veil:rgba(0,0,0,.56);--pop:0 24px 70px rgba(0,0,0,.6);--thumb:0 1px 3px rgba(0,0,0,.4);
  --radius:18px;--h:34px}
html[data-theme="light"]{color-scheme:only light;
  --bg:#f5f5f7;--panel:#fff;--tile:#f5f5f7;--field:#fff;--ctl:rgba(0,0,0,.06);--ctl-h:rgba(0,0,0,.1);
  --seg:#fff;--line:rgba(0,0,0,.09);--line2:rgba(0,0,0,.18);
  --text:#1d1d1f;--text2:#424245;--muted:#6e6e73;--faint:#86868b;
  --accent:#0066cc;--btn:#0071e3;--btn-h:#0077ed;--ok:#248a3d;--warn:#b25000;--danger:#d70015;
  --card:0 1px 2px rgba(0,0,0,.03);--card-h:0 6px 24px rgba(0,0,0,.08);--nav:rgba(251,251,253,.8);--veil:rgba(0,0,0,.32);--pop:0 24px 70px rgba(0,0,0,.18);--thumb:0 1px 3px rgba(0,0,0,.14)}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:var(--line2) transparent}
html{background:var(--bg);scrollbar-gutter:stable;overflow-y:scroll}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);
  font:14px/1.47 -apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display","PingFang SC","Helvetica Neue","Microsoft YaHei",sans-serif;
  letter-spacing:-.01em;-webkit-font-smoothing:antialiased;font-feature-settings:"tnum" 1}
button,input,select,textarea{font:inherit;letter-spacing:inherit;color:inherit}
button{border:0;background:none;cursor:pointer}
:focus-visible{outline:2px solid var(--btn);outline-offset:2px}
[hidden]{display:none!important}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.spacer{flex:1}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

/* 页头：固定在顶部；右侧按钮宽度写死，文字变了位置也不变 */
.topbar{position:sticky;top:0;z-index:30;background:var(--nav);border-bottom:1px solid var(--line);
  -webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px)}
.topbar-in{width:min(1200px,calc(100% - 44px));height:52px;margin:0 auto;display:flex;align-items:center;gap:18px}
.appname{margin:0;width:112px;flex:none;font-size:17px;font-weight:600;letter-spacing:-.02em;white-space:nowrap}
.pages,.filters{position:relative;display:inline-flex;flex:none;padding:2px;border-radius:9px;background:var(--ctl)}
.pages::before{content:"";position:absolute;top:2px;bottom:2px;left:2px;width:calc(50% - 2px);border-radius:7px;background:var(--seg);
  box-shadow:var(--thumb);transition:transform .25s cubic-bezier(.3,.8,.3,1)}
.pages.on-rules::before{transform:translateX(100%)}
.pages a{position:relative;width:72px;height:28px;display:grid;place-items:center;border-radius:7px;color:var(--text);
  text-decoration:none;font-size:13px;font-weight:500;white-space:nowrap}
.pages a.active{font-weight:600}
.diskline{display:flex;align-items:center;gap:10px;margin-left:auto;flex:none;font-size:12px;color:var(--muted);white-space:nowrap}
.diskline strong{color:var(--text);font-weight:500}
.bar{height:6px;border-radius:99px;background:var(--ctl);overflow:hidden}
.bar>i{display:block;height:100%;width:0;border-radius:inherit;background:var(--accent);transition:width .7s ease,background-color .3s ease}
.diskline .bar{width:84px;height:4px}
.top-actions{display:flex;align-items:center;gap:8px;flex:none}
.topbar .ghost{height:30px;padding:0;font-size:12px;font-weight:500}
#jellyfinLink{width:92px}#upgradeOpen{width:92px}#langToggle{width:38px}#themeToggle{width:30px}
#themeToggle svg{width:15px;height:15px;display:block}
html[data-theme="light"] #themeToggle .sun,html:not([data-theme="light"]) #themeToggle .moon{display:none}
.shell{width:min(1200px,calc(100% - 44px));margin:0 auto;padding:36px 0 96px}

/* 按钮：胶囊形，三种 */
.ghost,.danger,.primary,.delete-confirm{height:var(--h);padding:0 16px;border-radius:980px;display:inline-flex;align-items:center;
  justify-content:center;gap:5px;font-size:13px;font-weight:500;white-space:nowrap;text-decoration:none;
  transition:background-color .15s ease,color .15s ease,opacity .15s ease,transform .12s ease}
.ghost{background:var(--ctl);color:var(--text)}.ghost:hover:not(:disabled){background:var(--ctl-h)}
.danger{background:color-mix(in srgb,var(--danger) 13%,transparent);color:var(--danger)}
.danger:hover:not(:disabled){background:color-mix(in srgb,var(--danger) 21%,transparent)}
.primary{background:var(--btn);color:#fff}.primary:hover:not(:disabled){background:var(--btn-h)}
.delete-confirm{background:var(--danger);color:#fff}.delete-confirm:hover:not(:disabled){filter:brightness(1.08)}
button:disabled,.ghost:disabled{opacity:.36;cursor:default}
button:not(:disabled):active{transform:scale(.97)}.topbar button:active{transform:none}

/* 下拉、输入框 */
select{-webkit-appearance:none;appearance:none;height:var(--h);padding:0 30px 0 12px;border:1px solid var(--line2);border-radius:10px;
  color:var(--text);font-size:13px;cursor:pointer;text-overflow:ellipsis;min-width:0;
  background:var(--field) url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%2212%22 height=%2212%22 viewBox=%220 0 12 12%22><path d=%22M2.5 4.5 6 8l3.5-3.5%22 fill=%22none%22 stroke=%22%2386868b%22 stroke-width=%221.5%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22/></svg>') no-repeat right 10px center;
  transition:border-color .15s ease,box-shadow .15s ease}
select:hover:not(:disabled){border-color:var(--faint)}select:disabled{opacity:.5;cursor:default}
select:focus-visible,.subtitle-search:focus,.sync-input:focus,.rule-text:focus,.rule-note:focus{outline:none;border-color:var(--btn);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--btn) 28%,transparent)}
input[type=checkbox]{accent-color:var(--btn);width:15px;height:15px;margin:0}
.subtitle-search,.sync-input,.rule-text{display:block;width:100%;height:38px;padding:0 12px;border:1px solid var(--line2);border-radius:10px;
  background:var(--field);color:var(--text);font-size:14px}
.subtitle-search{margin-top:16px}

/* 影片页 */
.library-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}
.page-title{margin:0;font-size:40px;line-height:1.1;font-weight:600;letter-spacing:-.03em}
.filter{height:28px;padding:0 12px;border-radius:7px;display:inline-flex;align-items:center;font-size:13px;font-weight:500;color:var(--text);white-space:nowrap;
  transition:background-color .18s ease,box-shadow .18s ease}
.filter.active{background:var(--seg);box-shadow:var(--thumb);font-weight:600}
.filter b{margin-left:6px;font-weight:500;color:var(--muted)}
.cards{display:flex;flex-direction:column;gap:14px}
.mcard{background:var(--panel);border-radius:var(--radius);padding:24px 28px;box-shadow:var(--card);animation:rise .3s ease both;
  transition:box-shadow .25s ease}
.mcard:hover{box-shadow:var(--card-h)}
.cards .mcard:nth-child(2){animation-delay:.03s}.cards .mcard:nth-child(3){animation-delay:.06s}.cards .mcard:nth-child(n+4){animation-delay:.09s}
.mc-top{display:flex;align-items:center;gap:14px}
.mc-name{flex:1;min-width:0;font-size:19px;line-height:1.26;font-weight:600;letter-spacing:-.02em;overflow-wrap:anywhere}
.mc-size{flex:none;color:var(--muted);font-size:13px}
.vbadge{flex:none;height:24px;padding:0 10px;border-radius:99px;display:inline-flex;align-items:center;font-size:12px;font-weight:600;
  white-space:nowrap;background:var(--ctl);color:var(--muted)}
.vbadge::before{content:"";width:6px;height:6px;margin-right:6px;border-radius:50%;background:currentColor}
.vbadge.v-must{background:color-mix(in srgb,var(--danger) 14%,transparent);color:var(--danger)}
.vbadge.v-should{background:color-mix(in srgb,var(--warn) 15%,transparent);color:var(--warn)}
.vbadge.v-none,.vbadge.v-done{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}
.mc-pic{margin-top:6px;color:var(--muted);font-size:13px}
.mc-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.qb{height:20px;padding:0 7px;border-radius:5px;display:inline-flex;align-items:center;font-size:11px;font-weight:700;letter-spacing:.02em;
  border:1px solid var(--line2);color:var(--muted);white-space:nowrap}
.qb-res{border-color:var(--text2);color:var(--text)}
.qb-dv{border-color:transparent;background:linear-gradient(90deg,#7b5cff,#e0457b);color:#fff}
.qb-hdr{border-color:transparent;background:var(--warn);color:#fff}
.qb-audio{border-color:var(--accent);color:var(--accent)}
.mc-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-top:18px}
.mc-col{background:var(--tile);border-radius:14px;padding:14px 16px 15px}
.mc-col b{display:block;margin-bottom:6px;font-size:12px;font-weight:600;color:var(--muted)}
.mc-col.without b{color:var(--danger)}.mc-col.gain b{color:var(--ok)}.mc-col.cost b{color:var(--warn)}
.mc-col ul{list-style:none;margin:0;padding:0;font-size:14px;line-height:1.5;color:var(--text)}
.mc-col li{position:relative;padding-left:13px}.mc-col li+li{margin-top:4px}
.mc-col li::before{content:"";position:absolute;left:1px;top:.66em;width:4px;height:4px;border-radius:50%;background:var(--faint)}
.mc-pick{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:18px;padding-top:18px;border-top:1px solid var(--line);
  font-size:13px;color:var(--muted)}
.mc-pick select{max-width:280px}.mc-pick label{display:flex;align-items:center;gap:6px;cursor:pointer}
.pick-hint{color:var(--faint);font-size:12px}
.mc-note{margin-top:12px;font-size:14px;color:var(--muted)}
.mc-done{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap}
.mc-done-info{flex:1;min-width:260px}.mc-done-actions{display:flex;gap:10px;flex-wrap:wrap}
.mc-facts{display:flex;flex-wrap:wrap;gap:6px 28px;margin-top:10px;font-size:14px}
.mc-facts b{margin-right:10px;font-size:12px;font-weight:600;color:var(--faint)}
.message{margin-top:12px;padding:9px 12px;border-radius:10px;font-size:13px;line-height:1.5;
  background:color-mix(in srgb,var(--danger) 10%,transparent);color:var(--danger)}
.message.warn{background:color-mix(in srgb,var(--warn) 12%,transparent);color:var(--warn)}
.donehint{margin-top:8px;font-size:12px;color:var(--warn)}
.empty{text-align:center;padding:72px 20px;color:var(--muted)}
.empty strong{display:block;margin-bottom:6px;color:var(--text);font-size:19px;font-weight:600;letter-spacing:-.02em}

/* 进度 */
.job{display:none}.job.inline{margin-top:16px}
.job-top{display:flex;align-items:flex-end;justify-content:space-between;gap:20px}
.job-kicker{display:flex;align-items:center;gap:8px;color:var(--accent);font-size:13px;font-weight:600}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--accent);animation:pulse 1.8s ease-out infinite}
.job-meta{text-align:right;color:var(--muted);font-size:12px}
.job-pct{display:block;color:var(--text);font-size:28px;line-height:1.1;font-weight:600;letter-spacing:-.03em}
.job .bar{height:6px;margin:12px 0 8px}
.job .bar>i{position:relative;overflow:hidden}
.job .bar>i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);
  transform:translateX(-100%);animation:sheen 1.8s ease-in-out infinite}
.job-foot{display:flex;justify-content:space-between;gap:16px;color:var(--muted);font-size:12px}
.steps{display:flex;gap:6px;overflow-x:auto;padding:12px 0 2px;scrollbar-width:none}.steps::-webkit-scrollbar{display:none}
.step{flex:none;height:24px;padding:0 10px;border-radius:99px;display:inline-flex;align-items:center;background:var(--ctl);color:var(--faint);
  font-size:12px;white-space:nowrap}
.step.done{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}
.step.now{color:var(--accent);background:color-mix(in srgb,var(--accent) 15%,transparent);font-weight:600}
.step .n{opacity:.7;margin-right:4px}
details{margin-top:12px}summary{cursor:pointer;color:var(--muted);font-size:12px;user-select:none}
pre{margin:10px 0 0;padding:12px 14px;max-height:230px;overflow:auto;border-radius:12px;background:var(--tile);color:var(--muted);
  font:11px/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap}
.format-flow{display:grid;grid-template-columns:minmax(0,1fr) 20px minmax(0,1fr);gap:6px;margin:12px 0 4px}
.format-box{min-width:0;border-radius:10px;padding:9px 11px;background:var(--tile)}
.format-label{display:block;margin-bottom:3px;color:var(--faint);font-size:11px}
.format-box strong{display:block;font-size:12px;font-weight:600;overflow-wrap:anywhere}
.format-box small{display:block;margin-top:3px;color:var(--muted);font-size:11px}
.format-arrow{display:grid;place-items:center;color:var(--faint)}
.tag{display:inline-flex;align-items:center;margin:1px 4px 1px 0;padding:2px 8px;border-radius:99px;font-size:11px;background:var(--ctl);color:var(--muted)}

/* 设置页 */
.rules-page{display:grid;grid-template-columns:200px minmax(0,1fr);background:var(--panel);border-radius:var(--radius);box-shadow:var(--card)}
.rules-side{display:flex;flex-direction:column;padding:26px 14px 20px;border-right:1px solid var(--line)}
.rules-side h2{margin:0 12px 14px;font-size:12px;font-weight:600;color:var(--muted)}
#rulesNav{display:flex;flex-direction:column;gap:2px}
#rulesNav button{height:34px;padding:0 12px;border-radius:8px;display:flex;align-items:center;justify-content:space-between;gap:6px;
  color:var(--text);font-size:14px;text-align:left;white-space:nowrap;transition:background-color .15s ease}
#rulesNav button:hover{background:var(--ctl)}
#rulesNav button.active{background:var(--ctl-h);font-weight:600}
#rulesNav small{color:var(--warn);font-size:11px;font-weight:500}
.version-box{margin-top:auto;padding:18px 12px 0;color:var(--faint);font-size:11px;line-height:1.6;overflow-wrap:anywhere}
.rules-main{display:flex;flex-direction:column;min-width:0}
.rules-body{flex:1;min-height:380px;padding:30px 34px 28px}
.rules-body.swap{animation:rise .22s ease both}
.rules-body h2{margin:0;font-size:28px;line-height:1.15;font-weight:600;letter-spacing:-.025em}
.rules-body h3{margin:30px 0 10px;font-size:17px;font-weight:600;letter-spacing:-.015em}
.rules-intro{margin:8px 0 22px;color:var(--muted);font-size:14px}
.rules-caption{margin:12px 0 0;color:var(--muted);font-size:13px}
.rule-field{display:grid;grid-template-columns:minmax(0,1fr) minmax(170px,32%);align-items:center;gap:20px;padding:16px 0;border-top:1px solid var(--line)}
.rule-field label{font-size:15px;font-weight:600;letter-spacing:-.01em}
.rule-description{margin:3px 0 0;color:var(--muted);font-size:13px;line-height:1.5}
.rule-why{display:flex;align-items:flex-start;gap:8px;margin-top:8px;font-size:13px}
.rule-why b{padding-top:5px;color:var(--faint);font-size:12px;font-weight:500;white-space:nowrap}
.rules-body select{width:100%}
textarea.rule-note{display:block;width:100%;min-width:0;padding:5px 8px;border:1px solid transparent;border-radius:8px;background:transparent;
  color:var(--muted);font-size:13px;line-height:1.5;resize:none;overflow:hidden;transition:background-color .15s ease,border-color .15s ease}
textarea.rule-note:hover:not(:disabled){background:var(--ctl)}
textarea.rule-note:focus{background:var(--field);color:var(--text)}
.folder-pick{display:flex;align-items:center;justify-content:flex-end;gap:12px}
.folder-now{font-size:14px;font-weight:500;overflow-wrap:anywhere}
.rule-matrix-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
.rule-matrix{table-layout:fixed;text-align:left;font-size:14px}
.rule-matrix th,.rule-matrix td{padding:11px 12px 11px 0;vertical-align:middle;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.rule-matrix thead th{padding-top:6px;padding-bottom:8px;color:var(--faint);font-size:12px;font-weight:500;white-space:nowrap}
.rule-matrix tbody th{color:var(--text);font-weight:500}
.rule-matrix tbody tr:last-child>*{border-bottom:0}
.rule-matrix select{width:100%}
.rule-five th:nth-child(1){width:19%}.rule-five th:nth-child(2){width:21%}.rule-five th:nth-child(3){width:9%}
.rule-five th:nth-child(4){width:11%}.rule-five th:nth-child(5){width:40%}
.rule-split th:nth-child(1){width:15%}.rule-split th:nth-child(2){width:26%}.rule-split th:nth-child(3){width:9%}
.rule-split th:nth-child(4){width:16%}.rule-split th:nth-child(5){width:34%}
.rule-split tr.group-more td{border-top:0}
.rule-split tr.group-first:has(+tr.group-more)>td:not([rowspan="2"]){border-bottom:0;padding-bottom:3px}
.rule-split tr.group-more>td{padding-top:3px}
.rule-matrix .output{white-space:nowrap}
.rule-do{color:var(--warn);font-weight:600}.rule-skip{color:var(--muted)}
.help-steps{margin:0;padding:0;list-style:none;counter-reset:step}
.help-steps li{counter-increment:step;display:grid;grid-template-columns:28px 130px minmax(0,1fr);gap:12px;align-items:baseline;padding:14px 0;border-top:1px solid var(--line)}
.help-steps li::before{content:counter(step);width:24px;height:24px;border-radius:50%;display:grid;place-items:center;background:var(--ctl);
  color:var(--muted);font-size:12px;font-weight:600}
.help-steps b{font-size:15px;font-weight:600}.help-steps span{color:var(--text2);font-size:14px}
.rules-caption a{color:var(--accent)}
@media(max-width:860px){.help-steps li{grid-template-columns:28px minmax(0,1fr)}.help-steps span{grid-column:2}}
.env-table th:nth-child(1){width:24%}.env-table th:nth-child(2){width:12%}
.env-ok,.env-bad,.env-warn{font-weight:600}.env-ok{color:var(--ok)}.env-bad{color:var(--danger)}.env-warn{color:var(--warn)}
.rules-reset{display:flex;margin:26px 0 0 auto}
.rules-foot{position:sticky;bottom:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 34px;
  border-top:1px solid var(--line);border-radius:0 0 var(--radius) 0;background:var(--nav);
  -webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px)}
.rules-actions{display:flex;align-items:center;gap:10px}
.rules-status{font-size:12px;color:var(--muted)}.rules-status.pending{color:var(--warn)}

/* 弹窗 */
.modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;padding:20px;background:var(--veil);
  -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);visibility:hidden;opacity:0;pointer-events:none;
  transition:opacity .2s ease,visibility 0s linear .2s}
.modal.open{visibility:visible;opacity:1;pointer-events:auto;transition:opacity .2s ease}
.dialog{width:min(460px,100%);max-height:calc(100vh - 40px);overflow:auto;padding:28px;border-radius:20px;background:var(--panel);
  box-shadow:var(--pop);transform:translateY(12px) scale(.97);transition:transform .26s cubic-bezier(.2,.9,.3,1.15)}
.modal.open .dialog{transform:none}
.subtitle-picker-dialog{width:min(680px,100%)}#folderModal .dialog{width:min(540px,100%)}
.dialog h2{margin:0 0 8px;font-size:21px;line-height:1.2;font-weight:600;letter-spacing:-.02em}
.dialog p{margin:0;color:var(--muted);font-size:14px}
.dialog-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:24px}
.subtitle-picker-actions{justify-content:space-between}
.delete-name{margin:14px 0;padding:11px 14px;border-radius:12px;background:var(--tile);font-size:13px;line-height:1.5;overflow-wrap:anywhere}
.sub-note{margin:4px 0 0!important;color:var(--muted);font-size:13px!important}.sub-note a{color:var(--accent)}
.sub-head{display:flex;align-items:center;justify-content:space-between;margin:22px 0 0;font-size:13px;font-weight:600;color:var(--muted)}
.subtitle-list{display:grid;gap:8px;max-height:min(420px,52vh);overflow:auto;margin-top:10px}
.sub-row{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:12px;background:var(--tile);animation:rise .2s ease both}
.sub-row>div:first-child{flex:1;min-width:0}
.sub-row strong{display:block;font-size:13px;font-weight:600;line-height:1.4;overflow-wrap:anywhere}
.sub-row span{display:block;margin-top:2px;color:var(--muted);font-size:12px}
.sub-row .tag-good{color:var(--ok)}.sub-row .tag-warn{color:var(--warn)}
.sub-row button{height:30px;padding:0 13px;font-size:12px}
.subtitle-choice{width:100%;display:flex;align-items:center;gap:12px;color:var(--text);text-align:left;transition:background-color .15s ease}
.subtitle-choice:hover{background:var(--ctl-h)}
.subtitle-choice{justify-content:flex-start;padding:10px 14px;background:transparent;border-radius:10px}
.subtitle-choice strong{flex:1;font-size:14px;font-weight:500;overflow-wrap:anywhere}
.subtitle-choice em{color:var(--faint);font-style:normal;font-size:17px;line-height:1}
.folder-icon{flex:none;width:20px;height:16px;color:var(--accent)}
#folderList{gap:0;margin-top:12px;padding:6px;border-radius:14px;background:var(--tile);min-height:220px;align-content:start}
#folderList .subtitle-choice+.subtitle-choice{box-shadow:0 -1px 0 var(--line)}
.crumbs{display:flex;flex-wrap:wrap;align-items:center;gap:2px;margin-top:6px;font-size:13px}
.crumbs button{padding:3px 7px;border-radius:7px;color:var(--muted);font-size:13px;transition:background-color .15s ease}
.crumbs button:hover{background:var(--ctl)}.crumbs button:last-child{color:var(--text);font-weight:600}
.crumbs i{color:var(--faint);font-style:normal}
.subtitle-list-status{padding:28px 12px;text-align:center;color:var(--muted);font-size:13px}
.auto-key{margin-top:14px}.auto-key small{display:block;margin:8px 0 12px;color:var(--muted);font-size:12px}
.file-grid{margin:16px 0 10px}
.file-field{display:block;padding:12px 14px;border-radius:12px;background:var(--tile)}
.file-field span{display:block;margin-bottom:8px;color:var(--muted);font-size:12px}
.file-field input{display:block;width:100%;font-size:12px}
.upgrade-note{min-height:18px;color:var(--muted);font-size:12px}
.sync-why{margin:4px 0 0!important;color:var(--warn)!important;font-size:13px!important}.sync-why:empty{display:none}.sync-why.ok{color:var(--ok)!important}
.sync-label{display:block;margin:18px 0 8px;font-size:13px;font-weight:600}
.sync-input-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.sync-input{flex:1;min-width:150px;font:600 15px/1 ui-monospace,SFMono-Regular,Menlo,monospace}
.sync-steps{display:flex;gap:6px}
.sync-step{height:38px;padding:0 10px;border-radius:10px;background:var(--ctl);color:var(--text);font-size:12px;transition:background-color .15s ease}
.sync-step:hover{background:var(--ctl-h)}
.sync-help{display:block;margin-top:8px;color:var(--muted);font-size:12px}
.sync-cues{display:grid;gap:6px;max-height:150px;overflow:auto;margin-top:12px}
.sync-cue{display:grid;grid-template-columns:118px minmax(0,1fr);gap:10px;padding:8px 12px;border-radius:10px;background:var(--tile)}
.sync-cue time{color:var(--accent);font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.sync-cue span{font-size:12px;overflow-wrap:anywhere}
.sync-error{display:none;margin-top:10px;color:var(--warn);font-size:12px}
.toast{position:fixed;left:50%;top:66px;z-index:80;max-width:min(440px,calc(100% - 40px));padding:10px 18px;border-radius:14px;
  background:var(--panel);color:var(--text);box-shadow:var(--pop);font-size:13px;opacity:0;transform:translate(-50%,-8px);pointer-events:none;
  transition:opacity .2s ease,transform .2s ease}
.toast.show{opacity:1;transform:translate(-50%,0)}.toast.warn{color:var(--warn)}.toast.error{color:var(--danger)}
#libraryPage:not([hidden]),#rulesPage:not([hidden]){animation:fade .18s ease both}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes sheen{to{transform:translateX(100%)}}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 45%,transparent)}70%,100%{box-shadow:0 0 0 8px transparent}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media(max-width:860px){
  .topbar-in{height:auto;flex-wrap:wrap;gap:10px 12px;padding:10px 0;width:calc(100% - 28px)}
  .diskline .bar{display:none}.top-actions{margin-left:auto}
  .shell{width:calc(100% - 28px);padding-top:22px}.page-title{font-size:26px}.library-head{flex-wrap:wrap}
  .mcard{padding:18px}.mc-name{font-size:17px}
  .rules-page{grid-template-columns:1fr}.rules-side{padding:14px;border-right:0;border-bottom:1px solid var(--line)}
  .rules-side h2{display:none}#rulesNav{flex-direction:row;overflow:auto}.version-box{padding:10px 4px 0}
  .rules-body{padding:22px 18px}.rules-body h2{font-size:24px}.rules-foot{padding:12px 18px;border-radius:0 0 var(--radius) var(--radius)}
  .rule-field{grid-template-columns:1fr;gap:10px}.rule-matrix{min-width:900px}
}
@media(max-width:560px){.mc-pick select{max-width:100%;flex:1 1 100%}.mc-pick .spacer{display:none}.sync-cue{grid-template-columns:96px minmax(0,1fr)}}
</style><script>try{var t=localStorage.getItem('theme');if(t!=='light'&&t!=='dark')t=matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';document.documentElement.dataset.theme=t;document.querySelector('meta[name=color-scheme]').content='only '+t}catch(e){}</script></head><body>
<header class="topbar"><div class="topbar-in">
    <h1 class="appname">TV转码</h1>
    <nav class="pages" id="pagesNav" aria-label="页面">
      <a href="#" id="navMovies" class="active">影片</a>
      <a href="#rules" id="navRules">设置</a>
    </nav>
    <div class="diskline">
      <strong id="diskFree">正在读取…</strong><span id="diskNote"></span>
      <div class="bar storage-bar"><i id="diskbar"></i></div>
    </div>
    <div class="top-actions">
    <a class="ghost" id="jellyfinLink" target="_blank" rel="noopener" aria-label="打开 Jellyfin" data-no-translate>Jellyfin ↗</a>
    <button class="ghost" id="upgradeOpen" type="button">升级</button>
    <button class="ghost" id="langToggle" type="button" aria-label="切换语言" data-no-translate>EN</button>
    <button class="ghost" id="themeToggle" type="button" aria-label="切换深色或浅色">
      <svg class="sun" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1"/></svg>
      <svg class="moon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8z"/></svg>
    </button>
    </div>
</div></header>
<main class="shell">

  <p class="message warn" id="pipelineNotice" style="display:none" role="status"></p>
  <div id="jobpark" hidden>
  <section class="job inline" id="jobcard" aria-live="polite">
    <div class="job-top">
      <div>
        <div class="job-kicker"><span class="pulse" aria-hidden="true"></span><span id="jobphase">正在处理</span></div>
      </div>
      <div class="job-meta"><strong class="job-pct" id="jobpct">0%</strong><span id="jobelapsed">已用 0秒</span></div>
    </div>
    <div class="bar"><i id="jobbar"></i></div>
    <div class="job-foot"><span id="jobeta">正在估算剩余时间…</span><span id="queueNote"></span></div>
    <div id="jobsteps" class="steps" aria-label="处理步骤"></div>
    <details><summary>查看实时日志</summary><pre id="joblog"></pre></details>
  </section>
  </div>

  <section class="card rules-page" id="rulesPage" aria-labelledby="settingsTitle" hidden>
    <aside class="rules-side"><h2 id="settingsTitle">设置</h2><nav id="rulesNav" aria-label="设置分类"></nav>
      <div class="version-box" id="versionBox"></div></aside>
    <div class="rules-main">
      <div id="rulesBody" class="rules-body"></div>
      <footer class="rules-foot">
        <span class="rules-status" id="rulesStatus" role="status" aria-live="polite"></span>
        <div class="rules-actions">
          <button class="ghost" id="rulesDiscard" type="button" hidden>撤销修改</button>
          <button class="primary" id="rulesSave" type="button" disabled>保存设置</button>
        </div>
      </footer>
    </div>
  </section>
  <section class="card library" id="libraryPage">
    <div class="library-head">
      <h2 class="page-title">影片</h2>
      <nav class="filters" aria-label="媒体状态筛选">
        <button class="filter active" data-filter="all" type="button">全部 <b id="countAll">0</b></button>
        <button class="filter" data-filter="ready" type="button">待处理 <b id="countReady">0</b></button>
        <button class="filter" data-filter="active" type="button">进行中 <b id="countActive">0</b></button>
        <button class="filter" data-filter="done" type="button"><span id="libraryName">影片</span> <b id="countDone">0</b></button>
      </nav>
    </div>
    <div id="rows" class="cards"><div class="empty"><strong>正在载入媒体</strong>请稍候</div></div>
  </section>
</main>


<div class="modal" id="deleteModal" role="dialog" aria-modal="true" aria-labelledby="deleteTitle">
  <div class="dialog">
    <h2 id="deleteTitle">删除源文件？</h2>
    <div class="delete-name" id="deleteName"></div>
    <p id="deleteWarning">删除后无法恢复。</p>
    <div class="dialog-actions">
      <button class="ghost" id="deleteCancel" type="button">取消</button>
      <button class="delete-confirm" id="deleteConfirm" type="button">删除</button>
    </div>
  </div>
</div>
<div class="modal" id="folderModal" role="dialog" aria-modal="true" aria-labelledby="folderTitle">
  <div class="dialog">
    <h2 id="folderTitle">选择文件夹</h2>
    <nav class="crumbs folder-now" id="folderPath" data-no-translate></nav>
    <div class="subtitle-list" id="folderList"></div>
    <div class="dialog-actions">
      <button class="ghost" id="folderUp" type="button">上一级</button>
      <span class="spacer"></span>
      <button class="ghost" id="folderCancel" type="button">取消</button>
      <button class="primary" id="folderChoose" type="button">选这个文件夹</button>
    </div>
  </div>
</div>
<div class="modal" id="renameModal" role="dialog" aria-modal="true" aria-labelledby="renameTitle">
  <div class="dialog">
    <h2 id="renameTitle">改名</h2>
    <p>使用标准片名（建议带年份）以便 Jellyfin 识别。外挂字幕同步改名。</p>
    <input class="subtitle-search" id="renameInput" type="text" maxlength="180" placeholder="例如：Evangelion 3.0+1.0 Thrice Upon a Time (2021)">
    <div class="sync-error" id="renameError"></div>
    <div class="dialog-actions">
      <button class="ghost" id="renameCancel" type="button">取消</button>
      <button class="primary" id="renameConfirm" type="button">改名</button>
    </div>
  </div>
</div>
<div class="modal" id="rebuildModal" role="dialog" aria-modal="true" aria-labelledby="rebuildTitle">
  <div class="dialog">
    <h2 id="rebuildTitle">删除成品重做？</h2>
    <div class="delete-name" id="rebuildName"></div>
    <p>仅删除影片库中的成品，源文件保留。删除后无法恢复。</p>
    <div class="dialog-actions">
      <button class="ghost" id="rebuildCancel" type="button">取消</button>
      <button class="delete-confirm" id="rebuildConfirm" type="button">删除成品</button>
    </div>
  </div>
</div>
<div class="modal" id="upgradeModal" role="dialog" aria-modal="true" aria-labelledby="upgradeTitle">
  <div class="dialog">
    <h2 id="upgradeTitle">升级</h2>
    <p class="sub-note" id="upgradeVersion"></p>
    <p class="sub-note" id="upgradeLatest"></p>
    <p>选择新版 app.py。升级期间页面短暂断开；失败自动回退。</p>
    <div class="file-grid single">
      <label class="file-field"><span>新版 app.py</span>
        <input id="upgradeApp" type="file" accept=".py,text/x-python">
      </label>
    </div>
    <p class="upgrade-note" id="upgradeNote"></p>
    <div class="dialog-actions">
      <button class="ghost" id="upgradeCancel" type="button">取消</button>
      <button class="delete-confirm" id="upgradeConfirm" type="button">开始升级</button>
    </div>
  </div>
</div>
<div class="modal" id="subtitlePickerModal" role="dialog" aria-modal="true" aria-labelledby="subtitlePickerTitle">
  <div class="dialog subtitle-picker-dialog">
    <h2 id="subtitlePickerTitle">外挂中文字幕</h2>
    <p class="sub-note" id="subtitleNote"></p>
    <div class="auto-key" id="jellyfinKeyRow" hidden>
      <input class="subtitle-search" id="jellyfinKey" type="password" autocomplete="off" placeholder="Jellyfin API 密钥">
      <small>在 Jellyfin 控制台 → API 密钥 中新建，粘贴到此处并保存。仅需一次。</small>
      <button class="primary" id="jellyfinKeySave" type="button">保存</button>
    </div>
    <div id="subtitleSections">
      <h3 class="sub-head">现有字幕</h3>
      <div class="subtitle-list" id="subtitleExisting"></div>
      <h3 class="sub-head">在线字幕<button class="ghost" id="subtitleSearchBtn" type="button">搜索</button></h3>
      <div class="subtitle-list" id="subtitleResults"><div class="subtitle-list-status">点击“搜索”列出可下载的中文字幕。</div></div>
    </div>
    <div class="dialog-actions subtitle-picker-actions">
      <button class="ghost" id="subtitleLocal" type="button">从电脑上传</button>
      <button class="ghost" id="subtitlePickerCancel" type="button">关闭</button>
    </div>
  </div>
</div>
<div class="modal" id="subtitleSyncModal" role="dialog" aria-modal="true" aria-labelledby="subtitleSyncTitle">
  <div class="dialog">
    <h2 id="subtitleSyncTitle">校准字幕时间</h2>
    <p class="sync-why" id="subtitleSyncError"></p>
    <label class="sync-label" for="subtitleSyncManual">首句出现时间</label>
    <div class="sync-input-row">
      <input class="sync-input" id="subtitleSyncManual" type="text" inputmode="decimal" placeholder="00:10:32.500">
      <div class="sync-steps" aria-label="微调">
        <button class="sync-step" type="button" data-sync-step="-10">−10 秒</button>
        <button class="sync-step" type="button" data-sync-step="-1">−1 秒</button>
        <button class="sync-step" type="button" data-sync-step="1">+1 秒</button>
        <button class="sync-step" type="button" data-sync-step="10">+10 秒</button>
      </div>
    </div>
    <small class="sync-help" id="subtitleSyncDirection">填入首句台词在影片中的出现时间，整份字幕随之平移。</small>
    <div class="sync-cues" id="subtitleSyncCues" aria-label="改完后前几句的时间"></div>
    <div class="dialog-actions">
      <button class="ghost" id="subtitleSyncCancel" type="button">取消</button>
      <button class="primary" id="subtitleSyncConfirm" type="button">应用</button>
    </div>
  </div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const $=s=>document.querySelector(s);
let sel=new Set(), state={items:[],queue:[]}, activeFilter='all', ticking=false,
  pendingDelete=null, pendingDiscard=false, pendingRebuild=null, pendingSubtitle=null,
  pendingSyncConfirm=null,jellyfinSubtitles=[], toastTimer=null;
function esc(value){return String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function tag(cls,txt){return '<span class="tag '+cls+'">'+esc(txt)+'</span>'}
function formatSeconds(sec){
  sec=Math.max(0,Math.floor(sec||0));const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=sec%60;
  return h?h+'小时'+m+'分':(m?m+'分'+s+'秒':s+'秒');
}
function humanBytes(bytes){
  let n=Number(bytes||0),units=['B','KB','MB','GB','TB'],i=0;
  while(n>=1024&&i<units.length-1){n/=1024;i++}
  return i===0?Math.round(n)+units[i]:n.toFixed(1)+units[i];
}
function signedOffset(seconds){
  const value=Number(seconds||0),sign=value>=0?'延后 ':'提前 ',total=Math.abs(value);
  const minutes=Math.floor(total/60),rest=(total-minutes*60).toFixed(3).replace(/\\.?0+$/,'');
  return sign+(minutes?minutes+'分 ':'')+rest+'秒';
}
function subtitleClock(milliseconds){
  let value=Math.max(0,Math.round(Number(milliseconds)||0));
  const hours=Math.floor(value/3600000);value%=3600000;
  const minutes=Math.floor(value/60000);value%=60000;
  const seconds=Math.floor(value/1000),millis=value%1000;
  return String(hours).padStart(2,'0')+':'+String(minutes).padStart(2,'0')+':'+
    String(seconds).padStart(2,'0')+'.'+String(millis).padStart(3,'0');
}
function parseSubtitleClock(value){
  const text=String(value||'').trim().replace(',','.'),parts=text.split(':');
  if(parts.length<2||parts.length>3)return null;
  const seconds=Number(parts.pop()),minutes=Number(parts.pop()),hours=parts.length?Number(parts.pop()):0;
  if(!Number.isFinite(hours)||!Number.isFinite(minutes)||!Number.isFinite(seconds)||
      hours<0||minutes<0||minutes>=60||seconds<0||seconds>=60)return null;
  return Math.round((hours*3600+minutes*60+seconds)*1000);
}
function showToast(message,type=''){
  const el=$('#toast');el.textContent=message;el.className='toast show '+type;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.className='toast',4200);
}
function kindTag(it){
  if(it.kind==='bdmv')return tag('t-acc','BDMV 原盘');
  if(it.kind==='iso')return tag('t-acc','光盘镜像');
  if(it.kind==='multi_disc')return tag('t-err','多盘目录');
  if(it.kind==='dir')return tag('t-dim','目录资源');
  return tag('t-dim','媒体文件');
}
function statusTag(it){
  if(it.status==='growing')return tag('t-warn','下载中');
  if(it.status==='queued'){const pos=(state.queue||[]).indexOf(it.name);return tag('t-acc',pos>=0?'排队 #'+(pos+1):'排队中')}
  if(it.status==='processing')return tag('t-acc','处理中');
  if(it.status==='done')return tag('t-ok','已完成 '+(it.done_ts||''));
  if(it.status==='error')return tag('t-err','处理失败');
  if(it.status==='blocked')return tag('t-err','需要拆分');
  if(['queued','analyzing'].includes(it.preview_status))return tag('t-acc','正在预读规格');
  return it.clean?tag('t-ok','可直接入库'):tag('t-warn','等待处理');
}
function matches(it,filter){
  if(filter==='ready')return it.status==='ready'||it.status==='error';
  if(filter==='active')return ['growing','queued','processing'].includes(it.status);
  if(filter==='done')return it.status==='done';
  if(filter==='attention')return ['error','blocked'].includes(it.status)||(it.warnings||[]).length>0||
    (it.plan||[]).some(p=>p.includes('⚠'));
  return true;
}
function planTags(it){
  return (it.plan||[]).map(p=>tag((p.includes('合规')||p.includes('原样入库'))?'t-ok':(p.includes('⚠')?'t-warn':'t-dim'),p)).join('')||
    (it.status==='done'?tag('t-ok','已入库'):it.status==='blocked'?'':
    tag('t-dim',it.status==='growing'?'等待下载完成':
      (['queued','analyzing'].includes(it.preview_status)?'MakeMKV 只读预分析中':'等待探测')));
}
function formatFlow(preview){
  if(!preview)return '';
  const sourceLabel=preview.pre_read?'转换前预读':(preview.disc_pending?'原盘预读':'原盘画面');
  const targetLabel=preview.pre_read||preview.disc_pending?'预计 LG G5 成品':'LG G5 播放结果';
  return '<div class="format-flow'+(preview.provisional?' provisional':'')+'">'+
    '<div class="format-box"><span class="format-label">'+sourceLabel+'</span><strong>'+esc(preview.source)+
    '</strong><small>'+esc(preview.source_detail)+'</small></div>'+
    '<div class="format-arrow" aria-hidden="true">→</div>'+
    '<div class="format-box target"><span class="format-label">'+targetLabel+'</span><strong>'+esc(preview.target)+
    '</strong><small>'+esc(preview.target_detail)+'</small></div></div>';
}
const PAGE_VERSION='__APP_VERSION__';
const choice=Object.create(null);
function updateSelection(){}
function pickFor(it){
  const c=it.card;if(!c)return null;
  const saved=choice[it.name]||{};
  const audio=(saved.audio!=null&&c.audio.some(a=>a.i===saved.audio))?saved.audio:(c.selected.audio.length?c.selected.audio[0]:null);
  const sub=(saved.sub!=null&&(saved.sub===-1||c.subs.some(x=>x.i===saved.sub)))?saved.sub:(c.selected.subs.length?c.selected.subs[0]:-1);
  return {audio,sub,all:!!saved.all};
}
function selectedVerdict(it,p){
  const c=it.card;
  if(p.all)return c.all;
  const mode=c.subs.find(track=>track.i===p.sub)?.mode||'none';
  return c.by_selection?.[p.audio??-1]?.[mode]||c.by_audio?.[p.audio]||c;
}
function libraryName(){return (state.folders||{}).library||'影片'}
function badge(it){
  if(it.status==='growing')return ['','下载中'];
  if(it.status==='queued'){const pos=(state.queue||[]).indexOf(it.name);return ['','排队中'+(pos>=0?' #'+(pos+1):'')]}
  if(it.status==='processing')return ['','处理中'];
  if(it.status==='done')return ['v-done',it.library?libraryName():'已入库'];
  if(it.status==='blocked'||it.preview_status==='no_feature')return ['','无法处理'];
  if(it.status==='error')return ['v-must','上次失败'];
  if(it.preview_status==='failed')return ['v-must','读取失败'];
  if(it.card){
    const p=pickFor(it),v=selectedVerdict(it,p);
    return ['v-'+v.verdict,v.verdict_label];
  }
  return ['','读取中'];
}
function list(items){return '<ul>'+items.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>'}
function busy(){return !!state.job||(state.queue||[]).length>0}
function discardBtn(it){return it.discardable?'<button class="danger" data-discard="1" type="button">删除源文件</button>':''}
function subBtn(it){return ['ready','error'].includes(it.status)?'<button class="ghost" data-subtitle="1" type="button">'+(it.subtitle_candidate?'更换外挂中文字幕':'外挂中文字幕')+'</button>':''}
function cardBody(it){
  if(it.status==='growing')return '<div class="mc-note">下载中，完成后自动分析。</div>';
  if(it.status==='blocked')return discardBtn(it)?'<div class="mc-pick"><span class="spacer"></span>'+discardBtn(it)+'</div>':'';
  if(it.status==='processing')return '<div class="mc-jobslot" data-jobslot="1"></div>';
  if(it.status==='queued')return '<div class="mc-pick"><span>已排队，等待前序任务。</span><span class="spacer"></span><button class="ghost" data-cancel="1" type="button">取消排队</button></div>';
  if(it.status==='done'){
    const info=it.info,sameName=it.library||it.output_name===it.name;
    const lines=[];
    if(info)lines.push('<div class="mc-pic">'+esc(info.picture)+'</div>',
      '<div class="mc-facts"><span><b>声音</b>'+esc(info.audio)+'</span><span><b>字幕</b>'+esc(info.subs)+'</span></div>');
    else lines.push('<div class="mc-pic">'+(it.info_unavailable?'暂时读不到影片信息':'正在读取影片信息…')+'</div>');
    if(!sameName&&it.output_name)lines.push('<div class="mc-facts"><span><b>'+(it.done_verdict==='none'?'原样入库':'成品')+'</b>'+esc(it.output_name)+(it.output_size_h?' · '+esc(it.output_size_h):'')+'</span></div>');
    if(it.delete_block_reason)lines.push('<div class="donehint">'+esc(it.delete_block_reason)+'</div>');
    return '<div class="mc-done"><div class="mc-done-info">'+lines.join('')+'</div><div class="mc-done-actions">'+
      '<button class="ghost" data-rename="1" type="button">改名</button>'+
      '<button class="ghost" data-subtitle="1" type="button">'+(it.subtitle_candidate?'更换外挂中文字幕':'外挂中文字幕')+'</button>'+
      (it.retryable?'<button class="danger" data-rebuild="1" type="button">删除成品重做</button>':'')+
      (it.deletable?'<button class="danger" data-del="1" type="button">删除源文件 · '+esc(it.size_h)+'</button>':'')+'</div></div>';
  }
  if(it.preview_status==='no_feature')return '<div class="mc-pick"><span>'+esc(it.preview_error||'这张盘没有正片')+'</span><span class="spacer"></span>'+discardBtn(it)+'</div>';
  if(['queued','analyzing'].includes(it.preview_status))return '<div class="mc-note">正在读取光盘信息（只读）。</div>';
  const c=it.card;
  if(!c){
    if(it.preview_status==='failed'){
      const upd=state.makemkv_update||{};
      const updating=upd.status==='running';
      const last=updating?'<div class="mc-note">'+esc(upd.message||'正在更新读盘工具…')+'</div>':
        (upd.status==='failed'?'<div class="message warn">上次更新：'+esc(upd.message||'失败')+'</div>':'');
      return '<div class="message">'+esc(it.preview_error||'光盘读取失败，原因未知')+'</div>'+last+
        '<div class="mc-pick"><span class="spacer"></span>'+
        discardBtn(it)+
        '<button class="primary" data-mkvupdate="1" type="button"'+(updating?' disabled':'')+'>更新读盘工具</button>'+
        '<button class="ghost" data-repreview="1" type="button"'+(updating?' disabled':'')+'>重新读取</button></div>';
    }
    return '<div class="mc-note">正在读取文件信息…</div>';
  }
  const p=pickFor(it);
  const head='<div class="mc-pic">'+esc(c.picture)+'</div>';
  if(c.verdict==='none'&&c.all.verdict==='none'&&!c.subs.length&&c.audio.length<=1){
    return head+'<div class="mc-cols"><div class="mc-col gain"><b>现在</b>'+list(c.gains)+'</div><div class="mc-col cost"><b>入库</b>'+list(c.costs)+'</div></div>'+
      '<div class="mc-pick"><span class="spacer"></span>'+discardBtn(it)+subBtn(it)+(it.selectable?'<button class="primary" data-asis="1" type="button">'+(busy()?'排队入库':'直接入库')+'</button>':'')+'</div>';
  }
  const aOpts=c.audio.map(a=>'<option value="'+a.i+'"'+(a.i===p.audio?' selected':'')+'>'+esc(a.label)+'</option>').join('');
  const sOpts='<option value="-1"'+(p.sub===-1?' selected':'')+'>不要字幕</option>'+
    c.subs.map(x=>'<option value="'+x.i+'"'+(x.i===p.sub?' selected':'')+'>'+esc(x.label)+'</option>').join('');
  const dis=p.all?' disabled':'';
  // 得失跟着当前选的音轨走
  const v=selectedVerdict(it,p);
  const cols=v.verdict==='none'
    ?'<div class="mc-col gain"><b>现在</b>'+list(v.gains)+'</div><div class="mc-col cost"><b>入库</b>'+list(v.costs)+'</div>'
    :((v.now||[]).length?'<div class="mc-col without"><b>现在</b>'+list(v.now)+'</div>':'')+
      '<div class="mc-col gain"><b>转换后</b>'+list(v.gains)+'</div><div class="mc-col cost"><b>代价</b>'+list(v.costs)+'</div>';
  return head+'<div class="mc-cols">'+cols+'</div>'+
    '<div class="mc-pick">'+
    (c.audio.length?'<span>音轨</span><select data-audio="1" aria-label="保留哪条音轨"'+dis+'>'+aOpts+'</select>':'')+
    '<span>字幕</span><select data-sub="1" aria-label="保留哪条字幕"'+dis+'>'+sOpts+'</select>'+
    '<label><input type="checkbox" data-all="1"'+(p.all?' checked':'')+'>全部保留</label>'+
    '<span class="spacer"></span>'+discardBtn(it)+subBtn(it)+
    (it.selectable&&c.can_import?'<button class="ghost" data-asis="1" type="button">直接入库</button>':'')+
    (it.selectable?'<button class="primary" data-go="1" type="button">'+(busy()?'排队转换':'转换')+'</button>':'')+'</div>';
}
function renderRows(){
  const rows=$('#rows'),jobCard=$('#jobcard');
  const visible=state.items.filter(it=>matches(it,activeFilter));
  const names=new Set(visible.map(it=>it.name));
  const existing=new Map();
  for(const row of Array.from(rows.children)){
    if(names.has(row.dataset.name))existing.set(row.dataset.name,row);
    else if(row.dataset.name!==undefined||visible.length){
      if(row.contains(jobCard))$('#jobpark').appendChild(jobCard);
      row.remove();
    }
  }
  if(!visible.length){
    const empty=state.items.length?'<strong>没有符合条件的影片</strong>切换筛选查看其他影片':
      '<strong>还没有影片</strong>下载到“'+esc((state.folders||{}).inbox||'下载文件夹')+'”后自动出现';
    if(!rows.firstElementChild)rows.innerHTML='<div class="empty"></div>';
    if(rows.firstElementChild.innerHTML!==empty)rows.firstElementChild.innerHTML=empty;
    return;
  }
  let next=rows.firstElementChild;
  for(const it of visible){
    let row=existing.get(it.name);
    if(!row){
      row=document.createElement('article');row.className='mcard';row.dataset.name=it.name;
    }
    const b=badge(it);
    const errors=it.error?'<div class="message">'+esc(it.error)+'</div>':'';
    const warnings=(it.warnings||[]).map(w=>'<div class="message warn">⚠ '+esc(w)+'</div>').join('');
    const html='<div class="mc-top"><div class="mc-name">'+esc(it.library?it.name.slice(4).split('/')[0]:it.name)+'</div>'+
      '<span class="mc-size">'+esc(it.size_h)+'</span><span class="vbadge '+b[0]+'">'+esc(b[1])+'</span></div>'+
      ((it.badges||[]).length?'<div class="mc-badges">'+it.badges.map(x=>'<span class="qb qb-'+esc(x.kind)+'">'+esc(x.text)+'</span>').join('')+'</div>':'')+
      errors+warnings+cardBody(it);
    // 其他影片在下载或排队时，保留这张卡片正在操作的控件。
    const editing=row.contains(document.activeElement)&&document.activeElement.tagName==='SELECT'&&row.dataset.status===it.status;
    if(row.cardHtml!==html&&!editing){
      if(row.contains(jobCard))$('#jobpark').appendChild(jobCard);
      row.innerHTML=html;row.cardHtml=html;row.dataset.status=it.status;
    }
    if(row!==next)rows.insertBefore(row,next);
    next=row.nextElementSibling;
    if(it.status==='processing'&&state.job?.name===it.name){
      const slot=row.querySelector('[data-jobslot]');
      if(slot&&slot!==jobCard.parentElement)slot.appendChild($('#jobcard'));
    }
  }
}
function itemForControl(control){
  const row=control.closest('article');
  return row&&state.items.find(it=>it.name===row.dataset.name);
}
$('#rows').onchange=event=>{
  const el=event.target,it=itemForControl(el);if(!it)return;
  const d=el.dataset;
  if('audio' in d)choice[it.name]={...choice[it.name],audio:+el.value};
  else if('sub' in d)choice[it.name]={...choice[it.name],sub:+el.value};
  else if('all' in d)choice[it.name]={...choice[it.name],all:el.checked};
  else return;
  if(el.tagName==='SELECT')el.blur();
  renderRows();
};
$('#rows').onclick=event=>{
  const btn=event.target.closest('button');if(!btn)return;
  const it=itemForControl(btn);if(!it)return;
  const d=btn.dataset;
  if('discard' in d)openDelete(it,true);
  else if('go' in d)submitItem(it,false,btn);
  else if('asis' in d)submitItem(it,true,btn);
  else if('cancel' in d)cancelItem(it,btn);
  else if('mkvupdate' in d)updateMakemkv(btn);
  else if('repreview' in d)repreviewItem(it,btn);
  else if('del' in d)openDelete(it);
  else if('rebuild' in d)openRebuild(it);
  else if('subtitle' in d)openSubtitlePicker(it);
  else if('rename' in d)openRename(it);
};
async function submitItem(item,asIs,btn){
  if(!item)return;btn.disabled=true;
  const body={names:[item.name],as_is:asIs};
  if(!asIs&&item.card){
    const p=pickFor(item);
    body.selection={[item.name]:p.all?{all:true}:{audio:p.audio==null?[]:[p.audio],subs:p.sub>=0?[p.sub]:[]}};
  }
  try{
    const r=await fetch('/api/process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'提交失败');
    if(res.warnings?.length)showToast(res.warnings.join(' '),'warn');
    else if(res.queued?.includes(item.name))showToast(asIs?'已加入队列：直接入库':'已加入队列：转换');
    else showToast('影片状态已变化','warn');
    await tick();
  }catch(error){showToast(error.message||'提交失败','error')}
  finally{btn.disabled=false}
}
async function updateMakemkv(btn){
  btn.disabled=true;btn.textContent='正在开始更新…';
  try{
    const r=await fetch('/api/makemkv_update',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'更新失败');
    showToast(res.message||'开始更新读盘工具');await tick();
  }catch(error){showToast(error.message||'更新失败','error')}
  finally{btn.disabled=false;btn.textContent='更新读盘工具'}
}
async function repreviewItem(item,btn){
  if(!item)return;btn.disabled=true;btn.textContent='正在排队读取…';
  try{
    const r=await fetch('/api/repreview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:item.name})});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'操作失败');
    showToast('已重新排队读取光盘信息');await tick();
  }catch(error){showToast(error.message||'操作失败','error')}
  finally{btn.disabled=false;btn.textContent='重新读取'}
}
async function cancelItem(item,btn){
  if(!item)return;btn.disabled=true;
  try{
    const r=await fetch('/api/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:item.name})});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'取消失败');
    showToast('已取消排队');await tick();
  }catch(error){showToast(error.message||'取消失败','error')}
  finally{btn.disabled=false}
}
function renderSubtitleSyncPreview(){
  if(!pendingSyncConfirm)return;
  const targetFirst=parseSubtitleClock($('#subtitleSyncManual').value);
  if(targetFirst===null){
    $('#subtitleSyncDirection').textContent='时间格式不正确';
    $('#subtitleSyncDirection').style.color='var(--danger)';
    $('#subtitleSyncConfirm').disabled=true;return;
  }
  $('#subtitleSyncConfirm').disabled=false;
  $('#subtitleSyncDirection').style.color='';
  $('#subtitleSyncDirection').textContent='填入首句台词在影片中的出现时间，整份字幕随之平移。';
  const delta=targetFirst-pendingSyncConfirm.sourceFirstStart;
  $('#subtitleSyncCues').innerHTML=(pendingSyncConfirm.sourcePreviewCues||[]).slice(0,3).map(cue=>
    '<div class="sync-cue"><time>'+subtitleClock(cue.start_ms+delta)+' → '+
    subtitleClock(cue.end_ms+delta)+'</time><span>'+esc(cue.text)+'</span></div>'
  ).join('')||'<div class="subtitle-list-status">没有可预览的字幕段</div>';
}
function openSubtitleSyncConfirm(result,action){
  const automaticOffset=Number(result.offset_seconds||0);
  const sourceFirstStart=Number(result.source_first_start_ms||0);
  const automaticFirstStart=Number(result.automatic_first_start_ms??sourceFirstStart);
  const suspicious=result.automatic_suspicious===true;
  const automaticAvailable=result.automatic_available!==false;
  pendingSyncConfirm={
    action,automaticOffset,automaticFirstStart,sourceFirstStart,
    sourcePreviewCues:result.source_preview_cues||result.preview_cues||[],
    automaticAvailable
  };
  $('#subtitleSyncError').textContent=result.automatic_error||result.automatic_note||'';
  $('#subtitleSyncError').classList.toggle('ok',!result.automatic_error);
  $('#subtitleSyncManual').value=subtitleClock(
    automaticAvailable&&!suspicious?automaticFirstStart:sourceFirstStart
  );
  renderSubtitleSyncPreview();
  $('#subtitleSyncModal').classList.add('open');$('#subtitleSyncConfirm').focus();
}
function closeSubtitleSyncConfirm(){
  $('#subtitleSyncModal').classList.remove('open');pendingSyncConfirm=null;
  $('#subtitleSections').querySelectorAll('button').forEach(btn=>btn.disabled=false);
}
function chooseLocalSubtitle(item){
  const input=document.createElement('input');input.type='file';
  input.accept='.srt,application/x-subrip,text/plain';
  input.onchange=()=>{
    const file=input.files[0];if(!file)return;
    const send=async(confirmAudio,manualFirstStart=null)=>{
      const data=new FormData();data.append('name',item.name);data.append('subtitle',file);
      if(confirmAudio)data.append('confirm_audio','1');
      if(!confirmAudio)data.append('auto','1');
      if(manualFirstStart!==null)data.append('manual_first_start_ms',String(manualFirstStart));
      if(confirmAudio)showToast('正在应用…');
    try{
      const r=await fetch('/api/subtitle_candidate',{method:'POST',body:data});
      const res=await r.json();if(!r.ok)throw new Error(res.error||'字幕保存失败');
      if(res.confirmation_required){
        openSubtitleSyncConfirm(res,firstStart=>send(true,firstStart));return;
      }
      closeSubtitleSyncConfirm();
      closeSubtitlePicker();showToast(res.message||'中文字幕已准备');await tick();
    }catch(error){
      closeSubtitleSyncConfirm();showToast(error.message||'字幕保存失败','error');
    }
    };
    send(false);
  };
  input.click();
}
async function subtitleCall(path,extra){
  const item=pendingSubtitle,body={name:item.name,...(extra||{})},typed=$('#jellyfinKey').value.trim();if(typed)body.api_key=typed;
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const res=await r.json();
  if(res.needs_key){$('#jellyfinKeyRow').hidden=false;$('#jellyfinKey').focus()}
  else if(r.ok){$('#jellyfinKey').value='';$('#jellyfinKeyRow').hidden=true}
  if(!r.ok)throw new Error(res.error||'操作失败');
  return res;
}
function subRow(title,meta,buttons){
  return '<div class="sub-row"><div><strong>'+esc(title)+'</strong>'+meta+'</div>'+buttons+'</div>';
}
async function loadExistingSubtitles(){
  const item=pendingSubtitle,box=$('#subtitleExisting');if(!item)return;
  box.innerHTML='<div class="subtitle-list-status">正在读取…</div>';
  try{
    const res=await subtitleCall('/api/subtitles/list');
    if(!pendingSubtitle||pendingSubtitle.name!==item.name)return;
    const shownTitle='<span data-no-translate>'+esc(res.title)+'</span>';
    $('#subtitleNote').innerHTML=res.recognised?'Jellyfin 里的片名：'+shownTitle:'Jellyfin 未识别此影片（显示为“'+shownTitle+'”）。请先在 Jellyfin 中执行 ⋮ → 识别。';
    const rows=res.existing.map(x=>subRow(x.origin+(x.when?' · '+x.when:''),'<span>'+esc(x.filename)+' · '+esc(x.language)+(x.size_h?' · '+esc(x.size_h):'')+'</span>',
      (x.sync_id?'<button class="ghost" data-sync="'+esc(x.sync_id)+'" type="button">校准时间</button>':'')+
      '<button class="danger" data-delsub="'+(x.index==null?'':x.index)+'" data-delname="'+esc(x.beside_movie?x.filename:'')+'" type="button">删除</button>'));
    if(res.embedded)rows.push(subRow('内嵌字幕 '+res.embedded+' 条','<span>加载慢，无法在此删除</span>',''));
    box.innerHTML=rows.join('')||'<div class="subtitle-list-status">暂无外挂字幕</div>';
  }catch(error){box.innerHTML='<div class="subtitle-list-status">'+esc(error.message)+'</div>'}
}
// Jellyfin 登记新字幕有时慢几秒：下载后过一会儿再自动刷新两次，窗口还开着才刷
function refreshSubtitlesLater(){
  const name=pendingSubtitle&&pendingSubtitle.name;
  for(const delay of [6000,15000])setTimeout(async()=>{
    if(pendingSubtitle&&pendingSubtitle.name===name&&$('#subtitlePickerModal').classList.contains('open')){await loadExistingSubtitles();tick()}
  },delay);
}
async function searchSubtitles(){
  const item=pendingSubtitle,box=$('#subtitleResults'),btn=$('#subtitleSearchBtn');if(!item)return;
  btn.disabled=true;box.innerHTML='<div class="subtitle-list-status">搜索中…</div>';
  try{
    const res=await subtitleCall('/api/subtitles/search');
    if(!pendingSubtitle||pendingSubtitle.name!==item.name)return;
    box.innerHTML=res.results.map(x=>subRow(x.name,'<span>'+
      (x.recommended?'<b class="tag-good">推荐</b> · ':'')+
      x.tags.map(t=>'<b class="'+(t.includes('不同')||t.includes('机器')||t.includes('只翻')?'tag-warn':'tag-good')+'">'+esc(t)+'</b>').join(' · ')+(x.tags.length?' · ':'')+
      '下载 '+x.downloads+' 次'+(x.format?' · '+esc(x.format):'')+'</span>',
      '<button class="'+(x.recommended?'primary':'ghost')+'" data-getsub="'+esc(x.id)+'" type="button">下载</button>')).join('')||
      '<div class="subtitle-list-status">未找到中文字幕，可从电脑上传</div>';
  }catch(error){box.innerHTML='<div class="subtitle-list-status">'+esc(error.message)+'</div>'}
  finally{btn.disabled=false}
}
async function openSubtitlePicker(item){
  pendingSubtitle=item;
  $('#subtitlePickerTitle').textContent='外挂中文字幕';
  $('#jellyfinKey').value='';$('#jellyfinKeyRow').hidden=true;
  const inLibrary=item.status==='done';$('#subtitleSections').hidden=!inLibrary;
  $('#subtitleNote').textContent=inLibrary?'':'影片尚未转换。可先上传字幕，转换后自动放到影片旁；在线搜索需入库后使用。';
  $('#subtitleResults').innerHTML='<div class="subtitle-list-status">点击“搜索”列出可下载的中文字幕。</div>';
  $('#subtitlePickerModal').classList.add('open');$('#subtitlePickerCancel').focus();
  if(inLibrary)loadExistingSubtitles();
}
$('#subtitleSearchBtn').onclick=searchSubtitles;
$('#jellyfinKeySave').onclick=loadExistingSubtitles;
$('#subtitleSections').onclick=async e=>{
  const btn=e.target.closest('button');if(!btn||!pendingSubtitle)return;const d=btn.dataset;
  if('sync' in d){useJellyfinSubtitle(d.sync);return}
  if('getsub' in d){
    btn.disabled=true;btn.textContent='正在下载…';
    try{const res=await subtitleCall('/api/subtitles/download',{subtitle_id:d.getsub});showToast(res.message);btn.textContent='已下载';await loadExistingSubtitles();tick();refreshSubtitlesLater()}
    catch(error){showToast(error.message,'error');btn.disabled=false;btn.textContent='下载'}
    return;
  }
  if('delsub' in d){
    // 删除不弹确认框：第一次点变成“确认删除”，再点才删
    if(!btn.dataset.armed){btn.dataset.armed='1';btn.textContent='确认删除';setTimeout(()=>{if(btn.isConnected){delete btn.dataset.armed;btn.textContent='删除'}},4000);return}
    btn.disabled=true;
    try{const res=await subtitleCall('/api/subtitles/delete',d.delname?{filename:d.delname}:{index:+d.delsub});showToast(res.message);await loadExistingSubtitles();tick();refreshSubtitlesLater()}
    catch(error){showToast(error.message,'error');btn.disabled=false}
  }
};
function closeSubtitlePicker(){
  $('#subtitlePickerModal').classList.remove('open');
  pendingSubtitle=null;jellyfinSubtitles=[];
}
async function useJellyfinSubtitle(subtitleId,confirmAudio=false,manualFirstStart=null){
  if(!pendingSubtitle)return;const item=pendingSubtitle;
  $('#subtitleSections').querySelectorAll('button').forEach(btn=>btn.disabled=true);
  showToast(confirmAudio?'正在应用…':'正在计算字幕时间…');
  try{
    const r=await fetch('/api/subtitle_candidate/jellyfin',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:item.name,subtitle_id:subtitleId,confirm_audio:confirmAudio,
        manual_first_start_ms:manualFirstStart,auto:!confirmAudio})});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'保存 Jellyfin 字幕失败');
    if(res.confirmation_required){
      $('#subtitleSections').querySelectorAll('button').forEach(btn=>btn.disabled=false);
      openSubtitleSyncConfirm(res,firstStart=>useJellyfinSubtitle(subtitleId,true,firstStart));return;
    }
    closeSubtitleSyncConfirm();
    closeSubtitlePicker();showToast(res.message||'中文字幕已准备');await tick();
  }catch(error){
    closeSubtitleSyncConfirm();
    showToast(error.message||'保存 Jellyfin 字幕失败','error');
    $('#subtitleSections').querySelectorAll('button').forEach(btn=>btn.disabled=false);
  }
}
function renderCounts(){
  const items=state.items,ready=items.filter(i=>i.status==='ready'||i.status==='error').length;
  const done=items.filter(i=>i.status==='done').length,active=items.filter(i=>['growing','queued','processing'].includes(i.status)).length;
  $('#countAll').textContent=items.length;$('#countReady').textContent=ready;$('#countActive').textContent=active;
  $('#countDone').textContent=done;$('#libraryName').textContent=libraryName();
}
function renderDisk(){
  const d=state.disk;if(!d)return;
  $('#diskFree').textContent='剩余 '+d.free_h;
  $('#diskNote').textContent='共 '+d.total_h+(d.low?' · 空间紧张':'');
  $('#diskbar').style.width=d.pct_used+'%';
  $('#diskbar').style.background=d.low?'var(--danger)':(d.pct_used>85?'var(--warn)':'var(--accent)');
}
function renderUpgrade(){
  const notice=$('#pipelineNotice');notice.textContent=state.processing_block_reason||'';notice.style.display=state.processing_block_reason?'block':'none';
  $('#jellyfinLink').href=state.jellyfin_link||(location.protocol+'//'+location.hostname+':'+(state.jellyfin_port||8096));
  const u=state.upgrade||{},active=['accepted','building','ready','switching'].includes(u.status);
  const busy=!!state.job||(state.queue||[]).length>0;
  $('#upgradeOpen').disabled=active||busy;
  $('#upgradeOpen').textContent=active?(u.status==='building'?'正在构建…':'正在升级…'):'升级';
  $('#upgradeNote').textContent=u.message||'';
  const version='版本 '+(state.release||'—')+' · 指纹 '+(state.app_version||'—');
  // 升级时间按浏览器所在时区显示：升级脚本所在的容器用的是 UTC，它写下的文字时间会差 8 小时
  const two=n=>String(n).padStart(2,'0'),at=u.updated_epoch?new Date(u.updated_epoch*1000):null;
  const when=at?two(at.getMonth()+1)+'-'+two(at.getDate())+' '+two(at.getHours())+':'+two(at.getMinutes()):'';
  $('#versionBox').textContent=version+(when&&u.status==='success'?' · 升级于 '+when:'');
  $('#upgradeVersion').textContent='当前'+version;
}
const PHASE_PLAIN=[[/^探测流信息/,'读取影片信息'],[/^(取出原盘正片|MakeMKV)/,'读取光盘'],[/^DoVi/,'调整杜比视界'],
  [/^封装/,'转换'],[/^直接入库/,'复制到影片库'],[/^成品自检/,'检查成品'],[/^移入/,'放进影片库'],[/^OCR/,'识别字幕']];
function plainPhase(name){for(const [re,label] of PHASE_PLAIN)if(re.test(name||''))return label;return name||'正在处理'}
function renderJob(){
  const j=state.job,card=$('#jobcard');card.style.display=j?'block':'none';if(!j)return;
  const pct=Math.max(0,Math.min(100,j.percent||0));
  $('#jobphase').textContent=plainPhase(j.phase);$('#jobpct').textContent=Math.floor(pct)+'%';
  $('#jobelapsed').textContent='已用 '+formatSeconds(j.elapsed);$('#jobbar').style.width=pct+'%';
  $('#queueNote').textContent=(state.queue||[]).length?'后面还有 '+state.queue.length+' 部':'';
  $('#jobeta').textContent=j.eta_sec!=null&&j.eta_clock?'本阶段约剩 '+formatSeconds(j.eta_sec)+' · 预计 '+j.eta_clock:
    (pct>=100?'本阶段已完成':'正在估算本阶段剩余时间');
  $('#jobsteps').innerHTML=(j.steps||[]).map((s,i)=>{
    const cls=i<j.step_idx?'step done':(i===j.step_idx?'step now':'step'),mark=i<j.step_idx?'✓':i+1;
    return '<span class="'+cls+'"><span class="n">'+mark+'</span>'+esc(plainPhase(s))+'</span>';
  }).join('');
  const log=$('#joblog');log.textContent=(j.log||[]).join('\\n');log.scrollTop=log.scrollHeight;
}
function openDelete(item,discard){
  pendingDelete=item;pendingDiscard=!!discard;$('#deleteName').textContent=item.name+' · 可回收 '+item.size_h;
  $('#deleteTitle').textContent=discard?'删除未转换的源文件？':'删除源文件？';
  $('#deleteConfirm').textContent='删除';
  $('#deleteWarning').textContent=discard?'删除后无法恢复。':'请先在电视上确认成品正常。删除后无法恢复，成品不受影响。';
  $('#deleteModal').classList.add('open');$('#deleteCancel').focus();
}
function closeDelete(){$('#deleteModal').classList.remove('open');pendingDelete=null}
let pendingRename=null;
function openRename(item){
  pendingRename=item;$('#renameError').textContent='';
  $('#renameInput').value=(item.output_name||'').replace(/\\.[^.]+$/,'');
  $('#renameModal').classList.add('open');$('#renameInput').focus();$('#renameInput').select();
}
function closeRename(){$('#renameModal').classList.remove('open');pendingRename=null}
async function confirmRename(){
  if(!pendingRename)return;const item=pendingRename,btn=$('#renameConfirm');btn.disabled=true;
  try{
    const r=await fetch('/api/rename_output',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:item.name,title:$('#renameInput').value})});
    const res=await r.json();if(!r.ok){$('#renameError').textContent=res.error||'改名失败';return}
    closeRename();showToast(res.message||'已改名');await tick();
  }catch(error){$('#renameError').textContent='无法连接服务，请稍后重试'}
  finally{btn.disabled=false}
}
function openRebuild(item){
  pendingRebuild=item;
  const output=item.output_name||'当前记录成品',size=item.output_size_h?' · '+item.output_size_h:'';
  $('#rebuildName').textContent=output+size;
  $('#rebuildModal').classList.add('open');$('#rebuildCancel').focus();
}
function closeRebuild(){$('#rebuildModal').classList.remove('open');pendingRebuild=null}
async function tick(){
  if(ticking)return;ticking=true;
  try{
    const response=await fetch('/api/state');if(!response.ok)throw new Error('状态接口异常');
    state=await response.json();
    // 服务升级后，已经打开的页面还在跑旧脚本：按钮和文字都对不上。发现版本变了就自动刷新一次。
    if(state.app_version&&PAGE_VERSION!=='__APP_'+'VERSION__'&&state.app_version!==PAGE_VERSION){
      let last='';try{last=sessionStorage.getItem('reloadedFor')||''}catch(e){}
      if(last!==state.app_version){try{sessionStorage.setItem('reloadedFor',state.app_version)}catch(e){}location.reload();return}
    }
    if(pendingFolders&&Object.keys(pendingFolders).every(k=>state.folders?.[k+'_path']===pendingFolders[k])){
      pendingFolders=null;rulesBusy=false;rulesMessage='媒体目录已切换';renderRules();
    }
    if(pendingFolders&&['rolled_back','failed'].includes((state.upgrade||{}).status)&&String(state.upgrade.upgrade_id||'').endsWith('-folders')){
      pendingFolders=null;rulesBusy=false;rulesMessage=state.upgrade.message||'切换没有成功，已恢复原样';renderRules();
    }
    if(rulesData&&!$('#rulesPage').hidden&&!rulesLoading)updateRulesStatus();
    renderCounts();renderDisk();renderUpgrade();renderJob();renderRows();updateSelection();
  }catch(error){showToast('无法连接服务，正在重试','error')}
  finally{ticking=false}
}
document.querySelectorAll('.filter').forEach(btn=>btn.onclick=()=>{
  activeFilter=btn.dataset.filter;document.querySelectorAll('.filter').forEach(x=>x.classList.toggle('active',x===btn));
  renderRows();updateSelection();
});
let rulesData=null,rulesGroup=null,rulesDraft={},rulesBusy=false,rulesLoading=false,rulesMessage='',pendingFolders=null;
const ruleSections={
  '画面':['视频封装','视频流原样复制，不重新编码。'],
  '声音':['音轨处理','新增音轨排在首位并设为默认。'],
  '字幕':['字幕封装','外挂文件与影片同目录，文件名以片名开头。'],
  '默认保留':['默认选轨','转换前可在影片卡片上改选。'],
  '文件夹':['媒体目录','可选 NAS 上的任意文件夹。两个文件夹不能相同或互相包含。保存后面板重启。'],
  '环境':['运行环境','面板依赖的工具和服务。'],
  '说明':['使用说明','']
};
function ruleNow(row){return {...row,...rulesDraft[row.id]}}
function editRule(row,key,value){
  const dirty={...rulesDraft[row.id]};
  if(value===row[key])delete dirty[key];else dirty[key]=value;
  if(Object.keys(dirty).length)rulesDraft[row.id]=dirty;else delete rulesDraft[row.id];
  rulesMessage='';
}
function updateRulesStatus(){
  const count=Object.keys(rulesDraft).length,moving=Object.keys(rulesDraft).some(key=>key.startsWith('folder.'));
  const status=$('#rulesStatus');
  let issue='';
  if(count&&busy())issue='任务进行中，队列清空后可保存';
  if(moving){
    const paths=rulesData.rows.filter(r=>r.note_fixed).map(r=>ruleNow(r).action+'/');
    if(paths[0].startsWith(paths[1])||paths[1].startsWith(paths[0]))issue='下载目录和影片目录须独立，不能相同或相互包含';
  }
  status.textContent=rulesMessage||(count?count+' 项待保存'+(issue?' · '+issue:(moving?' · 保存后将重启面板':'')):'已保存 · 用于后续任务');
  status.classList.toggle('pending',!!count);
  $('#rulesSave').disabled=!count||rulesBusy||!!issue;
  $('#rulesDiscard').hidden=!count;$('#rulesDiscard').disabled=rulesBusy;
  const reset=$('#rulesReset');
  if(reset)reset.disabled=rulesBusy||!rulesData.rows.some(r=>r.group===rulesGroup&&!r.note_fixed&&!r.note_only&&ruleNow(r).action!==r.default);
  $('#rulesNav').querySelectorAll('[data-group]').forEach(btn=>{
    const n=rulesData.rows.filter(r=>r.group===btn.dataset.group&&rulesDraft[r.id]).length;
    btn.querySelector('small').textContent=n?'待存 '+n:'';
  });
}
function ruleSelect(r){
  const action=ruleNow(r).action;
  return '<select id="s-'+esc(r.id)+'" data-rule="'+esc(r.id)+'" aria-label="'+esc(r.label)+'"'+(rulesBusy?' disabled':'')+'>'+
    r.options.map(o=>'<option value="'+esc(o.value)+'"'+(o.value===action?' selected':'')+'>'+esc(o.label)+'</option>').join('')+'</select>';
}
function ruleTable(headers,rows){
  return '<div class="rule-matrix-wrap'+(headers.length===4?' rule-codecs':'')+'"><table class="rule-matrix'+(headers.length===5?' rule-five':'')+'"><thead><tr>'+headers.map(h=>'<th scope="col">'+esc(h)+'</th>').join('')+
    '</tr></thead><tbody>'+rows.map(cells=>'<tr><th scope="row">'+cells[0]+'</th>'+cells.slice(1).map((c,i)=>'<td data-label="'+esc(headers[i+1])+'">'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
// 一项设置对应一行或两行结果：名称、选择框、备注跨行，成品格式和结果一行一个
function splitTable(headers,groups){
  return '<div class="rule-matrix-wrap"><table class="rule-matrix rule-split"><thead><tr>'+headers.map(h=>'<th scope="col">'+esc(h)+'</th>').join('')+
    '</tr></thead><tbody>'+groups.map(g=>g.lines.map((line,i)=>'<tr class="'+(i===0?'group-first':'group-more')+'">'+
      (i===0?'<th scope="row" rowspan="'+g.lines.length+'">'+g.name+'</th><td rowspan="'+g.lines.length+'">'+g.control+'</td>':'')+
      '<td>'+esc(line[0])+'</td><td>'+esc(line[1])+'</td>'+
      (i===0?'<td rowspan="'+g.lines.length+'">'+g.note+'</td>':'')+'</tr>').join('')).join('')+'</tbody></table></div>';
}
// 选文件夹：一层一层点进去，点“选这个文件夹”确定
let folderFor=null,folderAt='';
const FOLDER_ICON='<svg class="folder-icon" viewBox="0 0 20 16" aria-hidden="true"><path d="M1 3.2C1 2 2 1 3.2 1h3.9c.6 0 1.1.2 1.5.6l1 1h7.2C18 2.6 19 3.6 19 4.8v8C19 14 18 15 16.800 15H3.2C2 15 1 14 1 12.800z" fill="currentColor"/></svg>';
async function showFolder(path){
  $('#folderList').innerHTML='<div class="subtitle-list-status">正在读取…</div>';
  try{
    const r=await fetch('/api/folder_list?path='+encodeURIComponent(path)),res=await r.json();
    if(!r.ok)throw new Error(res.error||'读取失败');
    folderAt=res.path;
    const crumbs=res.path.split('/').filter(Boolean);
    $('#folderPath').innerHTML='<button type="button" data-enter="/">NAS</button>'+crumbs.map((name,i)=>
      '<i>›</i><button type="button" data-enter="/'+esc(crumbs.slice(0,i+1).join('/'))+'">'+esc(name)+'</button>').join('');
    $('#folderUp').disabled=res.parent===null;$('#folderUp').dataset.parent=res.parent||'';
    $('#folderChoose').disabled=res.path.split('/').filter(Boolean).length<2;
    $('#folderList').innerHTML=res.folders.map(name=>'<button class="subtitle-choice" type="button" data-enter="'+esc((res.path==='/'?'':res.path)+'/'+name)+'">'+FOLDER_ICON+'<strong>'+esc(name)+'</strong><em>›</em></button>').join('')||
      '<div class="subtitle-list-status">没有子文件夹</div>';
  }catch(error){$('#folderList').innerHTML='<div class="subtitle-list-status">'+esc(error.message)+'</div>'}
}
function openFolderPicker(id){
  folderFor=id;const now=ruleNow(rulesData.rows.find(r=>r.id===id)).action;
  $('#folderModal').classList.add('open');showFolder(now.split('/').slice(0,-1).join('/')||'/');
}
function closeFolderPicker(){$('#folderModal').classList.remove('open');folderFor=null}
function ruleReason(r){
  if(r.note_fixed)return '';
  return '<label class="rule-why"><b>备注</b><textarea class="rule-note" rows="1" data-note="'+esc(r.id)+'" maxlength="300"'+(rulesBusy?' disabled':'')+' aria-label="'+esc(r.label)+'的备注">'+esc(ruleNow(r).note)+'</textarea></label>';
}
function noteInput(id,fallback,label){
  const row=rulesData.rows.find(r=>r.id===id),draft=rulesDraft[id];
  const value=draft&&'note' in draft?draft.note:(row&&(row.has_note||!row.note_only)?row.note:fallback);
  return '<textarea class="rule-note table-note" rows="1" data-note="'+esc(id)+'" data-fallback="'+esc(fallback||'')+'" maxlength="300"'+
    (rulesBusy?' disabled':'')+' aria-label="'+esc(label)+'的备注" placeholder="备注">'+esc(value||'')+'</textarea>';
}
function ruleField(r){
  const description=r.description||(r.note_fixed?r.note:'');
  const control=r.note_fixed?'<div class="folder-pick"><span class="folder-now">'+esc(ruleNow(r).action)+'</span>'+
    '<button class="ghost" id="s-'+esc(r.id)+'" data-folder="'+esc(r.id)+'" type="button"'+(rulesBusy?' disabled':'')+'>选择文件夹</button></div>':ruleSelect(r);
  return '<div class="rule-field"><div><label for="s-'+esc(r.id)+'">'+esc(r.label)+'</label>'+
    (description?'<p class="rule-description">'+esc(description)+'</p>':'')+ruleReason(r)+'</div>'+control+'</div>';
}
function rulePreview(rows){
  const current=id=>ruleNow(rulesData.rows.find(r=>r.id===id)).action;
  if(rulesGroup==='画面'){
    const outputs=rulesData.video_outputs[current('video.dv')][current('video.dv7')];
    const table=(headers,data)=>ruleTable(headers,data.map(c=>[esc(c[0]),esc(c[1]),
      '<span class="'+(c[2]==='转换'?'rule-do':'rule-skip')+'">'+esc(c[2])+'</span>',esc(c[3]),noteInput(c[5],c[4],c[0]+' '+c[1])]));
    return '<h3>文件格式</h3>'+table(['片源','文件格式','处理','成品格式','备注'],outputs)+
      '<h3>画面编码</h3>'+table(['片源','画面编码','处理','成品编码','备注'],rulesData.video_codecs);
  }
  if(rulesGroup==='声音'||rulesGroup==='字幕'){
    const groups=rows.filter(r=>!r.note_only).map(r=>{
      const out=r.options.find(o=>o.value===ruleNow(r).action).outputs;
      return {name:esc(r.label),control:ruleSelect(r),note:noteInput(r.id,r.reason,r.label),
        lines:out.mkv===out.mp4?[['MKV、MP4',out.mkv]]:[['MKV',out.mkv],['MP4',out.mp4]]};
    });
    if(rulesGroup==='字幕')for(const c of rulesData.subtitle_fixed)
      groups.push({name:esc(c[0]),control:'<span class="rule-skip">'+esc(c[1])+'</span>',note:noteInput(c[5],c[4],c[0]),lines:[['MKV、MP4',c[2]]]});
    const sound=rulesGroup==='声音';
    return splitTable([sound?'源音轨编码':'源字幕','处理','成品格式',sound?'成品音轨':'成品字幕','备注'],groups)+
      (sound?'<p class="rules-caption">新增音轨最多 5.1 声道。E-AC-3 1536 kb/s，AC-3 640 kb/s。</p>':'');
  }
  if(rulesGroup==='文件夹'){
    return '';
  }
  const audio=current('pick.audio'),subs=current('pick.subs');
  const example=rulesData.selection_example;
  return '<h3>选轨示例</h3>'+ruleTable(['轨道','示例片源中的轨道','当前规则预选'],[
    ['音轨',esc(example.audio_source),esc(example.audio[audio])],
    ['字幕',esc(example.subs_source),esc(example.subs[subs])]]);
}
function renderRuleContent(){
  document.querySelector('.rules-foot').hidden=[ENV_GROUP,HELP_GROUP].includes(rulesGroup);   // 这两页只看不改，不显示保存栏
  if(rulesGroup===ENV_GROUP){renderEnvironment();return}
  if(rulesGroup===HELP_GROUP){renderHelp();return}
  const rows=rulesData.rows.filter(r=>r.group===rulesGroup),[title,description]=ruleSections[rulesGroup];
  const matrix=rulesGroup==='声音'||rulesGroup==='字幕';
  const notes=rows.filter(r=>!r.note_fixed&&!r.note_only);
  $('#rulesBody').innerHTML='<h2>'+esc(title)+'</h2><p class="rules-intro">'+esc(description)+'</p>'+
    (matrix?'':rows.filter(r=>!r.note_only).map(ruleField).join(''))+rulePreview(rows)+
    (notes.length?'<button class="ghost rules-reset" type="button" id="rulesReset">恢复本页默认设置</button>':'');
  const reset=$('#rulesReset');
  if(reset)reset.onclick=()=>{for(const r of notes)editRule(r,'action',r.default);renderRuleContent();updateRulesStatus()};
  $('#rulesBody').querySelectorAll('textarea.rule-note').forEach(fitNote);
}
// 备注框按内容长高，整段都看得见
function fitNote(el){el.style.height='auto';el.style.height=(el.scrollHeight+2)+'px'}
// 运行环境：面板依赖的工具和服务在不在。只看不改，所以不走“保存设置”那一套
async function renderEnvironment(){
  const body=$('#rulesBody');
  body.innerHTML='<h2>运行环境</h2><p class="rules-intro">面板依赖的工具和服务。</p><div class="subtitle-list-status">正在检查…</div>';
  try{
    const r=await fetch('/api/selfcheck'),res=await r.json();
    if(!r.ok)throw new Error(res.error||'检查失败');
    if(rulesGroup!==ENV_GROUP)return;
    const mark={ok:['env-ok','正常'],missing:['env-bad','缺失'],optional:['env-warn','不可用']};
    body.innerHTML='<h2>运行环境</h2><p class="rules-intro">面板依赖的工具和服务。</p><div class="rule-matrix-wrap"><table class="rule-matrix env-table"><thead><tr>'+
      '<th scope="col">项目</th><th scope="col">状态</th><th scope="col">详情</th></tr></thead><tbody>'+
      res.checks.map(c=>'<tr><th scope="row">'+esc(c.name)+'</th><td><span class="'+mark[c.state][0]+'">'+mark[c.state][1]+'</span></td><td data-no-translate>'+esc(c.detail)+'</td></tr>').join('')+
      '</tbody></table></div>';
  }catch(error){if(rulesGroup===ENV_GROUP)body.querySelector('.subtitle-list-status').textContent=error.message}
}
const ENV_GROUP='环境',HELP_GROUP='说明';
// 使用说明：写在面板里，只装了镜像、没看过项目主页的人也能看到
const HELP_STEPS=[
  ['下载','把影片下载到“下载文件夹”。下载完成后，“影片”页自动出现一张卡片。'],
  ['看卡片','右上角是结论：必须转换、建议转换或不用转换。中间三块是现在直接播会怎样、转换后会怎样、转换的代价。'],
  ['选音轨和字幕','卡片下方已按“默认选轨”预选好，一般不用改。'],
  ['转换','点“排队转换”，成品放进“影片文件夹”。不用转换的点“直接入库”。'],
  ['在电视上确认','等 Jellyfin 扫描到成品，在电视上播放，确认画面、声音、字幕正常。'],
  ['删除源文件','确认无误后，在卡片上点“删除源文件”。删除后无法恢复。成品有问题时点“删除成品重做”，源文件保留。']
];
function renderHelp(){
  $('#rulesBody').innerHTML='<h2>使用说明</h2><p class="rules-intro">Jellyfin 的媒体库指向“影片文件夹”；下载工具的保存位置设为“下载文件夹”。两个文件夹在“媒体目录”里查看和修改。</p>'+
    '<ol class="help-steps">'+HELP_STEPS.map(([title,text])=>'<li><b>'+title+'</b><span>'+text+'</span></li>').join('')+'</ol>'+
    '<h3>入库之后</h3><p class="rules-caption">卡片上可以改名、搜索或上传中文字幕、校准字幕时间。字幕对不上时点“校准时间”，可以反复校准。</p>'+
    '<h3>电视播放不正常</h3><p class="rules-caption">默认规则来自 LG G5 的实测，其他电视可能不同。到“视频封装”“音轨处理”“字幕封装”里改规则，再“删除成品重做”。</p>'+
    (state.help_url?'<p class="rules-caption"><a href="'+esc(state.help_url)+'" target="_blank" rel="noopener">完整说明（安装、升级、常见问题）</a></p>':'');
}
function renderRules(){
  const groups=rulesData.groups.concat(ENV_GROUP,HELP_GROUP);
  if(!groups.includes(rulesGroup))rulesGroup=groups[0];
  $('#rulesNav').innerHTML=groups.map(g=>'<button type="button" data-group="'+esc(g)+'"'+
    (g===rulesGroup?' class="active" aria-current="page"':'')+'>'+esc(ruleSections[g][0])+'<small></small></button>').join('');
  renderRuleContent();updateRulesStatus();
}
$('#rulesNav').onclick=e=>{
  const btn=e.target.closest('[data-group]');if(!btn||rulesBusy||rulesLoading)return;
  rulesGroup=btn.dataset.group;renderRules();
  const body=$('#rulesBody');body.classList.remove('swap');void body.offsetWidth;body.classList.add('swap');
  $('#rulesNav').querySelector('[aria-current]').focus();
};
$('#rulesBody').onchange=e=>{
  if(!e.target.dataset.rule||rulesBusy)return;
  const id=e.target.dataset.rule,row=rulesData.rows.find(r=>r.id===id);
  editRule(row,'action',row.note_fixed?e.target.value.trim().split('/').filter(Boolean).join('/'):e.target.value);renderRuleContent();updateRulesStatus();
  document.getElementById('s-'+id).focus();
};
$('#rulesBody').oninput=e=>{
  if(!e.target.dataset.note||rulesBusy)return;
  fitNote(e.target);
  const row=rulesData.rows.find(r=>r.id===e.target.dataset.note);
  if(row.note_only&&!row.has_note){
    if(e.target.value===e.target.dataset.fallback)delete rulesDraft[row.id];else rulesDraft[row.id]={note:e.target.value};
    rulesMessage='';
  }else editRule(row,'note',e.target.value);
  updateRulesStatus();
};
async function loadRules(){
  if(rulesLoading||rulesBusy)return;
  if(rulesData&&Object.keys(rulesDraft).length){renderRules();return}
  rulesLoading=true;$('#rulesBody').textContent='正在读取设置…';
  try{const response=await fetch('/api/rules'),data=await response.json();
    if(!response.ok)throw new Error(data.error||'读取设置失败');
    rulesMessage='';rulesData=data;renderRules();
  }catch(error){
    rulesData=null;$('#rulesNav').innerHTML='';$('#rulesStatus').textContent='';$('#rulesSave').disabled=true;
    $('#rulesBody').innerHTML='<p>'+esc(error.message||'读取设置失败')+'</p><button class="ghost" type="button" id="rulesRetry">重新读取</button>';
    $('#rulesRetry').onclick=loadRules;
  }finally{rulesLoading=false}
}
function showPage(){
  const rules=location.hash==='#rules';
  $('#rulesPage').hidden=!rules;$('#libraryPage').hidden=rules;
  $('#navRules').classList.toggle('active',rules);$('#navMovies').classList.toggle('active',!rules);
  $('#pagesNav').classList.toggle('on-rules',rules);
  if(rules)loadRules();
}
window.addEventListener('hashchange',showPage);
async function saveRules(){
  if(!rulesData||rulesBusy||!Object.keys(rulesDraft).length)return;
  rulesBusy=true;rulesMessage='正在保存…';renderRules();
  try{
    const response=await fetch('/api/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules:rulesDraft})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'保存失败');
    rulesData=data;rulesDraft={};rulesMessage=data.restarting?'已保存 · 正在切换目录':'设置已保存 · 用于后续任务';
    if(data.restarting)pendingFolders=Object.fromEntries(data.rows.filter(r=>r.note_fixed).map(r=>[r.id.slice(7),r.action]));
    await tick();
  }catch(error){rulesMessage=error.message||'保存失败，请重试'}
  finally{rulesBusy=!!pendingFolders;renderRules()}
}
$('#rulesBody').addEventListener('click',e=>{const b=e.target.closest('[data-folder]');if(b&&!rulesBusy)openFolderPicker(b.dataset.folder)});
$('#folderList').onclick=$('#folderPath').onclick=e=>{const b=e.target.closest('[data-enter]');if(b)showFolder(b.dataset.enter)};
$('#folderUp').onclick=()=>showFolder($('#folderUp').dataset.parent);
$('#folderCancel').onclick=closeFolderPicker;
$('#folderModal').onclick=e=>{if(e.target===$('#folderModal'))closeFolderPicker()};
$('#folderChoose').onclick=()=>{
  if(!folderFor||!folderAt)return;
  editRule(rulesData.rows.find(r=>r.id===folderFor),'action',folderAt);closeFolderPicker();renderRuleContent();updateRulesStatus();
};
$('#rulesSave').onclick=saveRules;
$('#rulesDiscard').onclick=()=>{rulesDraft={};rulesMessage='';renderRules()};
showPage();
$('#jellyfinLink').href=location.protocol+'//'+location.hostname+':8096';     // 状态读到后按实际地址更新
$('#deleteCancel').onclick=closeDelete;
$('#deleteModal').onclick=e=>{if(e.target===$('#deleteModal'))closeDelete()};
$('#rebuildCancel').onclick=closeRebuild;
$('#rebuildModal').onclick=e=>{if(e.target===$('#rebuildModal'))closeRebuild()};
$('#subtitlePickerCancel').onclick=closeSubtitlePicker;
$('#subtitlePickerModal').onclick=e=>{if(e.target===$('#subtitlePickerModal'))closeSubtitlePicker()};
$('#subtitleLocal').onclick=()=>{if(pendingSubtitle)chooseLocalSubtitle(pendingSubtitle)};
$('#subtitleSyncCancel').onclick=closeSubtitleSyncConfirm;
$('#subtitleSyncModal').onclick=e=>{if(e.target===$('#subtitleSyncModal'))closeSubtitleSyncConfirm()};
$('#subtitleSyncManual').oninput=renderSubtitleSyncPreview;
document.querySelectorAll('[data-sync-step]').forEach(btn=>btn.onclick=()=>{
  const current=parseSubtitleClock($('#subtitleSyncManual').value);
  if(current===null){renderSubtitleSyncPreview();return}
  $('#subtitleSyncManual').value=subtitleClock(Math.max(0,
    current+Number(btn.dataset.syncStep)*1000));
  renderSubtitleSyncPreview();
});
$('#subtitleSyncConfirm').onclick=async()=>{
  if(!pendingSyncConfirm)return;
  const firstStart=parseSubtitleClock($('#subtitleSyncManual').value);
  if(firstStart===null){
    renderSubtitleSyncPreview();return;
  }
  const action=pendingSyncConfirm.action;pendingSyncConfirm=null;
  $('#subtitleSyncModal').classList.remove('open');
  await action(firstStart);
};
async function checkLatestVersion(){
  const box=$('#upgradeLatest');box.textContent='正在检查新版本…';box.className='sub-note';
  try{
    const r=await fetch('/api/latest_version'),res=await r.json();
    if(!r.ok)throw new Error(res.error||'查不到新版本');
    box.innerHTML=res.newer?'有新版本 <span data-no-translate>'+esc(res.latest)+'</span> · <a href="'+esc(res.url)+'" target="_blank" rel="noopener">查看</a>':'已是最新版本';
    box.classList.toggle('env-warn',!!res.newer);
  }catch(error){box.textContent=error.message}
}
$('#upgradeOpen').onclick=()=>{
  checkLatestVersion();
  $('#upgradeModal').classList.add('open');$('#upgradeCancel').focus();renderUpgrade();
};
function closeUpgrade(){$('#upgradeModal').classList.remove('open')}
$('#upgradeCancel').onclick=closeUpgrade;
$('#upgradeModal').onclick=e=>{if(e.target===$('#upgradeModal'))closeUpgrade()};
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  if($('#renameModal').classList.contains('open'))closeRename();
  if($('#deleteModal').classList.contains('open'))closeDelete();
  if($('#rebuildModal').classList.contains('open'))closeRebuild();
  if($('#subtitlePickerModal').classList.contains('open'))closeSubtitlePicker();
  if($('#subtitleSyncModal').classList.contains('open'))closeSubtitleSyncConfirm();
  if($('#upgradeModal').classList.contains('open'))closeUpgrade();
});
$('#deleteConfirm').onclick=async()=>{
  if(!pendingDelete)return;const item=pendingDelete;const dBtn=$('#deleteConfirm'),dText=dBtn.textContent;
  dBtn.disabled=true;dBtn.textContent='正在删除…';
  try{
    const r=await fetch(pendingDiscard?'/api/discard_source':'/api/delete_source',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:item.name})});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'删除失败');
    sel.delete(item.name);closeDelete();showToast('已删除，回收 '+item.size_h);await tick();
  }catch(error){showToast(error.message||'删除失败','error')}
  finally{dBtn.disabled=false;dBtn.textContent=dText}
};
$('#renameCancel').onclick=closeRename;$('#renameConfirm').onclick=confirmRename;
$('#renameInput').onkeydown=e=>{if(e.key==='Enter')confirmRename()};
$('#renameModal').onclick=e=>{if(e.target===$('#renameModal'))closeRename()};
$('#rebuildConfirm').onclick=async()=>{
  if(!pendingRebuild)return;const item=pendingRebuild;const rBtn=$('#rebuildConfirm'),rText=rBtn.textContent;
  rBtn.disabled=true;rBtn.textContent='正在删除…';
  try{
    const r=await fetch('/api/rebuild',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:item.name})});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'删除成品失败');
    closeRebuild();showToast(res.message||'已删除当前成品并恢复待处理');await tick();
  }catch(error){showToast(error.message||'删除成品失败','error')}
  finally{rBtn.disabled=false;rBtn.textContent=rText}
};
$('#upgradeConfirm').onclick=async()=>{
  const appFile=$('#upgradeApp').files[0];
  if(!appFile){showToast('请选择新版 app.py','warn');return}
  const data=new FormData();data.append('app_py',appFile);
  $('#upgradeConfirm').disabled=true;
  try{
    const r=await fetch('/api/upgrade',{method:'POST',body:data});
    const res=await r.json();if(!r.ok)throw new Error(res.error||'升级启动失败');
    closeUpgrade();showToast(res.message||'已开始后台升级');await tick();
  }catch(error){showToast(error.message||'升级启动失败','error')}
  finally{$('#upgradeConfirm').disabled=false}
};
// 英语界面：按后台给的中英对照表替换页面上的文字。整句优先，其次带数字的句式，再按片段替换（长的优先）。
const EN="__EN_TEXT__",EN_PAT="__EN_PATTERNS__";
const EN_KEYS=typeof EN==='object'?Object.keys(EN).sort((a,b)=>b.length-a.length):[];
const EN_RE=typeof EN_PAT==='object'?EN_PAT.map(([re,to])=>[new RegExp(re,'g'),to]):[];
const HAS_ZH=/[、。㐀-鿿＀-￯]/;
function toEnglish(text){
  if(!text||!HAS_ZH.test(text))return text;
  const core=text.trim();
  if(Object.prototype.hasOwnProperty.call(EN,core))return text.replace(core,EN[core]);
  const out=englishParts(text);
  // 只翻出一半的句子不如整句保留中文
  return HAS_ZH.test(out)?text:out;
}
function englishParts(text){
  let out=text;
  for(const [re,to] of EN_RE)out=out.replace(re,to);
  for(const key of EN_KEYS)if(out.includes(key))out=out.split(key).join(EN[key]);
  return out.replace(/，/g,', ').replace(/。/g,'. ').replace(/：/g,': ').replace(/；/g,'; ').replace(/（/g,' (').replace(/）/g,')')
    .replace(/、/g,', ').replace(/[“”]/g,'"').replace(/ +/g,' ');
}
function currentLang(){let l='';try{l=localStorage.getItem('lang')||''}catch(e){}return l==='en'?'en':'zh'}
const TRANSLATED=new WeakMap();
function translateNode(node){
  if(node.nodeType===3){
    const parent=node.parentElement;
    if(!parent||parent.closest('script,style,pre,.mc-name,.folder-now,[data-no-translate]'))return;
    if(parent.tagName==='TEXTAREA')return;
    if(TRANSLATED.get(node)===node.nodeValue)return;
    const en=toEnglish(node.nodeValue);if(en!==node.nodeValue)node.nodeValue=en;TRANSLATED.set(node,en);return;
  }
  if(node.nodeType!==1||node.closest('script,style,pre'))return;
  for(const attr of ['placeholder','aria-label','title'])if(node.hasAttribute&&node.hasAttribute(attr)){
    const value=node.getAttribute(attr),en=toEnglish(value);if(en!==value)node.setAttribute(attr,en);
  }
  // 备注框里如果还是程序给的默认说明，就显示英语；用户自己写的不动
  if(node.tagName==='TEXTAREA'&&node.classList.contains('rule-note')&&Object.prototype.hasOwnProperty.call(EN,node.value.trim())){
    node.value=EN[node.value.trim()];if(typeof fitNote==='function')fitNote(node);
  }
  for(const child of node.childNodes)translateNode(child);
}
function startEnglish(){
  document.documentElement.lang='en';document.title=toEnglish(document.title);
  translateNode(document.body);
  new MutationObserver(list=>{for(const m of list){
    if(m.type==='characterData')translateNode(m.target);
    else if(m.type==='attributes')translateNode(m.target);
    else for(const node of m.addedNodes)translateNode(node);
  }}).observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['placeholder','aria-label','title']});
}
$('#langToggle').textContent=currentLang()==='en'?'中':'EN';
$('#langToggle').onclick=()=>{try{localStorage.setItem('lang',currentLang()==='en'?'zh':'en')}catch(e){}location.reload()};
if(currentLang()==='en')startEnglish();
// 深色 / 浅色：记在浏览器里；没选过就跟系统
function currentTheme(){let t='';try{t=localStorage.getItem('theme')||''}catch(e){}
  return t||(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches?'light':'dark')}
function applyTheme(){
  const light=currentTheme()==='light';document.documentElement.dataset.theme=light?'light':'dark';
  document.querySelector('meta[name=theme-color]').content=light?'#f5f5f7':'#000000';
  // “only”：告诉浏览器这一页的深浅色由页面自己管，系统是深色外观时也不要自动把浅色页面调暗
  document.querySelector('meta[name=color-scheme]').content=light?'only light':'only dark';
}
$('#themeToggle').onclick=()=>{try{localStorage.setItem('theme',currentTheme()==='light'?'dark':'light')}catch(e){}applyTheme()};
applyTheme();
tick();setInterval(tick,2500);
</script></body></html>"""


APP_RELEASE = "0.10.3"        # 给人看的版本号：改了功能就加；下面的指纹是这份文件内容算出来的，用来确认装上的就是这一份


def _app_version():
    """本程序文件内容的指纹。页面用它发现“服务已经升级，而我还是旧页面”。"""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except OSError:
        return "unknown"


APP_VERSION = _app_version()


@app.get("/")
def index():
    return (PAGE.replace("__APP_VERSION__", APP_VERSION)
            .replace('"__EN_TEXT__"', json.dumps(EN_TEXT, ensure_ascii=False).replace("</", "<\\/"))
            .replace('"__EN_PATTERNS__"', json.dumps(EN_PATTERNS, ensure_ascii=False).replace("</", "<\\/")))


if __name__ == "__main__":
    load_state()
    load_disc_preview_cache()
    recover_interrupted_upgrade()
    for d in (INBOX, WORK, MOVIES):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=disc_preview_loop, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=ensure_makemkv_sees_inbox, daemon=True).start()
    app.run(host="0.0.0.0", port=PANEL_PORT, threaded=True)
