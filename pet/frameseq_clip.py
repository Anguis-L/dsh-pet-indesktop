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

预取看门狗（"画面经常卡住不动"的用户实测根因）：worker 所在共享线程失能
时，``_request`` 的 queued 调用永远不被处理，``_advance`` 会停在 ``_awaiting``
上无限等待。``_advance`` 每 tick 复查等待时长：超 ``PREFETCH_STALL_MS``
重发请求 + WARNING（含目录名/帧号/wanted/pending 大小），连续
``PREFETCH_STALL_LIMIT`` 次仍无帧则同步加载兜底（播放链不断）；线程已死
则重建共享线程并换挂新 worker。到货即清零，恢复后静默。
"""
from __future__ import annotations

import atexit
import json
import logging
import time
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

logger = logging.getLogger(__name__)

DEFAULT_FPS = 24.0

#: 预取看门狗阈值（ms）：播放位置停在 ``_awaiting`` 上超过它即判定预取失能
#: （worker/共享线程事件循环停摆），重发请求并记 WARNING。
PREFETCH_STALL_MS = 500.0
#: 连续超时次数达到它 → 降级同步加载兜底（保证播放链不断，不跳帧不冻结）。
PREFETCH_STALL_LIMIT = 3


def _now() -> float:
    """看门狗时钟（模块属性可替换：测试注入假钟，不 sleep 赌时序）。"""
    return time.monotonic()


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
        # 预取看门狗状态：awaiting 起点（monotonic；None = 不在等）+ 连续超时次数
        self._awaiting_since: float | None = None
        self._stall_count = 0
        # 已退役 worker（共享线程死亡后换新 worker，旧对象的线程亲和性留在死
        # 线程上——不能 deleteLater（没人处理），只能保留引用不跨线程销毁）
        self._retired_workers: list[_PrefetchWorker] = []
        self._worker = _PrefetchWorker(self._frames)
        self._prefetch_thread = _shared_prefetch_thread()
        self._worker.moveToThread(self._prefetch_thread)
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
        self._clear_awaiting()
        img = self._pending.pop(0, None)
        if img is not None:
            self._apply(img)
            self.frameChanged.emit(0)
        else:
            self._img = None
            self._pm = None
            self._set_awaiting(0)
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
            self._clear_awaiting()
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

    @property
    def decode_throttle_divisor(self) -> int:
        """与 WebMClip 同形的只读属性——window/decode_fanout 按属性读
        （普通方法会在该路径 TypeError，4.4b 评审实测 484~546 次/45s）。"""
        return 1

    @property
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
            self._clear_awaiting()  # 到货 = 恢复：看门狗状态清零，此后不再告警
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
            self._set_awaiting(nxt)
            self._request(nxt)
            self._check_prefetch_watchdog()

    # ---------------------------------------------------------------- 预取看门狗
    def _set_awaiting(self, idx: int) -> None:
        """登记"播放位置在等 idx"；帧号变化 = 新一轮等待（超时计数清零）。

        同一帧的重复调用不改起点——否则每次 ``_advance`` 都会把计时推后，
        看门狗永远不会到点（挂死检测失效）。
        """
        if self._awaiting != idx or self._awaiting_since is None:
            self._stall_count = 0
            self._awaiting_since = _now()
        self._awaiting = idx

    def _clear_awaiting(self) -> None:
        """到货/跳帧收口：清等待态与超时计数（恢复后保持静默）。"""
        self._awaiting = -1
        self._awaiting_since = None
        self._stall_count = 0

    def _check_prefetch_watchdog(self) -> None:
        """预取看门狗：awaiting 挂死超时 → 重发请求；连续超时 → 同步加载兜底。

        worker/共享线程失能（退出收口后的孤儿 worker、线程事件循环停摆）时，
        ``_request`` 的 queued 调用永远不被处理，``_advance`` 就一直等不到帧——
        用户可见表现即"画面卡住不动"。这里按墙钟兜底：到点重发 + WARNING（含
        clip 目录名/帧号/wanted/pending 大小），连续 ``PREFETCH_STALL_LIMIT``
        次仍无帧就同步读一帧顶上，保证播放链不断。
        """
        since = self._awaiting_since
        if since is None or self._awaiting < 0:
            return
        now = _now()
        waited_ms = (now - since) * 1000.0
        if waited_ms < PREFETCH_STALL_MS:
            return
        # 到点即重置计时：下一次判定在又一个阈值之后（不刷屏），超时计数累加
        self._awaiting_since = now
        self._stall_count += 1
        idx = self._awaiting
        thread_alive = self._prefetch_thread_alive()
        if self._stall_count < PREFETCH_STALL_LIMIT:
            logger.warning(
                "frameseq 预取超时 %.0fms：clip=%s frame=%d wanted=%d pending=%d "
                "thread_alive=%s（重发预取请求）",
                waited_ms, self._dir.name, idx, self._wanted, len(self._pending),
                thread_alive)
            if not thread_alive:
                self._revive_prefetch_worker()
            self._wanted = -1  # 清在途标记，让 _request 的重发不被去重拦下
            self._request(idx)
            return
        logger.warning(
            "frameseq 预取连续 %d 次超时（本次等待 %.0fms）：clip=%s frame=%d "
            "wanted=%d pending=%d thread_alive=%s，降级同步加载",
            self._stall_count, waited_ms, self._dir.name, idx, self._wanted,
            len(self._pending), thread_alive)
        self._stall_count = 0
        img = QImage(str(self._frames[idx]))
        if img.isNull():
            return  # 坏帧：保持现状，下个 tick 重新走看门狗
        if not self._running:
            return
        self._cur = idx
        self._clear_awaiting()
        self._apply(img)
        self.frameChanged.emit(idx)
        self._request(idx + 1)  # 同步兜底后仍续链式预取（worker 恢复即接回异步）

    def _prefetch_thread_alive(self) -> bool:
        """共享预取线程复查：线程对象失效/已停 = 死（异常一律按死处理）。

        ``_prefetch_thread`` 是本 clip 对共享线程的强引用：``_shared_thread``
        重建时旧线程若被 GC，``worker.thread()`` 会变悬垂指针，故不直接回查。
        """
        thread = self._prefetch_thread
        if thread is None:
            return False
        try:
            return bool(thread.isRunning())
        except RuntimeError:
            return False

    def _revive_prefetch_worker(self) -> None:
        """共享线程已死 → 重建线程并换新 worker（``_shared_prefetch_thread`` 有懒建语义）。

        ``moveToThread`` 只能由对象所属线程调用（GUI 线程调会被 Qt 拒绝），而旧
        worker 的亲和性绑在已停线程上，因此这里换一个新 worker；旧 worker 只留
        引用不删——跨线程销毁才是真风险（见 ``_retired_workers`` 注释）。
        """
        old = self._worker
        try:
            old.loaded.disconnect(self._on_loaded)
        except (RuntimeError, TypeError):
            pass
        self._retired_workers.append(old)
        if len(self._retired_workers) > 4:
            # 只保留最近几次：复活是异常路径，正常生命周期内一次都不会走到
            del self._retired_workers[0]
        self._worker = _PrefetchWorker(self._frames)
        self._prefetch_thread = _shared_prefetch_thread()
        self._worker.moveToThread(self._prefetch_thread)
        self._worker.loaded.connect(self._on_loaded,
                                    Qt.ConnectionType.QueuedConnection)
        logger.warning("frameseq 共享预取线程失能：已重建并换挂新 worker（clip=%s）",
                       self._dir.name)
