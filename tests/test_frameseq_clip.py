# -*- coding: utf-8 -*-
"""FrameSeqClip + MovieLibrary 帧序列接线 offscreen 单测。

覆盖（帧序列化 B 档）：
- FrameSeqClip 播放语义：start 首帧即上屏、逐帧推进、末帧 finished、
  jumpToFrame、空目录 errorOccurred + start False、alpha 通道存活；
- MovieLibrary.movie() 的 frameseq 优先接线：有 frameseq 目录 →
  FrameSeqClip；无 → 现 webm 路径（WebMClip）。

纪律（AGENTS.md 时序测试）：不启动真实播放定时器，同步直调
start/_advance/jumpToFrame；帧素材用 Qt 现场生成的 webp（不依赖
repo 素材与 ffmpeg）。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from pet.frameseq_clip import FrameSeqClip
from pet.library import MovieLibrary

app = QApplication.instance() or QApplication([])


def _make_frames(dir_path: Path, count: int = 4, size: tuple[int, int] = (32, 24)) -> None:
    """Qt 现场生成 count 帧带 alpha 图案的 webp + meta.json。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    w, h = size
    for i in range(count):
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        # 左上 4x4 不透明块（颜色随帧号变），其余透明——验证 alpha 与帧区分
        for y in range(4):
            for x in range(4):
                img.setPixel(x, y, (0xFF << 24) | ((i * 60) << 16) | 0x3366)
        assert img.save(str(dir_path / f"f_{i + 1:04d}.webp"), "webp", 100)
    (dir_path / "meta.json").write_text(
        json.dumps({"fps": 24.0, "source": "x.webm", "frames": count}),
        encoding="utf-8")


def test_start_emits_first_frame_immediately(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d)
    clip = FrameSeqClip(d)
    hits = []
    clip.frameChanged.connect(hits.append)
    assert clip.start() is True
    assert hits == [0]                      # 首帧即上屏（无冷启动）
    assert clip.currentImage() is not None
    clip.stop()


def test_playthrough_ends_with_finished(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d, count=3)
    clip = FrameSeqClip(d)
    frames, done = [], []
    clip.frameChanged.connect(frames.append)
    clip.finished.connect(lambda: done.append(1))
    clip.start()
    clip._advance()   # → 帧 1
    clip._advance()   # → 帧 2（末帧）
    assert frames == [0, 1, 2]
    assert done == []
    clip._advance()   # 越末 → finished，停表
    assert done == [1]
    assert frames == [0, 1, 2]
    assert not clip._timer.isActive()


def test_jump_to_frame(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d, count=4)
    clip = FrameSeqClip(d)
    clip.start()
    assert clip.jumpToFrame(3) is True
    assert clip.currentFrameNumber() == 3
    assert clip.jumpToFrame(99) is True     # 钳到末帧
    assert clip.currentFrameNumber() == 3
    clip.stop()


def test_alpha_channel_survives(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d, count=2)
    clip = FrameSeqClip(d)
    clip.start()
    img = clip.currentImage()
    assert img is not None
    assert ((img.pixel(1, 1) >> 24) & 0xFF) == 0xFF    # 不透明块
    assert ((img.pixel(20, 20) >> 24) & 0xFF) == 0     # 透明区
    clip.stop()


def test_empty_dir_fails_clean(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    clip = FrameSeqClip(d)
    errs = []
    clip.errorOccurred.connect(errs.append)
    assert clip.start() is False
    assert len(errs) == 1
    assert clip.frameCount() == 1           # 空目录安全回退


def test_decode_layer_compat_noops(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d)
    clip = FrameSeqClip(d)
    clip.warm_meta()
    clip.warm_first_frame()
    assert clip.currentImage() is not None  # warm 装载第 0 帧
    clip.cancel_first_frame_warm()
    assert clip.decode_throttle_divisor() == 1
    assert clip.decode_pace_external() is False
    clip.set_decode_pace_external(True)
    clip.set_decode_throttle(2)
    clip.set_recycle_minutes(5)
    clip.clear_display_frame()
    assert clip.currentImage() is None


# ---------------------------------------------------------------- MovieLibrary 接线
def _make_pack(tmp_path: Path) -> Path:
    """临时角色包：videos/idle/x.webm（伪造占位）+ frameseq/idle/x/。"""
    videos = tmp_path / "videos"
    (videos / "idle").mkdir(parents=True)
    (videos / "idle" / "x.webm").write_bytes(b"placeholder")
    _make_frames(tmp_path / "frameseq" / "idle" / "x")
    return videos


def test_library_prefers_frameseq_clip(tmp_path):
    videos = _make_pack(tmp_path)
    lib = MovieLibrary(character_id="shenshen", asset_dir=videos,
                       prewarm_enabled=False)
    assert "x" in lib._frameseq_dirs
    clip = lib.movie("x")
    assert isinstance(clip, FrameSeqClip)
    assert clip.frameCount() == 4


def test_library_without_frameseq_keeps_webm_path(tmp_path):
    videos = tmp_path / "videos"
    (videos / "idle").mkdir(parents=True)
    (videos / "idle" / "x.webm").write_bytes(b"placeholder")
    lib = MovieLibrary(character_id="shenshen", asset_dir=videos,
                       prewarm_enabled=False)
    assert lib._frameseq_dirs == {}
    clip = lib.movie("x")
    from pet.webm_clip import WebMClip
    assert isinstance(clip, WebMClip)       # 现路径逐行不变
