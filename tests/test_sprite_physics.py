# -*- coding: utf-8 -*-
"""Phase 1b 抛掷/拖拽物理 offscreen 单测（QT_QPA_PLATFORM=offscreen 可跑）。

覆盖 ThrowPhysicsController（重力下坠/边界反弹衰减/静止回 normal/子步
不穿透地板/只处理 thrown）与 PetSprite 拖拽轨迹协议（高速松手进
thrown/低速或停顿松手回 normal/初速方向/软上限/轨迹修剪）。

纪律（AGENTS.md 时序测试）：drag 轨迹时间戳用注入的假钟（sprite._clock）
确定性构造，控制器 tick 直接同步调用，不启动真实 QTimer、不固定 sleep。
素材用纯 QImage 假 clip（同 tests/test_overlay_window.py 的造假方式）。
"""
from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from pet import physics as physics_mod
from pet.pet_sprite import (
    INTERACTION_DRAG,
    INTERACTION_NORMAL,
    INTERACTION_THROWN,
    PetSprite,
)
from pet.sprite_physics import ThrowPhysicsController

app = QApplication.instance() or QApplication([])

# scale=0.5 时 sprite 外接矩形 320x180；bounds (0,0,800,600) 下
# 左上角可活动范围 x∈[0,480]、y∈[0,420]（地板 floor_y=420）
BOUNDS = QRectF(0, 0, 800, 600)
FLOOR_Y = 600 - 180
RIGHT_X = 800 - 320


class FakeClip(QObject):
    """接口对齐 WebMClip 的假 clip：frameChanged 信号 + 固定 QImage 帧。"""

    frameChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self.frame = 0
        self.image = QImage(640, 360, QImage.Format.Format_ARGB32)
        self.image.fill(Qt.GlobalColor.transparent)
        self.started = False
        self.playback_speed = 1.0
        self.speed_calls: list[float] = []

    def currentFrameNumber(self):
        return self.frame

    def currentImage(self):
        return self.image

    def frameCount(self):
        return 1

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.started = False

    def set_playback_speed(self, speed):
        self.playback_speed = float(speed)
        self.speed_calls.append(float(speed))


class FakeLibrary:
    def __init__(self, clip):
        self._clip = clip
        self.no_mirror: set[str] = set()

    def movie(self, name):
        return self._clip


class FakeClock:
    """单调假钟：advance(dt) 推进，调用返回当前值。"""

    def __init__(self, t: float = 1000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_sprite(pos=(100.0, 100.0), *, clock: FakeClock | None = None) -> PetSprite:
    sprite = PetSprite(FakeLibrary(FakeClip()), pos=QPointF(*pos), scale=0.5)
    sprite.bind_clip("fake")
    sprite.advance(0.016)  # 吞掉 bind 后的首帧脏标记
    if clock is not None:
        sprite._clock = clock
    return sprite


def _drag_steps(sprite: PetSprite, clock: FakeClock, start: tuple[float, float],
                steps: list[tuple[float, float, float]]) -> QPointF:
    """press 后按 (dx, dy, dt) 序列拖动，返回松手位置（不 release）。"""
    sprite.on_press(QPointF(*start))
    x, y = start
    for dx, dy, dt in steps:
        clock.advance(dt)
        x, y = x + dx, y + dy
        sprite.on_move(QPointF(x, y))
    return QPointF(x, y)


# ---------------------------------------------------------------- 飞行段（controller）
def test_thrown_falls_under_gravity():
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((100.0, 50.0))
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(0, 0))

    for _ in range(10):
        controller.tick([sprite], 0.016)

    assert sprite.interaction_state == INTERACTION_THROWN  # 半空中不回 normal
    assert sprite.pos.y() > 50.0                           # 下坠
    assert sprite.velocity.y() > 0.0                       # 重力累积出向下速度


def test_ground_bounce_reverses_and_damps():
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((100.0, FLOOR_Y - 5.0))
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(0, 2000))                  # 高速砸向地板

    controller.tick([sprite], 0.016)

    assert sprite.pos.y() <= FLOOR_Y                       # 不穿透地板
    assert sprite.velocity.y() < 0.0                       # 反弹向上
    assert abs(sprite.velocity.y()) < 2000.0               # 恢复系数衰减


def test_wall_bounce_reverses_horizontal():
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((RIGHT_X - 10.0, 100.0))
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(5000, -800))               # 斜向撞右墙

    controller.tick([sprite], 0.016)

    assert sprite.pos.x() <= RIGHT_X
    assert sprite.velocity.x() < 0.0                       # 水平反弹
    assert sprite.interaction_state == INTERACTION_THROWN  # 仍有速度，不收尾


def test_rest_on_ground_returns_to_normal():
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((100.0, FLOOR_Y))
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(5, 0))                     # 贴地低速滑动

    controller.tick([sprite], 0.016)

    assert sprite.interaction_state == INTERACTION_NORMAL  # 交还行为控制器
    assert sprite.velocity == QPointF(0, 0)                # 停住
    assert sprite.pos.y() == FLOOR_Y                       # 停在地板上


