# -*- coding: utf-8 -*-
"""M-2 统一 tick 驱动器回归（QT_QPA_PLATFORM=offscreen 可跑）。

覆盖（REVIEW_VERDICT.md M-2 / 设计稿 PHASE4_DESIGN.md T2）：
- tick_sim 顺序协议（行为→碰撞→物理）与**成员快照完整性**：仿真成员聚合自
  全部已挂载 overlay（多 overlay 预留；单 overlay 时等价于该 overlay.sprites）；
- advance+paint 段：驱动器逐个 overlay 调 ``tick_advance``，advance/脏矩形/
  位置监听 fanout/update 仍在 overlay 侧（V-3/V-4 语义不变）；
- 驱动器独立持有控制器：已挂控制器时**不再**调 ``overlay.before_sprites_advance``
  （无双份仿真）；未挂控制器时走兼容分支（demo/裸 overlay 旧装配）；
- 生命周期：attach 幂等；detach 最后一个成员停表（V-12），多成员时其余成员
  继续被驱动；overlay.start() 在 stop() 后能重新挂回；
- 时间基：首 tick 取档位间隔、显式 dt 直通、T3 心跳不跑仿真；
- M-1：升档同步立即（note_kinetic）、note_frame 喂"动画在播"、
  刷新率取多成员最高值、refresh 不覆盖非 T0 档位间隔；
- V-7：tick 路径逐 overlay 兜底卡死拖拽。

纪律（AGENTS.md 时序测试）：同步直调 ``on_tick(dt=...)``，不启动真实 QTimer、
不 sleep 赌时序；假 overlay/假 sprite 纯鸭式，不碰素材与 ffmpeg。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QApplication

from pet.overlay_window import OverlayWindow
from pet.tick_driver import TickDriver
from pet.tick_governor import (
    TIER_ACTIVE,
    TIER_IDLE_STILL,
    TIER_INTERVAL_MS,
    TIER_OCCLUDED,
)

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 假件
class FakeScreen:
    """鸭式 QScreen：只需 refreshRate（驱动器取数口）。"""

    def __init__(self, refresh=60.0):
        self._refresh = float(refresh)

    def refreshRate(self):
        return self._refresh


class FakeSprite:
    """鸭式 sprite：只给 advance/rect，够跑推进+脏矩形段。"""

    SIZE = (40, 30)

    def __init__(self, pos=(10, 10), *, movable=False, velocity=(0.0, 0.0)):
        self.pos = QPointF(*pos)
        self.velocity = QPointF(*velocity)
        self.interaction_state = "normal"
        self.movable = movable

    def rect(self):
        return QRect(int(self.pos.x()), int(self.pos.y()), *self.SIZE)

    def advance(self, dt):
        old = self.rect()
        if self.movable:
            self.pos += self.velocity * dt
        new = self.rect()
        return (old, new) if new != old else None


class FakeOverlay:
    """鸭式 overlay：记录驱动器要求的两段调用（仿真钩子 + 推进段）。"""

    def __init__(self, sprites=None, *, screen=None, visible=True):
        self.sprites = list(sprites or [])
        self.screen = screen
        self._visible = visible
        self.hook_calls: list[float] = []
        self.advance_calls: list[float] = []
        self.stale_checks = 0

    def isVisible(self):
        return self._visible

    def before_sprites_advance(self, dt):
        self.hook_calls.append(dt)

    def tick_advance(self, dt):
        self.advance_calls.append(dt)

    def _check_stale_press(self):
        self.stale_checks += 1


class Recorder:
    """记录 tick(sprites, dt) 的控制器替身。"""

    def __init__(self, name, calls):
        self.name = name
        self._calls = calls

    def tick(self, sprites, dt):
        self._calls.append((self.name, list(sprites), dt))


# ---------------------------------------------------------------- 两段拆分与顺序协议
def test_tick_sim_order_and_members_aggregate_across_overlays():
    """T2：成员快照聚合自全部 overlay；顺序协议行为→碰撞→物理不变。"""
    driver = TickDriver()
    s1, s2, s3 = FakeSprite(), FakeSprite(), FakeSprite()
    first = FakeOverlay([s1, s2], screen=FakeScreen(60.0))
    second = FakeOverlay([s3], screen=FakeScreen(170.0))
    driver.attach(first)
    driver.attach(second)

    calls: list = []
    driver.set_controllers(Recorder("behavior", calls), Recorder("collision", calls),
                           Recorder("physics", calls))
    driver.on_tick(dt=0.02)

    assert [c[0] for c in calls] == ["behavior", "collision", "physics"]
    for _name, sprites, dt in calls:
        assert sprites == [s1, s2, s3]      # 两个 overlay 的成员都在（聚合完整）
        assert dt == 0.02
    assert driver.sprites == [s1, s2, s3]
    assert driver.overlays == [first, second]
    # 推进段逐 overlay 各一次（同一 dt）
    assert first.advance_calls == [0.02]
    assert second.advance_calls == [0.02]
    # V-7：tick 路径兜底拖拽看门狗逐 overlay 走到
    assert (first.stale_checks, second.stale_checks) == (1, 1)


def test_controllers_attached_means_no_compat_hook_call():
    """M-2：已挂控制器 = 仿真段唯一入口在驱动器，钩子不得再被调用（无双份）。"""
    driver = TickDriver()
    overlay = FakeOverlay(screen=FakeScreen(60.0))
    driver.attach(overlay)
    calls: list = []
    driver.set_controllers(Recorder("behavior", calls), Recorder("collision", calls),
                           Recorder("physics", calls))

    driver.on_tick(dt=0.016)

    assert overlay.hook_calls == []
    assert [c[0] for c in calls] == ["behavior", "collision", "physics"]
    assert overlay.advance_calls == [0.016]


def test_partial_controllers_also_skip_compat_hook():
    """只挂一个控制器（测试/子集装配）也不算兼容分支：不回调钩子。"""
    driver = TickDriver()
    overlay = FakeOverlay(screen=FakeScreen(60.0))
    driver.attach(overlay)
    calls: list = []
    driver.set_controllers(behavior=Recorder("behavior", calls))

    driver.on_tick(dt=0.016)

    assert overlay.hook_calls == []
    assert [c[0] for c in calls] == ["behavior"]


def test_no_controllers_uses_deprecated_hook_compat_path():
    """兼容期（deprecation）：裸 overlay / demo 旧装配仍走钩子作仿真段。"""
    driver = TickDriver()
    overlay = FakeOverlay(screen=FakeScreen(60.0))
    driver.attach(overlay)

    driver.on_tick(dt=0.016)

    assert overlay.hook_calls == [0.016]
    assert overlay.advance_calls == [0.016]


# ---------------------------------------------------------------- 生命周期与成员语义
def test_attach_is_idempotent_and_detach_order_independent():
    driver = TickDriver()
    first = FakeOverlay(screen=FakeScreen(60.0))
    second = FakeOverlay(screen=FakeScreen(60.0))
    driver.attach(first)
    driver.attach(first)                     # 重复挂载是 no-op
    driver.attach(second)
    assert driver.overlays == [first, second]

    driver.detach(second)
    assert driver.overlays == [first]
    driver.detach(second)                    # 重复摘除是 no-op
    assert driver.overlays == [first]


def test_last_detach_stops_timer_others_keep_ticking():
    """V-12 + 多成员：摘最后一个才停表，其余成员继续被驱动。"""
    driver = TickDriver()
    first = FakeOverlay(screen=FakeScreen(60.0))
    second = FakeOverlay(screen=FakeScreen(60.0))
    driver.attach(first)
    driver.attach(second)
    driver.start()
    assert driver.timer.isActive()

    driver.detach(first)
    assert driver.timer.isActive()           # 还有成员：不停表
    driver.on_tick(dt=0.016)
    assert second.advance_calls == [0.016]
    assert first.advance_calls == []         # 摘除的成员不再被推进

    driver.detach(second)
    assert not driver.timer.isActive()       # 最后一个摘除 → 停表


def test_overlay_start_reattaches_after_stop():
    """closeEvent（detach 语义）+ stop() 后，start() 必须重新挂回驱动器。"""
    from PySide6.QtGui import QCloseEvent

    driver = TickDriver()
    overlay = OverlayWindow(driver=driver)
    overlay.start()
    assert driver.timer.isActive() and driver.overlays == [overlay]

    overlay.closeEvent(QCloseEvent())         # 关窗 → 从驱动器摘除
    assert driver.overlays == []
    assert not driver.timer.isActive()        # 无成员 → 停表（V-12）

    overlay.start()                           # 重启：幂等挂回并复走时钟
    assert driver.overlays == [overlay]
    assert driver.timer.isActive()
    driver.stop()


# ---------------------------------------------------------------- 时间基
def test_first_tick_uses_tier_interval_and_explicit_dt_passes_through():
    driver = TickDriver()
    overlay = FakeOverlay(screen=FakeScreen(60.0))
    driver.attach(overlay)

    driver.on_tick()                          # 首 tick：dt = 档位间隔（16ms）
    assert overlay.hook_calls == [pytest.approx(driver.timer.interval() / 1000.0)]

    driver.on_tick(dt=0.5)                    # 显式 dt 直通（测试同步驱动口）
    assert overlay.hook_calls[-1] == 0.5


def test_occluded_heartbeat_runs_no_simulation():
    driver = TickDriver()
    overlay = FakeOverlay(screen=FakeScreen(60.0), visible=False)
    driver.attach(overlay)
    driver._apply_tier(TIER_OCCLUDED)         # T3 心跳档

    driver.on_tick(dt=0.016)

    assert overlay.hook_calls == []
    assert overlay.advance_calls == []


# ---------------------------------------------------------------- M-1 挂钩（驱动器侧）
def test_note_kinetic_upgrades_synchronously():
    driver = TickDriver()
    overlay = FakeOverlay(screen=FakeScreen(60.0))
    driver.attach(overlay)
    driver._apply_tier(TIER_IDLE_STILL)

    driver.note_kinetic()

    assert driver.applied_tier == TIER_ACTIVE
    assert driver.timer.timerType() == Qt.TimerType.PreciseTimer


def test_note_frame_feeds_animating_input_and_any_motion_covers_all_overlays(monkeypatch):
    driver = TickDriver()
    still = FakeOverlay([FakeSprite()], screen=FakeScreen(60.0))
    moving_sprite = FakeSprite(velocity=(120.0, 0.0))
    moving = FakeOverlay([moving_sprite], screen=FakeScreen(60.0))
    driver.attach(still)
    seen: dict = {}

    def fake_evaluate(*, any_motion, animating, visible):
        seen.update(any_motion=any_motion, animating=animating, visible=visible)
        return TIER_ACTIVE

    monkeypatch.setattr(driver._governor, "evaluate", fake_evaluate)
    driver.note_frame()
    driver._sync_tier()
    assert seen["animating"] is True          # 帧活性喂进"动画在播"
    assert seen["visible"] is True

    seen.clear()
    driver.attach(moving)                     # 第二个 overlay 的成员也要计入
    driver._sync_tier()
    assert seen["any_motion"] is True         # M-1 不变量：任一成员在动 ⇒ 必 T0


def test_refresh_uses_fastest_attached_screen_and_survives_detach():
    driver = TickDriver()
    slow = FakeOverlay(screen=FakeScreen(60.0))
    fast = FakeOverlay(screen=FakeScreen(170.0))
    driver.attach(slow)
    assert driver.timer.interval() == TickDriver.tick_interval_ms(60.0)
    driver.attach(fast)
    assert driver.timer.interval() == TickDriver.tick_interval_ms(170.0)  # 6ms
    driver.detach(fast)
    assert driver.timer.interval() == TickDriver.tick_interval_ms(60.0)


def test_refresh_does_not_clobber_downgraded_interval():
    """M-1：屏事件重读刷新率不得把降档间隔（250ms）打回全速。"""
    driver = TickDriver()
    driver.attach(FakeOverlay(screen=FakeScreen(170.0)))
    driver._apply_tier(TIER_IDLE_STILL)

    driver.refresh_tick_interval()

    assert driver.timer.interval() == TIER_INTERVAL_MS[TIER_IDLE_STILL]


def test_tick_interval_boundaries_shared_with_overlay():
    assert TickDriver.tick_interval_ms(170.0) == 6
    assert TickDriver.tick_interval_ms(90.0) == 11
    assert TickDriver.tick_interval_ms(60.0) == 16
    assert TickDriver.tick_interval_ms(None) == 16
    assert OverlayWindow._tick_interval_ms(144.0) == TickDriver.tick_interval_ms(144.0)


# ---------------------------------------------------------------- 真实 overlay 端到端
class RecordingOverlay(OverlayWindow):
    """把 update() 的累加区域记下来，不做真实绘制调度。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.updated = QRegion()

    def update(self, *args):  # noqa: D102 - 测试替身
        for arg in args:
            if isinstance(arg, QRegion):
                self.updated |= arg
            elif isinstance(arg, QRect):
                self.updated |= QRegion(arg)


def test_driver_tick_advances_real_overlay_with_dirty_rect_and_fanout():
    """端到端：驱动器 tick → overlay.tick_advance → 脏矩形盖旧|新 rect + 位置 fanout。"""
    driver = TickDriver()
    overlay = RecordingOverlay(driver=driver)
    sprite = FakeSprite(pos=(10, 10), movable=True, velocity=(100.0, 0.0))
    overlay.add_sprite(sprite)
    seen: list = []
    overlay.add_position_listener(sprite, seen.append)

    old = sprite.rect()
    driver.on_tick(dt=0.1)

    new = sprite.rect()
    assert new != old
    assert QRegion(old).subtracted(overlay.updated).isEmpty()
    assert QRegion(new).subtracted(overlay.updated).isEmpty()
    assert seen == [sprite]                   # 位置 fanout 在推进段之后
    # 未挂控制器的兼容分支：推进段照跑（无仿真也推进画面）
    assert overlay._timer is driver.timer
