# -*- coding: utf-8 -*-
"""岛速通道回归：拖拽灵动岛撞桌宠要「有冲量弹开」，不是只平推。

根因：``sprite_collision._static_member_state`` 把岛（静态成员）速度写死
``vx=vy=0``，``add_static_member`` 也不收速度；求解器只认相对速度 → vn≈0 →
冲量 j=0 → 只剩位置分离 = 平推。旧架构的岛速估计在
``island_collision._update_motion``（overlay 拓扑下该 body 被旁路，app.py:2244）。

本文件覆盖：
- 岛桥把采样出的岛速随 update_geometry → _sync → add_static_member 传进世界；
- 岛以 > ``STATIC_HIT_MIN_DV`` 的速度撞静止 sprite → 冲量 + THROWN + 事件 + bump；
- 守卫逐条（旧代码注释记载的实机教训）：几何动画期间不采样（展开动画峰值
  ~2600px/s 会把旁边的鱼凭空拍飞）、尺寸变化只重置采样点、dt<0.01s 跳过样本、
  瞬移跳变清零、仅拖拽中允许钳到上限、岛停下 rect 未变速度也归零。

纪律（AGENTS.md）：注入假钟 + 同步直调 update_geometry/tick，禁固定 sleep；
假件复用 ``tests/test_island_bridge.py`` 的零 Qt 协议替身。
"""
from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pet import collision as collision_mod
from pet.island_bridge import IslandCollisionBridge
from pet.sprite_collision import STATIC_HIT_MIN_DV, SpriteCollisionWorld
from tests.test_island_bridge import FakeSprite

ISLAND = collision_mod.ISLAND_MEMBER_ID
# 拖拽中的岛速上限（口径沿用 island_collision._MAX_ISLAND_SPEED=1500）
MAX_ISLAND_SPEED = 1500.0

# 岛几何（碰撞世界局部坐标）：宽 200、高 44 的胶囊；sprite 60×60 贴其右端，
# 第二次采样（left=340）时胶囊右端圆心 (518,122) 与 sprite 圆心 (550,122)
# 距离 32 < 22+30 → 法线恰好为 (+1, 0)，岛向右拖即朝 sprite 接近。
ISLAND_LEFT = 300.0
ISLAND_TOP = 100.0
ISLAND_W = 200.0
ISLAND_H = 44.0
SPRITE_POS = (520.0, 92.0)


class FakeClock:
    """可注入假钟（岛速采样用；不 sleep 赌时序）。"""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class IslandStub:
    """核心桥速度守卫读的岛侧状态（零 Qt 鸭子类型）。

    ``_geo_to`` 非 None = 展开/停靠/归位几何动画中；``_dragging`` = 用户拖拽中。
    """

    def __init__(self, *, dragging=True, geo_to=None):
        self._dragging = bool(dragging)
        self._geo_to = geo_to


def _bridge(*, dragging=True, geo_to=None, clock=None):
    bumps: list = []
    bridge = IslandCollisionBridge(
        island=IslandStub(dragging=dragging, geo_to=geo_to),
        clock=clock if clock is not None else FakeClock(),
        bump=lambda s, dx, dy: bumps.append((s, dx, dy)))
    return bridge, bumps


def _moving_island(bridge, clock, *, dx=40.0, dt=0.05):
    """先采样基线、再位移 dx 采样一次 → 岛速 ≈ dx/dt。"""
    bridge.update_geometry(ISLAND_LEFT, ISLAND_TOP, ISLAND_W, ISLAND_H)
    clock.advance(dt)
    bridge.update_geometry(ISLAND_LEFT + dx, ISLAND_TOP, ISLAND_W, ISLAND_H)


# ---------------------------------------------------------------- 主回归（弹开）
def test_moving_island_impulses_stationary_sprite():
    """岛以 800px/s 拖向静止 sprite → 冲量切 THROWN + CollisionEvent + bump。"""
    world = SpriteCollisionWorld()
    clock = FakeClock()
    bridge, bumps = _bridge(dragging=True, clock=clock)
    try:
        bridge.attach(world)
        sprite = FakeSprite(*SPRITE_POS)
        events: list = []
        world.add_collision_listener(events.append)

        _moving_island(bridge, clock)  # dx=40 / 0.05s = 800px/s
        assert world._static_member_velocity[ISLAND][0] == pytest.approx(800.0)

        world.tick([sprite], 1 / 60)

        assert [e for e in events if ISLAND in (e.a, e.b)], "岛的真撞击未 fire 事件"
        assert sprite.interaction_state == "thrown"
        assert math.hypot(sprite.velocity.x(), sprite.velocity.y()) >= STATIC_HIT_MIN_DV
        assert bumps, "岛 bump 反馈未触发"
    finally:
        bridge.detach()


def test_static_member_velocity_participates_in_solver():
    """世界层直喂：静态成员速度产生相对接近速度 → 冲量（非仅位置分离）。"""
    world = SpriteCollisionWorld()
    try:
        world.add_static_member(ISLAND, ISLAND_LEFT + 40.0, ISLAND_TOP,
                                ISLAND_W, ISLAND_H, vx=800.0, vy=0.0)
        sprite = FakeSprite(*SPRITE_POS)
        events: list = []
        world.add_collision_listener(events.append)

        world.tick([sprite], 1 / 60)

        assert [e for e in events if ISLAND in (e.a, e.b)]
        assert sprite.interaction_state == "thrown"
    finally:
        world.remove_static_member(ISLAND)