def test_substep_integration_does_not_tunnel_floor():
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((100.0, FLOOR_Y - 50.0))
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(0, 8000))                  # 50ms 单步会飞出 400px

    controller.tick([sprite], 0.05)                        # 大 dt：靠 ≤8ms 子步切分

    # 单步积分的末尾位置会被钳在地板上；子步积分在 tick 中途反弹、剩余
    # 时间按反弹后速度继续上升——pos 严格高于地板即证明子步语义生效
    assert 0.0 <= sprite.pos.y() < FLOOR_Y
    assert sprite.velocity.y() < 0.0
    assert sprite.interaction_state == INTERACTION_THROWN


def test_controller_ignores_non_thrown_sprites():
    controller = ThrowPhysicsController(BOUNDS)
    normal = _make_sprite((50.0, 50.0))
    normal.set_velocity(QPointF(300, 0))                   # normal：advance 管
    dragged = _make_sprite((300.0, 50.0))
    dragged.on_press(QPointF(310.0, 60.0))                 # drag：光标管

    controller.tick([normal, dragged], 0.016)

    assert normal.pos == QPointF(50.0, 50.0)               # 控制器不碰
    assert normal.velocity == QPointF(300, 0)
    assert dragged.pos == QPointF(300.0, 50.0)


def test_advance_skips_integration_when_thrown():
    sprite = _make_sprite((100.0, 100.0))
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(300, 0))

    sprite.advance(0.016)                                  # thrown：控制器积分
    assert sprite.pos == QPointF(100.0, 100.0)

    sprite.interaction_state = INTERACTION_NORMAL
    sprite.advance(0.016)                                  # normal：advance 积分
    assert sprite.pos == QPointF(104.8, 100.0)


# ---------------------------------------------------------------- F4：飞行期动画匹配
def test_flight_anim_speed_follows_velocity_and_landing_resets():
    """F4：飞行每 tick 按速度设 clip 速率；落地复位回用户速率。

    不复位会让 duration()（除以 playback_speed，webm_clip.py:1387-1390）
    在下一次 _plan_move 里按加速后的时长量化位移。
    """
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((100.0, 100.0))
    clip = sprite._clip
    sprite.playback_speed = 1.0
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(1400, 0))                  # 高速飞行

    controller.tick([sprite], 0.016)

    assert sprite.interaction_state == INTERACTION_THROWN
    assert clip.playback_speed > 1.0                       # 频闪修法：加速播放
    assert sprite.velocity.x() > 0.0                       # 位置积分未被影响

    sprite.set_pos(QPointF(100.0, FLOOR_Y))                # 贴地低速 → 落地
    sprite.set_velocity(QPointF(5, 0))
    controller.tick([sprite], 0.016)

    assert sprite.interaction_state == INTERACTION_NORMAL
    assert clip.playback_speed == 1.0                      # 速率复位


def test_flight_anim_speed_stacks_on_user_rate():
    """倍率叠加在用户播放速率之上（window.py:4328-4340），不是硬写成 1.75×。"""
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((100.0, 100.0))
    clip = sprite._clip
    sprite.playback_speed = 1.5
    sprite.interaction_state = INTERACTION_THROWN
    sprite.set_velocity(QPointF(700, 0))

    controller.tick([sprite], 0.016)

    speed = math.hypot(sprite.velocity.x(), sprite.velocity.y())
    assert clip.playback_speed > 1.5
    assert clip.playback_speed == pytest.approx(
        1.5 * physics_mod.flight_anim_speed(speed))


def test_flight_anim_speed_untouched_for_non_thrown():
    controller = ThrowPhysicsController(BOUNDS)
    sprite = _make_sprite((100.0, 100.0))
    clip = sprite._clip
    sprite.set_velocity(QPointF(900, 0))                   # normal：advance 管

    controller.tick([sprite], 0.016)

    assert clip.playback_speed == 1.0                      # 控制器不碰非 thrown


# ---------------------------------------------------------------- 松手抛掷判定（sprite 侧）
def test_high_speed_release_enters_thrown():
    clock = FakeClock()
    sprite = _make_sprite((100.0, 100.0), clock=clock)
    # 400px / 0.1s ≈ 4000px/s，远超 DEAD_ZONE_SPEED
    release = _drag_steps(sprite, clock, (110.0, 110.0),
                          [(40.0, 0.0, 0.01)] * 10)
    sprite.on_release(release)

    assert not sprite.dragging
    assert sprite.interaction_state == INTERACTION_THROWN
    assert sprite.velocity.x() > physics_mod.DEAD_ZONE_SPEED
    assert sprite.velocity.y() == 0.0                      # 纯水平甩动
    # 松手后停在最后一次拖动位置（grab 偏移 (10,10)）
    assert sprite.pos == release - QPointF(10.0, 10.0)


