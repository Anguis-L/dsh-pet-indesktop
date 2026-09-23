# -*- coding: utf-8 -*-
"""FrameSeqClip + MovieLibrary 帧序列接线 offscreen 单测。

覆盖（帧序列化 B 档）：
- FrameSeqClip 播放语义：start 首帧异步交付、逐帧推进、末帧 finished、
  未到货等待不跳帧、jumpToFrame、空目录 errorOccurred + start False、
  alpha 通道存活；
- MovieLibrary.movie() 的 frameseq 优先接线：有 frameseq 目录 →
  FrameSeqClip；无 → 现 webm 路径（WebMClip）。

纪律（AGENTS.md 时序测试）：播放定时器不依赖真实走时，同步直调
start/_advance/jumpToFrame；异步预取交付用 processEvents 事件泵 +
宽预算等待（不赌固定 sleep）。帧素材用 Qt 现场生成的 webp。
"""
from __future__ import annotations

import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from pet.frameseq_clip import FrameSeqClip
from pet.library import MovieLibrary

app = QApplication.instance() or QApplication([])


def _pump_until(cond, timeout_s=3.0):
    """事件泵：等到 cond 为真（异步预取交付经 queued 信号，需要事件循环）。"""
    t0 = time.monotonic()
    while not cond():
        QApplication.processEvents()
        if time.monotonic() - t0 > timeout_s:
            raise AssertionError("事件泵超时：异步交付未到达")
        time.sleep(0.005)


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


def test_start_delivers_first_frame_async(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d)
    clip = FrameSeqClip(d)
    hits = []
    clip.frameChanged.connect(hits.append)
    try:
        assert clip.start() is True
        _pump_until(lambda: hits == [0])       # 首帧异步交付（~2.5ms，无冷启动）
        assert clip.currentImage() is not None
    finally:
        clip.close()


def test_playthrough_ends_with_finished(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d, count=3)
    clip = FrameSeqClip(d)
    frames, done = [], []
    clip.frameChanged.connect(frames.append)
    clip.finished.connect(lambda: done.append(1))
    try:
        clip.start()
        _pump_until(lambda: frames == [0])
        clip._advance()                        # 请求帧 1，异步到货
        _pump_until(lambda: frames == [0, 1])
        clip._advance()                        # → 帧 2（末帧）
        _pump_until(lambda: frames == [0, 1, 2])
        assert done == []
        clip._advance()                        # 越末 → finished，停表
        assert done == [1]
        assert frames == [0, 1, 2]
        assert not clip._timer.isActive()
    finally:
        clip.close()


def test_advance_waits_without_frame_skip(tmp_path):
    """未到货等待语义：_advance 在帧未就绪时不跳帧、不上屏旧帧号。"""
    d = tmp_path / "clip"
    _make_frames(d, count=3)
    clip = FrameSeqClip(d)
    frames = []
    clip.frameChanged.connect(frames.append)
    try:
        clip.start()
        _pump_until(lambda: frames == [0])
        clip._awaiting = -1
        clip._pending.clear()                  # 制造"未预取"状态
        clip._advance()                        # 未到货：只登记 awaiting，不上屏
        assert frames == [0]
        assert clip._awaiting == 1
        _pump_until(lambda: frames == [0, 1])  # 到货即上屏并续播
        assert clip._awaiting == -1
    finally:
        clip.close()


def test_jump_to_frame(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d, count=4)
    clip = FrameSeqClip(d)
    try:
        clip.start()
        assert clip.jumpToFrame(3) is True
        assert clip.currentFrameNumber() == 3
        assert clip.currentImage() is not None  # 低频同步路径立即生效
        assert clip.jumpToFrame(99) is True     # 钳到末帧
        assert clip.currentFrameNumber() == 3
    finally:
        clip.close()


def test_alpha_channel_survives(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d, count=2)
    clip = FrameSeqClip(d)
    hits = []
    clip.frameChanged.connect(hits.append)
    try:
        clip.start()
        _pump_until(lambda: hits == [0])
        img = clip.currentImage()
        assert img is not None
        assert ((img.pixel(1, 1) >> 24) & 0xFF) == 0xFF    # 不透明块
        assert ((img.pixel(20, 20) >> 24) & 0xFF) == 0     # 透明区
    finally:
        clip.close()


def test_empty_dir_fails_clean(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    clip = FrameSeqClip(d)
    errs = []
    clip.errorOccurred.connect(errs.append)
    try:
        assert clip.start() is False
        assert len(errs) == 1
        assert clip.frameCount() == 1           # 空目录安全回退
    finally:
        clip.close()


def test_decode_layer_compat_noops(tmp_path):
    d = tmp_path / "clip"
    _make_frames(d)
    clip = FrameSeqClip(d)
    try:
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
    finally:
        clip.close()


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
    try:
        assert "x" in lib._frameseq_dirs
        clip = lib.movie("x")
        assert isinstance(clip, FrameSeqClip)
        assert clip.frameCount() == 4
    finally:
        for m in lib.movies().values():
            close = getattr(m, "close", None)
            if callable(close):
                close()


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
