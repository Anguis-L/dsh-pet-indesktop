# -*- coding: utf-8 -*-
"""PetSprite clip 生命周期单测（QT_QPA_PLATFORM=offscreen 可跑）。

覆盖「移动与动画匹配」层落在 sprite 侧的接缝：
- F2 圈末 re-arm：restart_clip 只调 start()（WebMClip start 自带软停续圈 /
  fresh start 复位 _ended_fired，帧序列 clip 的 start 自带回首帧）、
  clip.finished 转发给挂接的控制器的回调；
- F4 飞行期动画速率：set_flight_anim_speed 叠加、reset_playback_speed 复位
  回用户速率（_clip_duration 会除以 playback_speed，不复位会让下一次
  _plan_move 按加速后的时长建计划）；
- F6 bind_clip 返回 bool：start() 明确拒绝（False）才算失败，返 None 的
  播放器（GifClip.start）按接受处理。

纪律（AGENTS.md 时序测试）：全部同步直调，不起真实 QTimer、不固定 sleep；
假 clip 用纯 QImage + 显式信号，不依赖 webm 素材与 ffmpeg。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPointF, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from pet.pet_sprite import PetSprite

app = QApplication.instance() or QApplication([])


class FakeClip(QObject):
    """接口对齐 WebMClip 的假 clip：记录 start/stop 次数与播放速率。"""

    frameChanged = Signal(int)
    finished = Signal()

    def __init__(self, *, accept_start=True, start_returns_none=False):
        super().__init__()
        self.frame = 0
        self.image = QImage(64, 36, QImage.Format.Format_ARGB32)
        self.image.fill(0xFF336699)
        self.start_count = 0
        self.stop_count = 0
        self.jump_count = 0
        self.events: list[str] = []
        self.accept_start = accept_start
        self.start_returns_none = start_returns_none
        self.playback_speed = 1.0
        self.speed_calls: list[float] = []

    def currentFrameNumber(self):
        return self.frame

    def currentImage(self):
        return self.image

    def frameCount(self):
        return 1

    def duration(self):
        return 1.0

    def start(self):
        self.start_count += 1
        self.events.append("start")
        if self.start_returns_none:
            return None
        return bool(self.accept_start)

    def stop(self):
        self.stop_count += 1

    def jumpToFrame(self, _index):
        self.jump_count += 1
        self.events.append("jump")
        return True

    def set_playback_speed(self, speed):
        self.playback_speed = float(speed)
        self.speed_calls.append(float(speed))


class NoFinishedClip(QObject):
    """缺 finished 信号的播放器（旧测试替身）：bind 不得因此报错。"""

    frameChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self.image = QImage(64, 36, QImage.Format.Format_ARGB32)
        self.image.fill(0xFF336699)
        self.starts = 0

    def currentFrameNumber(self):
        return 0

    def currentImage(self):
        return self.image

    def start(self):
        self.starts += 1
        return True

    def stop(self):
        pass


class FakeLibrary:
    def __init__(self, clip):
        self._clip = clip
        self.no_mirror: set[str] = set()

    def movie(self, _name):
        return self._clip


def _make_sprite(clip) -> PetSprite:
    return PetSprite(FakeLibrary(clip), pos=QPointF(0, 0), scale=0.5)


# ---------------------------------------------------------------- F2：圈末 re-arm
def test_restart_clip_starts_current_clip_again():
    clip = FakeClip()
    sprite = _make_sprite(clip)
    assert sprite.bind_clip("walk") is True
    assert clip.start_count == 1

    assert sprite.restart_clip() is True

    assert clip.start_count == 2            # 原地续播
    # 必须先 jumpToFrame(0) 再 start（旧机 _restart_current_clip 序列）：
    # WebM 软停续圈的前提 _soft_parked 只在 stop() 里置位，jumpToFrame 内部
    # 走 stop()；只 start 会落 fresh start——换代、退役 reader、每圈新起
    # ffmpeg（实跑实证 gen 1->2 retired=1，旧机序列 1->1 retired=0）。
    assert clip.events[-2:] == ["jump", "start"]


def test_restart_clip_invalidates_frame_signature():
    """M2：restart 作废帧签名——否则 start 归帧号 0 后第一次重建会用
    「帧号 0 + 旧末帧图」记签名，真帧 0 到货被快路径吞掉（每圈吞一帧）。"""
    clip = FakeClip()
    sprite = _make_sprite(clip)
    sprite.bind_clip("walk")
    sprite._rebuild_pixmap()  # 让签名挂上当前帧
    assert sprite._frame_sig is not None

    assert sprite.restart_clip() is True

    assert sprite._frame_sig is None
    assert sprite._rebuild_pixmap() is True  # 签名作废后必重建（不再吞帧）


def test_restart_clip_without_clip_returns_false():
    sprite = _make_sprite(FakeClip())
    assert sprite.restart_clip() is False


def test_restart_clip_reports_rejected_start():
    clip = FakeClip(accept_start=False)
    sprite = _make_sprite(clip)
    sprite.bind_clip("walk")
    assert sprite.restart_clip() is False


def test_finished_signal_forwarded_to_attached_controller():
    clip = FakeClip()
    sprite = _make_sprite(clip)
    seen = []
    sprite._clip_finished_cb = seen.append      # 控制器挂接点（_ensure_hooks）
    sprite.bind_clip("walk")

    clip.finished.emit()

    assert seen == [sprite]


def test_rebind_disconnects_finished_from_old_clip():
    old = FakeClip()
    new = FakeClip()
    library = FakeLibrary(old)
    sprite = PetSprite(library, scale=0.5)
    seen = []
    sprite._clip_finished_cb = seen.append
    sprite.bind_clip("walk")

    library._clip = new
    sprite.bind_clip("run")
    old.finished.emit()                          # 旧 clip 的迟到圈末

    assert seen == []                            # 不得打到已换绑的 sprite 上
    new.finished.emit()
    assert seen == [sprite]


def test_bind_clip_tolerates_clip_without_finished_signal():
    clip = NoFinishedClip()
    sprite = _make_sprite(clip)
    assert sprite.bind_clip("walk") is True
    assert clip.starts == 1


def test_close_clears_clip_finished_hook():
    clip = FakeClip()
    sprite = _make_sprite(clip)
    seen = []
    sprite._clip_finished_cb = seen.append
    sprite.bind_clip("walk")

    sprite.close()
    clip.finished.emit()

    assert seen == []                            # 释放后不再回调（sprite 已移除）


# ---------------------------------------------------------------- F6：bind 结果
def test_bind_clip_returns_false_when_start_rejected():
    clip = FakeClip(accept_start=False)
    sprite = _make_sprite(clip)
    assert sprite.bind_clip("walk") is False


def test_bind_clip_treats_none_start_result_as_accepted():
    """GifClip.start() 无返回值：None 不得被当成起播失败（否则 GIF 角色不走路）。"""
    clip = FakeClip(start_returns_none=True)
    sprite = _make_sprite(clip)
    assert sprite.bind_clip("walk") is True


# ---------------------------------------------------------------- F4：飞行期速率
def test_flight_anim_speed_scales_user_playback_speed():
    clip = FakeClip()
    sprite = _make_sprite(clip)
    sprite.playback_speed = 1.5                  # 用户速率
    sprite.bind_clip("hang")

    sprite.set_flight_anim_speed(1.75)

    assert clip.playback_speed == 1.5 * 1.75


def test_reset_playback_speed_restores_user_rate():
    """落地必须复位：_clip_duration 除 playback_speed（webm_clip.py:1387-1390）。"""
    clip = FakeClip()
    sprite = _make_sprite(clip)
    sprite.playback_speed = 1.2
    sprite.bind_clip("hang")
    sprite.set_flight_anim_speed(1.75)
    assert clip.playback_speed != 1.2

    sprite.reset_playback_speed()

    assert clip.playback_speed == 1.2


def test_set_flight_anim_speed_skips_redundant_writes():
    """每 tick 调用：速率不变（Δ<0.05）不得重复写（WebMClip 会重设 QTimer 间隔）。"""
    clip = FakeClip()
    sprite = _make_sprite(clip)
    sprite.bind_clip("hang")
    sprite.set_flight_anim_speed(1.0)
    calls = len(clip.speed_calls)

    sprite.set_flight_anim_speed(1.0)

    assert len(clip.speed_calls) == calls


# ---------------------------------------------------------------- 慢帧归因：bind 的 jump 策略
def test_bind_skips_jump_when_display_frame_ready():
    """显示槽已有帧（预热/上一圈残留）→ 不再 jump（start 不清槽，显示连续）。"""
    clip = FakeClip()
    sprite = _make_sprite(clip)
    assert sprite.bind_clip("walk") is True
    assert clip.jump_count == 0          # FakeClip.currentImage 恒非空 → 不跳
    assert clip.start_count == 1


def test_bind_no_jump_for_non_frameseq_without_frame():
    """WebM 型 clip 无帧也不 jump——它的 jumpToFrame(0) 会 hard-stop 现有
    reader、随后 start 重新 spawn ffmpeg（GUI 线程 50ms 慢帧源）；
    无帧时靠保留旧 _pixmap 兜底 + start 异步交付（py-spy 慢帧归因）。"""
    clip = FakeClip()
    clip.image = QImage()                # 空图 = 显示槽无帧
    sprite = _make_sprite(clip)
    assert sprite.bind_clip("walk") is True
    assert clip.jump_count == 0          # 非帧序列：不跳（异步交付）
    assert clip.start_count == 1


def test_bind_jumps_for_frameseq_without_frame(tmp_path):
    """FrameSeqClip 无帧 → jumpToFrame(0)（同步磁盘读 ~2.5ms，便宜且必需：
    它的 start 是异步交付首帧）。真 FrameSeqClip 实例（isinstance 判定）。"""
    from PySide6.QtGui import QImage as _QI
    from pet.frameseq_clip import FrameSeqClip

    d = tmp_path / "clip"
    d.mkdir()
    (d / "meta.json").write_text('{"fps": 24}', encoding="utf-8")
    img = _QI(8, 8, _QI.Format.Format_ARGB32)
    img.fill(0xFF336699)
    assert img.save(str(d / "f_0001.webp"))  # 空帧目录的 start() 会拒播
    clip = FrameSeqClip(d)
    jumps: list = []
    original = clip.jumpToFrame
    clip.jumpToFrame = lambda n: (jumps.append(n), original(n))[1]
    sprite = _make_sprite(clip)
    assert sprite.bind_clip("walk") is True
    assert jumps == [0]