# ---------------------------------------------------------------- 守卫逐条
def test_geometry_animation_does_not_sample_island_speed():
    """几何动画期间（_geo_to 非 None）不采样：不得把旁边的鱼凭空拍飞。"""
    world = SpriteCollisionWorld()
    clock = FakeClock()
    bridge, _ = _bridge(dragging=True, geo_to=object(), clock=clock)
    try:
        bridge.attach(world)
        sprite = FakeSprite(*SPRITE_POS)
        events: list = []
        world.add_collision_listener(events.append)

        _moving_island(bridge, clock)  # 同样的位移：动画期间必须不采样
        assert world._static_member_velocity[ISLAND] == (0.0, 0.0)

        world.tick([sprite], 1 / 60)
        assert events == []
        assert sprite.interaction_state == "normal"
    finally:
        bridge.detach()


def test_size_change_resets_sample_point_without_estimate():
    """尺寸变化（展开/收起）只重置采样点：中心平移不当速度。"""
    world = SpriteCollisionWorld()
    clock = FakeClock()
    bridge, _ = _bridge(dragging=True, clock=clock)
    try:
        bridge.attach(world)
        _moving_island(bridge, clock)
        assert world._static_member_velocity[ISLAND][0] == pytest.approx(800.0)

        clock.advance(0.05)
        bridge.update_geometry(ISLAND_LEFT + 40.0, ISLAND_TOP, ISLAND_W + 60.0,
                               ISLAND_H)  # size 变了：清零 + 重置采样点
        assert world._static_member_velocity[ISLAND] == (0.0, 0.0)
    finally:
        bridge.detach()


def test_dense_callbacks_skip_sample_without_zeroing():
    """dt<0.01s 的高频回调「跳过」而不是清零：拖拽全程岛速不能恒为 0。"""
    world = SpriteCollisionWorld()
    clock = FakeClock()
    bridge, _ = _bridge(dragging=True, clock=clock)
    try:
        bridge.attach(world)
        _moving_island(bridge, clock)
        assert world._static_member_velocity[ISLAND][0] == pytest.approx(800.0)

        clock.advance(0.001)  # 样本太密：跳过（保留上次速度、不刷新采样点）
        bridge.update_geometry(ISLAND_LEFT + 80.0, ISLAND_TOP, ISLAND_W, ISLAND_H)
        assert world._static_member_velocity[ISLAND][0] == pytest.approx(800.0)
    finally:
        bridge.detach()


def test_teleport_jump_is_not_sampled():
    """瞬移跳变守卫：换屏/配置夹回造成的跳变不参与估计。"""
    world = SpriteCollisionWorld()
    clock = FakeClock()
    bridge, _ = _bridge(dragging=True, clock=clock)
    try:
        bridge.attach(world)
        bridge.update_geometry(ISLAND_LEFT, ISLAND_TOP, ISLAND_W, ISLAND_H)
        clock.advance(0.02)
        bridge.update_geometry(5000.0, ISLAND_TOP, ISLAND_W, ISLAND_H)  # 瞬移
        assert world._static_member_velocity[ISLAND] == (0.0, 0.0)
    finally:
        bridge.detach()


def test_overspeed_is_clamped_only_while_dragging():
    """仅拖拽中允许把超速钳到 _MAX_ISLAND_SPEED；非拖拽的极速位移清零。"""
    # 非拖拽：真实拖拽之外的岛移动没有合法高速来源 → 清零
    world = SpriteCollisionWorld()
    clock = FakeClock()
    bridge, _ = _bridge(dragging=False, clock=clock)
    try:
        bridge.attach(world)
        bridge.update_geometry(ISLAND_LEFT, ISLAND_TOP, ISLAND_W, ISLAND_H)
        clock.advance(0.02)
        bridge.update_geometry(ISLAND_LEFT + 60.0, ISLAND_TOP, ISLAND_W,
                               ISLAND_H)  # 3000px/s
        assert world._static_member_velocity[ISLAND] == (0.0, 0.0)
    finally:
        bridge.detach()

    # 拖拽中：合法甩动钳到上限
    world2 = SpriteCollisionWorld()
    clock2 = FakeClock()
    bridge2, _ = _bridge(dragging=True, clock=clock2)
    try:
        bridge2.attach(world2)
        bridge2.update_geometry(ISLAND_LEFT, ISLAND_TOP, ISLAND_W, ISLAND_H)
        clock2.advance(0.02)
        bridge2.update_geometry(ISLAND_LEFT + 60.0, ISLAND_TOP, ISLAND_W, ISLAND_H)
        assert world2._static_member_velocity[ISLAND] == (
            pytest.approx(MAX_ISLAND_SPEED), 0.0)
    finally:
        bridge2.detach()


def test_island_stop_with_unchanged_rect_zeroes_velocity():
    """岛停下时 rect 没变，速度也必须归零（否则旧速度残留继续拍鱼）。"""
    world = SpriteCollisionWorld()
    clock = FakeClock()
    bridge, _ = _bridge(dragging=True, clock=clock)
    try:
        bridge.attach(world)
        _moving_island(bridge, clock)
        assert world._static_member_velocity[ISLAND][0] == pytest.approx(800.0)

        clock.advance(0.05)
        bridge.update_geometry(ISLAND_LEFT + 40.0, ISLAND_TOP, ISLAND_W,
                               ISLAND_H)  # rect 不变 = 岛停了
        assert world._static_member_velocity[ISLAND] == (0.0, 0.0)

        sprite = FakeSprite(*SPRITE_POS)
        events: list = []
        world.add_collision_listener(events.append)
        world.tick([sprite], 1 / 60)
        assert events == []
        assert sprite.interaction_state == "normal"
    finally:
        bridge.detach()
