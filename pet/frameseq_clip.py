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
        f_0001.webp ...（libvpx 解码 + bgra 直通的无损帧，视觉 bit-exact）

播放模型与现架构一致（链式一次性播放）：start() 从第 0 帧起按 fps 推进，
末帧后停表并发 finished()，循环由上层状态机重启 clip 承接。

线程模型（实测驱动，2026-09-23 A/B）：逐帧 QImage 加载 ~2.5ms 若放在
GUI 定时器里，3 宠待机循环时 GUI 线程被吃掉 ~18%/核——直接顶撞
"高刷不卡顿"硬指标。因此解码全部在预取 worker 线程：GUI 定时器只消费
已到货的帧（pending 映射），未到货则等待不跳帧（同 WebMClip 语义）；
worker 常驻下一帧预取。内存只驻留当前帧 + 1~2 帧预取（+OS 页缓存），
无 reader 线程级队列。jumpToFrame/warm_first_frame 这类低频同步路径
允许一次 ~2.5ms 的同步加载（调用方期望立即生效）。
"""
from __future__ import annotations

import atexit
import json
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QMetaObject,
    QObject,
    Qt,
    Q_ARG,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage, QPixmap

DEFAULT_FPS = 24.0

# 预取线程进程级共享（每 clip 一个 QThread 的方案被实机否决：clip 销毁时
# 运行中的 QThread 触发 access violation——tests/test_move_sync.py 实崩）。
_shared_thread: QThread | None = None


def _shutdown_shared_prefetch() -> None:
    """进程/应用退出收口：停掉共享预取线程。

    不收口则解释器退出时 QApplication 先于运行中的 QThread 销毁，
    Windows 上直接 0xC0000409 fail-fast（pytest 进程尾崩实测）。幂等：
    收口后可由 _shared_prefetch_thread() 重建（测试反复建 QApplication）。
    """
    global _shared_thread
    thread, _shared_thread = _shared_thread, None
    if thread is not None and thread.isRunning():
        thread.quit()
        thread.wait(2000)


def _shared_prefetch_thread() -> QThread:
    """懒建进程级预取线程（clip 只挂 worker，不拥有线程）。"""
    global _shared_thread
    if _shared_thread is not None and not _shared_thread.isRunning():
        _shared_thread = None  # 已被退出收口：按懒建语义重建
    if _shared_thread is None:
        _shared_thread = QThread()
        _shared_thread.setObjectName("frameseq-prefetch-shared")
        _shared_thread.start()
        app = QCoreApplication.instance()
        if app is not None:
            # 生产路径：exec() 退出时 aboutToQuit 收口
            app.aboutToQuit.connect(
                _shutdown_shared_prefetch, Qt.ConnectionType.UniqueConnection)
        # 兜底：无 exec() 的上下文（pytest/脚本——QApplication 析构不发
        # aboutToQuit），atexit 在模块拆除前收口（幂等，注册一次即安）
        atexit.register(_shutdown_shared_prefetch)
    return _shared_thread


class _PrefetchWorker(QObject):
    """后台预取：按路径加载帧（~2.5ms/帧）移出 GUI 线程。

    常驻独立 QThread；交付经 queued 信号传 QImage（隐式共享 + 文件加载
    即独立缓冲，clip 侧无需再拷贝，无跨线程别名）。
    """

    loaded = Signal(int, QImage)

    def __init__(self, frames: list[Path], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._frames = frames

    @Slot(int)
    def prefetch(self, idx: int) -> None:
        if 0 <= idx < len(self._frames):
            self.loaded.emit(idx, QImage(str(self._frames[idx])))


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
        # 异步预取（GUI 零解码）：pending = 已到货未上屏，wanted = 在途请求，
        # awaiting = 播放位置在等的帧号（到货即上屏）；线程进程级共享，
        # clip 只挂 worker（销毁经 close()→deleteLater，无线程寿命问题）
        self._pending: dict[int, QImage] = {}
        self._wanted = -1
        self._awaiting = -1
        self._worker = _PrefetchWorker(self._frames)
        self._worker.moveToThread(_shared_prefetch_thread())
        self._worker.loaded.connect(self._on_loaded,
                                    Qt.ConnectionType.QueuedConnection)

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
        """从第 0 帧起播（链式模型：播完发 finished，循环由上层重启）。

        首帧异步交付（~2.5ms 后到，frameChanged 通知——与 WebMClip 的
        冷路径语义一致，只是从 60-166ms 缩到 ~2.5ms）。
        """
        if not self._frames:
            self.errorOccurred.emit(f"frameseq: 空素材目录 {self._dir}")
            return False
        self._timer.stop()
        self._cur = 0
        self._awaiting = -1
        img = self._pending.pop(0, None)
        if img is not None:
            self._apply(img)
            self.frameChanged.emit(0)
        else:
            self._img = None
            self._pm = None
            self._awaiting = 0
            self._request(0)
        self._running = True
        self._timer.start(self._interval_ms())
        return True

    def stop(self) -> None:
        self._running = False
        self._timer.stop()

    def close(self) -> None:
        """停止播放并回收预取 worker（MovieLibrary.shutdown/收尾调用）。

        线程是进程级共享的（不随 clip 生灭）；worker 挂 deleteLater 由
        共享线程事件循环回收。"""
        self.stop()
        self._worker.deleteLater()

    def jumpToFrame(self, frame_index: int) -> bool:
        if not self._frames:
            return False
        frame_index = max(0, min(int(frame_index), len(self._frames) - 1))
        # 低频同步路径（拖拽/复位）：一次 ~2.5ms 同步加载换立即生效
        img = QImage(str(self._frames[frame_index]))
        if not img.isNull():
            self._cur = frame_index
            self._awaiting = -1
            self._apply(img)
        self.frameChanged.emit(frame_index)
        return True

    def set_playback_speed(self, speed: float) -> None:
        self.playback_speed = max(0.1, float(speed))
        if self._running:
            self._timer.start(self._interval_ms())

    # ---------------------------------------------------------------- 解码层兼容（本实现无 webm 解码层语义，全为良性 no-op）
    def warm_meta(self) -> None:
        return

    def warm_first_frame(self) -> None:
        if self._img is None and self._frames:
            img = QImage(str(self._frames[0]))
            if not img.isNull():
                self._apply(img)

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

    def _apply(self, img: QImage) -> None:
        self._img = img
        self._pm = QPixmap.fromImage(img)

    def _request(self, idx: int) -> None:
        """请求 worker 预取一帧（幂等去重：已到货/在途不重发）。"""
        if idx >= len(self._frames) or idx in self._pending or idx == self._wanted:
            return
        self._wanted = idx
        QMetaObject.invokeMethod(self._worker, "prefetch",
                                 Qt.ConnectionType.QueuedConnection,
                                 Q_ARG(int, idx))

    @Slot(int, QImage)
    def _on_loaded(self, idx: int, img: QImage) -> None:
        self._wanted = -1
        if img.isNull():
            return  # 坏帧：保持现状，不崩播放链
        if self._running and idx == self._awaiting:
            self._cur = idx
            self._awaiting = -1
            self._apply(img)
            self.frameChanged.emit(idx)
            self._request(idx + 1)  # 链式预取下一帧
        else:
            self._pending[idx] = img

    def _advance(self) -> None:
        nxt = self._cur + 1
        if nxt >= len(self._frames):
            # 链式一次性播放到尾：停表并发 finished（上层据此接下一个动画）
            self._running = False
            self._timer.stop()
            self.finished.emit()
            return
        img = self._pending.pop(nxt, None)
        if img is not None:
            self._cur = nxt
            self._apply(img)
            self.frameChanged.emit(nxt)
            self._request(nxt + 1)
        else:
            # 未到货：等待不跳帧（同 WebMClip 空转语义），并向 worker 催取
            self._awaiting = nxt
            self._request(nxt)
