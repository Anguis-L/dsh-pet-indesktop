# -*- coding: utf-8 -*-
"""FrameSeqClip：无损 WebP 帧序列播放器（帧序列化 B 档，热集专用）。

与 WebMClip/GifClip 同一播放器接口（frameChanged/finished/errorOccurred
+ start/stop/jumpToFrame/currentImage/currentPixmap/frameCount/duration/
set_playback_speed/...），供 MovieLibrary 在存在帧序列素材时替换
WebMClip——热路径（idle/move/turn/click/drag ≈95% 播放时长）从此没有
ffmpeg 子进程、spawn 冷启动（60-166ms）、管道/背压/看门狗/关机残留。

素材形态（转换器 tools/convert_frameseq.py 产出）：
    assets/characters/<id>/frameseq/<folder>/<stem>/
        meta.json      {"fps": 24.0, "source": "idle/x.webm", ...}
        f_0001.webp ...（-pix_fmt bgra 直通的无损帧，与 webm 解码 bit-exact）

播放模型与现架构一致（链式一次性播放）：start() 从第 0 帧起按 fps 推进，
末帧后停表并发 finished()，循环由上层状态机重启 clip 承接。每帧 QImage
按路径即时加载（实测 ~1.6ms/帧，见 .scratch/frame-seq-feasibility），
文件加载即独立缓冲，无共享解码缓冲的跨线程别名问题；内存只驻留当前帧
（+Qt 页缓存兜底），无 reader 线程、无队列。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap

DEFAULT_FPS = 24.0


class FrameSeqClip(QObject):
    """与 WebMClip 接口兼容的帧序列 clip（链式一次性播放）。"""

    frameChanged = Signal(int)
    finished = Signal()
    errorOccurred = Signal(str)

    def __init__(self, frames_dir: Path | str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dir = Path(frames_dir)
        self._fps = DEFAULT_FPS
        try:
            meta = json.loads((self._dir / "meta.json").read_text(encoding="utf-8"))
            self._fps = float(meta.get("fps") or DEFAULT_FPS)
        except (OSError, ValueError):
            pass  # 缺 meta 按默认 fps（与包内素材 24fps 一致）
        if self._fps <= 0:
            self._fps = DEFAULT_FPS
        self._frames = sorted(self._dir.glob("f_*.webp"))
        self._cur = 0
        self._img: QImage | None = None
        self._pm: QPixmap | None = None
        self.playback_speed = 1.0
        self._running = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._advance)

    # ---------------------------------------------------------------- 元信息
    def frameCount(self) -> int:
        return max(1, len(self._frames))

    def duration(self) -> float:
        return len(self._frames) / self._fps / self.playback_speed

    def currentFrameNumber(self) -> int:
        return self._cur

    def currentTimeSeconds(self) -> float:
        frames = len(self._frames)
        if frames <= 0:
            return 0.0
        return self._cur * (self.duration() / frames)

    # ---------------------------------------------------------------- 当前帧
    def currentImage(self) -> QImage | None:
        return self._img

    def currentPixmap(self) -> QPixmap | None:
        return self._pm

    def clear_display_frame(self) -> None:
        self._img = None
        self._pm = None

    # ---------------------------------------------------------------- 播放控制
    def start(self) -> bool:
        """从第 0 帧起播（链式模型：播完发 finished，循环由上层重启）。"""
        if not self._frames:
            self.errorOccurred.emit(f"frameseq: 空素材目录 {self._dir}")
            return False
        self._timer.stop()
        self._cur = 0
        self._load(0)
        self._running = True
        self.frameChanged.emit(0)  # 首帧即上屏：无 spawn 冷启动
        self._timer.start(self._interval_ms())
        return True

    def stop(self) -> None:
        self._running = False
        self._timer.stop()

    def jumpToFrame(self, frame_index: int) -> bool:
        if not self._frames:
            return False
        frame_index = max(0, min(int(frame_index), len(self._frames) - 1))
        self._cur = frame_index
        self._load(frame_index)
        self.frameChanged.emit(frame_index)
        return True

    def set_playback_speed(self, speed: float) -> None:
        self.playback_speed = max(0.1, float(speed))
        if self._running:
            self._timer.start(self._interval_ms())

    # ---------------------------------------------------------------- 解码层兼容（本实现无解码层，全为良性 no-op）
    def warm_meta(self) -> None:
        return

    def warm_first_frame(self) -> None:
        if self._img is None and self._frames:
            self._load(0)

    def cancel_first_frame_warm(self) -> None:
        return

    def decode_throttle_divisor(self) -> int:
        return 1

    def decode_pace_external(self) -> bool:
        return False

    def set_decode_pace_external(self, _value: bool) -> None:
        return

    def set_decode_throttle(self, _divisor: int) -> None:
        return

    def set_recycle_minutes(self, _minutes: int) -> None:
        return

    # ---------------------------------------------------------------- 内部
    def _interval_ms(self) -> int:
        return max(1, round(1000.0 / self._fps / self.playback_speed))

    def _load(self, idx: int) -> None:
        img = QImage(str(self._frames[idx]))
        if img.isNull():
            return  # 坏帧：保持上一帧，不崩播放链
        self._img = img      # 文件加载即独立缓冲，clip 自有
        self._pm = QPixmap.fromImage(img)

    def _advance(self) -> None:
        nxt = self._cur + 1
        if nxt >= len(self._frames):
            # 链式一次性播放到尾：停表并发 finished（上层据此接下一个动画）
            self._running = False
            self._timer.stop()
            self.finished.emit()
            return
        self._cur = nxt
        self._load(nxt)
        self.frameChanged.emit(nxt)
