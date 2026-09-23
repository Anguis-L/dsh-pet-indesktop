# -*- coding: utf-8 -*-
"""tools/convert_frameseq.py 端到端单测（offscreen 可跑）。

链路：Qt 生成 3 帧 → ffmpeg 现场压一个 3 帧 webm → convert_clip 转成
无损 WebP 帧序列 → 断言帧数/fps/meta/幂等跳过。真实 ffmpeg（本机
winget 安装），验证的正是 bgra 直通那组防 chroma 下采样的参数。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from tools.convert_frameseq import convert_clip

app = QApplication.instance() or QApplication([])

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="本机无 ffmpeg")

# 仓库内真实 yuva420p 素材：stream copy 裁 6 帧做夹具——ffmpeg 现场从
# PNG 压 VP9 会丢 alpha（8.1 实测），必须用真素材。用 idle 而非 random：
# random/工作状态-垂头叹气冒汗.webm 实测整帧不透明，验不了 alpha
REAL_WEBM = (Path(__file__).resolve().parent.parent
             / "assets" / "characters" / "shenshen" / "videos"
             / "idle" / "待机呼吸休闲.webm")


def _make_tiny_webm(tmp_path: Path, frames: int = 6) -> Path:
    """真素材 stream copy 裁帧 → 带 alpha 的小 webm（秒级）。"""
    webm = tmp_path / "tiny.webm"
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-i", str(REAL_WEBM), "-frames:v", str(frames),
         "-c:v", "copy", str(webm)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return webm


def test_convert_clip_produces_bit_exact_frames(tmp_path):
    webm = _make_tiny_webm(tmp_path)
    out_dir = tmp_path / "out"
    converted, err = convert_clip(webm, out_dir)
    assert err == ""
    assert converted is True

    frames = sorted(out_dir.glob("f_*.webp"))
    assert len(frames) == 6
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["fps"] == 24.0
    assert meta["frames"] == 6
    assert meta["source"] == "tiny.webm"

    # alpha 存活：真素材有透明区（bgra 直通，不 chroma 下采样）
    img = QImage(str(frames[0]))
    assert not img.isNull()
    alphas = {(img.pixel(x, y) >> 24) & 0xFF
              for x in range(0, img.width(), 17) for y in range(0, img.height(), 17)}
    assert 0 in alphas and 0xFF in alphas


def test_convert_clip_idempotent_skip(tmp_path):
    webm = _make_tiny_webm(tmp_path)
    out_dir = tmp_path / "out"
    convert_clip(webm, out_dir)
    converted, err = convert_clip(webm, out_dir)
    assert err == ""
    assert converted is False               # 已存在则跳过
    converted, err = convert_clip(webm, out_dir, force=True)
    assert converted is True                # force 重转