def test_slow_release_returns_normal():
    clock = FakeClock()
    sprite = _make_sprite((100.0, 100.0), clock=clock)
    sprite.set_velocity(QPointF(120, 0))                   # 拖拽前在游荡
    # 6px / 0.03s ≈ 200px/s，低于死区 = 原地放下
    release = _drag_steps(sprite, clock, (110.0, 110.0),
                          [(2.0, 0.0, 0.01)] * 3)
    sprite.on_release(release)

    assert sprite.interaction_state == INTERACTION_NORMAL
    assert sprite.velocity == QPointF(0, 0)                # 停住，不恢复拖拽前速度
    assert sprite.pos == release - QPointF(10.0, 10.0)


def test_stale_release_returns_normal():
    clock = FakeClock()
    sprite = _make_sprite((100.0, 100.0), clock=clock)
    release = _drag_steps(sprite, clock, (110.0, 110.0),
                          [(40.0, 0.0, 0.01)] * 10)        # 快甩
    clock.advance(0.5)                                     # 但松手前停顿 > STALE
    sprite.on_release(release)

    assert sprite.interaction_state == INTERACTION_NORMAL  # 停顿 = 静止放下
    assert sprite.velocity == QPointF(0, 0)


def test_release_velocity_direction_matches_drag():
    clock = FakeClock()
    sprite = _make_sprite((300.0, 300.0), clock=clock)
    # 向右上甩：dx:dy = 3:-2
    release = _drag_steps(sprite, clock, (310.0, 310.0),
                          [(30.0, -20.0, 0.01)] * 8)
    sprite.on_release(release)

    assert sprite.interaction_state == INTERACTION_THROWN
    vx, vy = sprite.velocity.x(), sprite.velocity.y()
    assert vx > 0.0 and vy < 0.0                           # 方向：右上
    assert math.isclose(vx / -vy, 1.5, rel_tol=0.2)        # 位移方向比例保持


def test_release_speed_soft_clamped_below_cap():
    clock = FakeClock()
    sprite = _make_sprite((100.0, 100.0), clock=clock)
    # 3000px / 0.05s ≈ 60000px/s：十倍于 cap 的暴力甩
    release = _drag_steps(sprite, clock, (110.0, 110.0),
                          [(300.0, 0.0, 0.005)] * 10)
    sprite.on_release(release)

    speed = math.hypot(sprite.velocity.x(), sprite.velocity.y())
    assert sprite.interaction_state == INTERACTION_THROWN
    assert physics_mod.DEAD_ZONE_SPEED < speed < sprite.throw_speed_cap


def test_drag_trail_sampling_and_pruning():
    clock = FakeClock()
    sprite = _make_sprite((100.0, 100.0), clock=clock)
    sprite.on_press(QPointF(110.0, 110.0))
    assert len(sprite.drag_trail) == 1                     # press 即首个样本

    for i in range(5):
        clock.advance(0.01)
        sprite.on_move(QPointF(120.0 + i * 10, 110.0))
    trail = sprite.drag_trail
    assert len(trail) == 6
    assert all(len(s) == 3 for s in trail)                 # (ts, x, y) 三元组
    assert trail[-1] == (clock.t, 160.0, 110.0)

    clock.advance(0.3)                                     # 远超 TRAIL_KEEP_SEC
    sprite.on_move(QPointF(170.0, 110.0))
    trail = sprite.drag_trail
    assert trail == [(clock.t, 170.0, 110.0)]              # 旧样本全部修剪

    clock.advance(0.01)
    sprite.on_release(QPointF(170.0, 110.0))
    assert sprite.drag_trail == []                         # 松手后清空


def test_press_sets_drag_state_and_stops_velocity():
    sprite = _make_sprite((100.0, 100.0))
    sprite.set_velocity(QPointF(200, 50))

    sprite.on_press(QPointF(110.0, 120.0))

    assert sprite.dragging
    assert sprite.interaction_state == INTERACTION_DRAG
    assert sprite.velocity == QPointF(0, 0)


# ---------------------------------------------------------------- V-2：body_box 口径
def test_sprite_bounds_inset_by_body_box(monkeypatch):
    """V-2：抛掷反弹边界按身体框内缩（旧口径按整 canvas，反弹发生在透明边上）。

    body_box=(100,60,400,330) × scale 0.5 → 局部身体框 QRect(50,30,150,135)；
    bounds (0,0,800,600) → 左上角活动范围 (-50,-30,600,435)。
    """
    from pet import catalog

    monkeypatch.setattr(catalog, "character_body_box", lambda _cid: (100, 60, 400, 330))
    sprite = _make_sprite()
    c = ThrowPhysicsController(BOUNDS)
    assert c._sprite_bounds(sprite) == (-50.0, -30.0, 600.0, 435.0)
