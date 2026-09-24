# -*- coding: utf-8 -*-
"""pet/island_bridge.py 的 offscreen 测试（4.3 前段：岛桥迁入 pet/ + 产品化）。

覆盖：
- 核心桥 ``IslandCollisionBridge``（零 Qt，几何经参数喂入）：attach 注册 /
  几何更新 / 去注册零残留 / stadium 圆链口径 / 幂等（重复 attach、同几何
  重复同步不打脏）/ 显隐 / 缺省几何与负尺寸/非有限值防御 / bump 回调
  （阈值、方向、强度封顶、计数、非岛 pair）/ 音效 hook / 回调异常静默降级；
- Qt 适配层 ``IslandWindowBridge``：真实 QWidget 岛的几何与显隐跟踪、
  无参几何回调通道、事件过滤通道、``set_origin`` 屏迁移重定位、无效几何
  防御、双 attach 幂等、真实 ``DynamicIsland`` 构造接线、退出零残留；
- 模块级零 Qt 守卫（PySide6 只落在惰性事件过滤器里）。

纪律（AGENTS.md）：``QT_QPA_PLATFORM=offscreen``；同步直调 + Qt 同步发送的
Show/Hide 事件，不 sleep、不赌时序；碰撞世界断言沿用 ``test_overlay_peripherals``
的既有口径（无公开成员查询面，读 ``_static_members`` 等私有表）。
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QWidget

from pet import collision as collision_mod
from pet import island_bridge as island_bridge_mod
from pet.config import Config
from pet.dynamic_island import DynamicIsland
from pet.island_bridge import IslandCollisionBridge, IslandWindowBridge
from pet.overlay_window import OverlayWindow
from pet.sprite_collision import (
    STATIC_HIT_MIN_DV,
    CollisionEvent,
    SpriteCollisionWorld,
    capsule_circles,
)

app = QApplication.instance() or QApplication([])

ISLAND = collision_mod.ISLAND_MEMBER_ID


# ---------------------------------------------------------------- 假件
class FakePoint:
    """QPointF 的鸭子类型替身（碰撞世界按 pos.x()/pos.y() 消费）。"""

    def __init__(self, x=0.0, y=0.0):
        self._x, self._y = float(x), float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class FakeRect:
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = float(x), float(y), float(w), float(h)

    def x(self):
        return self._x

    def y(self):
        return self._y

    def width(self):
        return self._w

    def height(self):
        return self._h


class FakeSprite:
    """碰撞世界最小协议 sprite（零 Qt，口径同 test_sprite_collision）。"""

    def __init__(self, x, y, w=60.0, h=60.0, vx=0.0, vy=0.0, collision_id="fish"):
        self.pos = FakePoint(x, y)
        self.velocity = FakePoint(vx, vy)
        self._w, self._h = float(w), float(h)
        self.scale = 1.0
        self.dragging = False
        self.interaction_state = "normal"
        self.collision_id = collision_id

    def rect(self):
        return FakeRect(self.pos.x(), self.pos.y(), self._w, self._h)

    def set_pos(self, pos):
        self.pos = FakePoint(pos.x(), pos.y())

    def set_velocity(self, velocity):
        self.velocity = FakePoint(velocity.x(), velocity.y())


class RecordingWorld:
    """只记录调用的零 Qt 假碰撞世界（数调用次数，验证幂等/零残留）。"""

    static_hit_min_dv = STATIC_HIT_MIN_DV

    def __init__(self):
        self.members: dict = {}
        self.circles: dict = {}
        self.velocities: dict = {}
        self.add_calls = 0
        self.remove_calls = 0
        self.listeners: list = []

    def add_static_member(self, member_id, left, top, width, height, *,
                          circles=None, vx=0.0, vy=0.0):
        self.add_calls += 1
        self.members[str(member_id)] = (left, top, width, height)
        self.velocities[str(member_id)] = (vx, vy)
        if circles is not None:
            self.circles[str(member_id)] = [list(c) for c in circles]

    def remove_static_member(self, member_id):
        self.remove_calls += 1
        self.members.pop(str(member_id), None)
        self.circles.pop(str(member_id), None)
        self.velocities.pop(str(member_id), None)

    def add_collision_listener(self, listener):
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_collision_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)


class StubIsland(QWidget):
    """几何/可见性可控的桩岛（QWidget 自身会钳制 0 尺寸，防御路径要直喂）。

    几何走 ``geometry()`` 覆盖（Python 调用方拿到桩值），bump 记录调用；
    真 QWidget 只为 ``installEventFilter`` 提供宿主。
    """

    def __init__(self):
        super().__init__()
        self.on_geometry_changed = None
        self.bump_calls: list = []
        self._geo = QRect(0, 0, 200, 44)
        self._visible = True

    def set_stub_geometry(self, rect):
        self._geo = QRect(rect)

    def set_stub_visible(self, visible):
        self._visible = bool(visible)

    def geometry(self):
        return QRect(self._geo)

    def isVisible(self):  # noqa: N802 (Qt 命名，鸭子协议)
        return self._visible

    def bump(self, strength=1.0, dir_x=0.0, dir_y=0.0):
        self.bump_calls.append((strength, dir_x, dir_y))


class FakeIsland(QWidget):
    """真实 QWidget 假岛：geometry/isVisible/显隐事件全走 Qt，bump 记录调用。"""

    def __init__(self):
        super().__init__()
        self.on_geometry_changed = None
        self.bump_calls: list = []

    def bump(self, strength=1.0, dir_x=0.0, dir_y=0.0):
        self.bump_calls.append((strength, dir_x, dir_y))

    def emit_geometry_changed(self):
        """模拟 DynamicIsland._emit_geometry_changed 的无参回调。"""
        if callable(self.on_geometry_changed):
            self.on_geometry_changed()


class StubSound:
    def __init__(self, *, boom: bool = False):
        self.events: list = []
        self._boom = boom

    def on_collision(self, event):
        if self._boom:
            raise RuntimeError("sound backend down")
        self.events.append(event)


def _core(**kwargs):
    """核心桥 + bump 记录表。"""
    bumps: list = []
    bridge = IslandCollisionBridge(
        bump=lambda s, dx, dy: bumps.append((s, dx, dy)), **kwargs)
    return bridge, bumps


def _island_event(j, *, a="sprite-1", b=ISLAND, nx=0.6, ny=0.8) -> CollisionEvent:
    return CollisionEvent(tick=1, pair="|".join(sorted([a, b])), a=a, b=b,
                          j=j, nx=nx, ny=ny, contact_x=0.0, contact_y=0.0)


# ---------------------------------------------------------------- 核心桥：注册/更新/去注册
def test_core_registers_member_only_after_valid_geometry():
    world = SpriteCollisionWorld()
    bridge, _ = _core()
    try:
        bridge.attach(world)
        assert bridge.attached and not bridge.registered
        assert ISLAND not in world._static_members  # 几何缺省 = 不注册
        assert bridge.update_geometry(120.0, 60.0, 400.0, 80.0) is True
        assert world._static_members[ISLAND] == (120.0, 60.0, 400.0, 80.0)
        assert bridge.registered and bridge.geometry == (120.0, 60.0, 400.0, 80.0)
    finally:
        bridge.detach()
    assert ISLAND not in world._static_members


def test_core_static_member_uses_stadium_capsule_chain():
    """产品化补强：静态成员碰撞体 = city stadium 圆链（非内切三圆回退）。"""
    world = SpriteCollisionWorld()
    bridge, _ = _core()
    try:
        bridge.attach(world)
        bridge.update_geometry(100.0, 100.0, 400.0, 80.0)
        chain = world._static_member_circles[ISLAND]
        assert chain == capsule_circles(100.0, 100.0, 400.0, 80.0)
        assert chain != collision_mod.circles_from_rect(100.0, 100.0, 400.0, 80.0)
        # 高度按胶囊上限截断（island_collision._CAPSULE_HEIGHT 口径）
        assert all(c[2] == 22.0 for c in chain)
    finally:
        bridge.detach()


def test_core_update_geometry_moves_member_and_chain():
    world = SpriteCollisionWorld()
    bridge, _ = _core()
    try:
        bridge.attach(world)
        bridge.update_geometry(10.0, 20.0, 200.0, 44.0)
        bridge.update_geometry(640.0, 300.0, 200.0, 44.0)
        assert world._static_members[ISLAND] == (640.0, 300.0, 200.0, 44.0)
        assert world._static_member_circles[ISLAND] == capsule_circles(
            640.0, 300.0, 200.0, 44.0)
    finally:
        bridge.detach()


def test_core_detach_leaves_no_residue_after_world_tick():
    world = SpriteCollisionWorld()
    bridge, _ = _core()
    bridge.attach(world)
    bridge.update_geometry(100.0, 100.0, 400.0, 80.0)
    world.tick([FakeSprite(180.0, 80.0)], 0.016)
    assert ISLAND in world._prev_circles  # 扫掠快照已含岛
    bridge.detach()
    assert bridge.attached is False and bridge.registered is False
    assert ISLAND not in world._static_members
    assert ISLAND not in world._static_member_circles
    assert ISLAND not in world._prev_circles
    assert world._listeners == []
    bridge.detach()  # 幂等
    assert world._listeners == []


def test_core_attach_is_idempotent():
    world = RecordingWorld()
    bridge, _ = _core()
    bridge.attach(world)
    bridge.attach(world)
    assert len(world.listeners) == 1
    bridge.update_geometry(0.0, 0.0, 200.0, 44.0)
    assert list(world.members) == [ISLAND]
    bridge.attach(world)  # 重复挂载：仍只有一份监听器/成员
    assert len(world.listeners) == 1
    assert list(world.members) == [ISLAND]


def test_core_repeat_same_geometry_does_not_redirty_world():
    """同几何重复同步不再调 add_static_member（静止豁免不被无谓打脏）。"""
    world = RecordingWorld()
    bridge, _ = _core()
    bridge.attach(world)
    bridge.update_geometry(1.0, 2.0, 200.0, 44.0)
    assert world.add_calls == 1
    bridge.update_geometry(1.0, 2.0, 200.0, 44.0)
    bridge.set_visible(True)
    assert world.add_calls == 1
    assert world.remove_calls == 0
    bridge.update_geometry(1.0, 2.0, 200.0, 48.0)  # 真变化才重登记
    assert world.add_calls == 2


def test_core_detach_then_attach_restores_from_last_geometry():
    world = SpriteCollisionWorld()
    bridge, _ = _core()
    bridge.attach(world)
    bridge.update_geometry(30.0, 40.0, 200.0, 44.0)
    bridge.detach()
    assert ISLAND not in world._static_members
    bridge.attach(world)
    assert world._static_members[ISLAND] == (30.0, 40.0, 200.0, 44.0)


def test_core_attach_to_another_world_clears_old_one():
    first, second = RecordingWorld(), RecordingWorld()
    bridge, _ = _core()
    bridge.attach(first)
    bridge.update_geometry(0.0, 0.0, 200.0, 44.0)
    bridge.attach(second)
    assert first.members == {} and first.listeners == []
    assert list(second.members) == [ISLAND]


# ---------------------------------------------------------------- 核心桥：显隐与防御
def test_core_visibility_toggle_walls_and_unwalls():
    world = SpriteCollisionWorld()
    bridge, _ = _core()
    try:
        bridge.attach(world)
        bridge.update_geometry(0.0, 0.0, 200.0, 44.0)
        bridge.set_visible(False)
        assert ISLAND not in world._static_members
        assert bridge.registered is False
        bridge.set_visible(True)
        assert world._static_members[ISLAND] == (0.0, 0.0, 200.0, 44.0)
    finally:
        bridge.detach()


def test_core_rejects_invalid_geometry_and_unwalls():
    world = SpriteCollisionWorld()
    bridge, _ = _core()
    try:
        bridge.attach(world)
        bridge.update_geometry(0.0, 0.0, 200.0, 44.0)
        for bad in ((0.0, 0.0, 0.0, 44.0),
                    (0.0, 0.0, 200.0, 0.0),
                    (0.0, 0.0, -10.0, 44.0),
                    (float("nan"), 0.0, 200.0, 44.0),
                    (0.0, float("inf"), 200.0, 44.0),
                    (0.0, 0.0, float("-inf"), 44.0),
                    (None, 0.0, 200.0, 44.0),
                    ("x", 0.0, 200.0, 44.0)):
            assert bridge.update_geometry(*bad) is False
            assert ISLAND not in world._static_members
            assert bridge.registered is False
            assert bridge.geometry is None
        # 恢复有效几何 → 复墙
        assert bridge.update_geometry(0.0, 0.0, 200.0, 44.0) is True
        assert ISLAND in world._static_members
    finally:
        bridge.detach()


def test_core_sync_without_world_is_inert():
    bridge, bumps = _core()
    assert bridge.update_geometry(0.0, 0.0, 200.0, 44.0) is True
    bridge.set_visible(False)
    bridge.set_visible(True)
    assert not bridge.attached and not bridge.registered
    bridge.on_collision(_island_event(500.0))
    assert bumps == []


# ---------------------------------------------------------------- 核心桥：撞击反馈
def test_core_bump_feedback_threshold_direction_and_cap():
    world = SpriteCollisionWorld()  # static_hit_min_dv = 60
    bridge, bumps = _core()
    try:
        bridge.attach(world)
        bridge.on_collision(_island_event(100.0))  # b=岛 → 取 +n
        assert bumps == [(0.25, 0.6, 0.8)]         # min(3, 100/400)
        assert bridge.bumps == 1
        bridge.on_collision(_island_event(900.0, a=ISLAND, b="sprite-9"))  # a=岛 → 取 -n
        assert bumps[-1] == (2.25, -0.6, -0.8)
        bridge.on_collision(_island_event(4000.0))
        assert bumps[-1][0] == 3.0                 # 强度封顶
        assert bridge.bumps == 3
    finally:
        bridge.detach()


def test_core_bump_feedback_ignores_light_and_non_island_pairs():
    world = SpriteCollisionWorld()
    bridge, bumps = _core()
    try:
        bridge.attach(world)
        bridge.on_collision(_island_event(30.0))               # 阈值下
        bridge.on_collision(_island_event(500.0, a="x", b="y"))  # 非岛 pair
        assert bumps == [] and bridge.bumps == 0
        # detach 后世界不再回调（直接调用也不发反馈）
        bridge.detach()
        bridge.on_collision(_island_event(500.0))
        assert bumps == []
    finally:
        bridge.detach()


def test_core_sound_hook_only_above_threshold_and_failure_is_silent():
    world = SpriteCollisionWorld()
    sound = StubSound()
    bridge, bumps = _core(sound=sound)
    try:
        bridge.attach(world)
        bridge.on_collision(_island_event(10.0))
        assert sound.events == []
        bridge.on_collision(_island_event(120.0))
        assert len(sound.events) == 1 and bridge.bumps == 1
        # 音效后端抛异常：bump 已发生，桥不崩
        boom = StubSound(boom=True)
        bridge2, bumps2 = _core(sound=boom)
        bridge2.attach(world)
        bridge2.on_collision(_island_event(120.0))
        assert bumps2 and bridge2.bumps == 1
        bridge2.detach()
    finally:
        bridge.detach()


def test_core_bump_callback_failure_is_silent():
    def boom(_strength, _dx, _dy):
        raise RuntimeError("island gone")

    world = SpriteCollisionWorld()
    bridge = IslandCollisionBridge(bump=boom)
    try:
        bridge.attach(world)
        bridge.on_collision(_island_event(500.0))  # 不抛
        assert bridge.bumps == 0                   # 反馈未成功，不计数
    finally:
        bridge.detach()


def test_core_island_wall_blocks_sprite_and_records_bump_through_world_tick():
    """端到端：桥登记的城墙真的挡 sprite，真撞击经 listener 回到 bump 回调。"""
    world = SpriteCollisionWorld()
    bridge, bumps = _core()
    try:
        bridge.attach(world)
        bridge.update_geometry(100.0, 100.0, 400.0, 80.0)  # 宽扁岛（胶囊中段）
        sprite = FakeSprite(180.0, 80.0, vy=900.0)         # 撞向胶囊中段
        world.tick([sprite], 0.016)
        assert bumps, "岛静态成员未产生真撞击反馈"
        assert bridge.bumps == len(bumps)
        assert sprite.pos.y() != 80.0  # 被墙挡住（位置分离/回弹）
    finally:
        bridge.detach()


# ---------------------------------------------------------------- Qt 适配层
def test_window_bridge_tracks_real_widget_geometry_and_visibility():
    overlay = OverlayWindow()
    origin = overlay.geometry().topLeft()
    world = SpriteCollisionWorld()
    island = FakeIsland()
    island.setGeometry(500, 300, 200, 44)
    island.show()
    bridge = IslandWindowBridge(island, world, overlay)
    try:
        local = (500 - origin.x(), 300 - origin.y(), 200, 44)
        assert world._static_members[ISLAND] == local
        assert world._static_member_circles[ISLAND] == capsule_circles(*local)
        assert bridge.registered and bridge.attached
        # 事件过滤通道：Move/Resize（setGeometry）即同步
        island.setGeometry(620, 340, 200, 44)
        moved = (620 - origin.x(), 340 - origin.y(), 200, 44)
        assert world._static_members[ISLAND] == moved
        # 无参几何回调通道（接线口径同 app.py on_geometry_changed）
        island.setGeometry(640, 360, 180, 44)
        island.emit_geometry_changed()
        assert world._static_members[ISLAND] == (
            640 - origin.x(), 360 - origin.y(), 180, 44)
        # Hide/Show 事件过滤 → 撤墙/复墙
        island.hide()
        assert ISLAND not in world._static_members
        island.show()
        assert ISLAND in world._static_members
    finally:
        bridge.close()
        island.close()
    assert ISLAND not in world._static_members
    assert world._listeners == []
    assert island.on_geometry_changed is None


def test_window_bridge_geometry_callback_and_visibility_with_stub():
    overlay = OverlayWindow()
    origin = overlay.geometry().topLeft()
    world = SpriteCollisionWorld()
    island = StubIsland()
    bridge = IslandWindowBridge(island, world, overlay)
    try:
        assert world._static_members[ISLAND] == (
            0 - origin.x(), 0 - origin.y(), 200, 44)
        island.set_stub_geometry(QRect(300, 220, 260, 44))
        island.on_geometry_changed()  # 无参回调通道
        assert world._static_members[ISLAND] == (
            300 - origin.x(), 220 - origin.y(), 260, 44)
        island.set_stub_visible(False)
        bridge.sync_geometry()
        assert ISLAND not in world._static_members
        island.set_stub_visible(True)
        bridge.sync_geometry()
        assert ISLAND in world._static_members
    finally:
        bridge.close()
        island.close()


def test_window_bridge_set_origin_relocalizes_geometry():
    overlay = OverlayWindow()
    origin = overlay.geometry().topLeft()
    world = SpriteCollisionWorld()
    island = StubIsland()
    bridge = IslandWindowBridge(island, world, overlay)
    try:
        island.set_stub_geometry(QRect(400, 300, 200, 44))
        bridge.sync_geometry()
        assert world._static_members[ISLAND] == (
            400 - origin.x(), 300 - origin.y(), 200, 44)
        # 屏迁移：overlay 原点右移下移 → 同一岛几何的局部坐标随之平移
        bridge.set_origin(QPoint(origin.x() + 40, origin.y() + 30))
        assert world._static_members[ISLAND] == (
            400 - origin.x() - 40, 300 - origin.y() - 30, 200, 44)
    finally:
        bridge.close()
        island.close()


def test_window_bridge_rejects_invalid_island_geometry():
    world = SpriteCollisionWorld()
    island = StubIsland()
    bridge = IslandWindowBridge(island, world)  # overlay=None → 原点 (0,0)
    try:
        assert world._static_members[ISLAND] == (0.0, 0.0, 200.0, 44.0)
        island.set_stub_geometry(QRect(10, 10, 0, 44))
        bridge.sync_geometry()
        assert ISLAND not in world._static_members
        island.set_stub_geometry(QRect(10, 10, -5, 44))
        bridge.sync_geometry()
        assert ISLAND not in world._static_members
        island.set_stub_geometry(QRect(10, 10, 200, 44))
        bridge.sync_geometry()
        assert world._static_members[ISLAND] == (10.0, 10.0, 200.0, 44.0)
    finally:
        bridge.close()
        island.close()


def test_window_bridge_bump_feedback_uses_island_bump_and_sound():
    overlay = OverlayWindow()
    world = SpriteCollisionWorld()
    island = FakeIsland()
    island.show()
    sound = StubSound()
    bridge = IslandWindowBridge(island, world, overlay, sound=sound)
    try:
        bridge.on_collision(_island_event(100.0))
        assert island.bump_calls == [(0.25, 0.6, 0.8)]
        assert len(sound.events) == 1
        assert bridge.bumps == 1
        bridge.on_collision(_island_event(20.0))  # 阈值下
        assert len(island.bump_calls) == 1 and bridge.bumps == 1
    finally:
        bridge.close()
        island.close()


def test_window_bridge_attach_is_idempotent_and_detach_restores():
    world = RecordingWorld()
    island = StubIsland()
    bridge = IslandWindowBridge(island, world)
    try:
        bridge.attach(world)
        bridge.attach(world)
        assert len(world.listeners) == 1
        assert list(world.members) == [ISLAND]
        bridge.detach()
        assert world.members == {} and world.listeners == []
        assert island.on_geometry_changed is None
        bridge.attach(world)  # 摘线后重挂：接线与墙都回来
        assert len(world.listeners) == 1
        assert list(world.members) == [ISLAND]
        assert island.on_geometry_changed is not None
    finally:
        bridge.close()
        island.close()


def test_window_bridge_real_dynamic_island_registers_in_world(tmp_path):
    overlay = OverlayWindow()
    origin = overlay.geometry().topLeft()
    world = SpriteCollisionWorld()
    cfg = Config(base=tmp_path)
    island = DynamicIsland(cfg)
    bridge = IslandWindowBridge(island, world, overlay, sound=StubSound())
    try:
        island.show()
        g = island.geometry()
        assert g.width() > 0 and g.height() > 0
        assert world._static_members[ISLAND] == (
            g.x() - origin.x(), g.y() - origin.y(), g.width(), g.height())
        assert world._static_member_circles[ISLAND] == capsule_circles(
            g.x() - origin.x(), g.y() - origin.y(), g.width(), g.height())
        bridge.on_collision(_island_event(200.0))  # 真实岛 bump 调用不炸
    finally:
        bridge.close()
        island.close()
    assert ISLAND not in world._static_members
    assert world._listeners == []


def test_window_bridge_warns_when_replacing_foreign_geometry_callback(caplog):
    """单槽回调已被旧路径占用 → WARNING（接线诊断：两条路径不得同挂）。"""
    import logging

    world = RecordingWorld()
    island = StubIsland()
    island.on_geometry_changed = lambda: None  # 模拟旧 island_collision 路径已占用
    with caplog.at_level(logging.WARNING, logger="pet.island_bridge"):
        bridge = IslandWindowBridge(island, world)
    try:
        assert any("on_geometry_changed" in record.message for record in caplog.records)
        assert island.on_geometry_changed is not None  # 已被本桥接管
    finally:
        bridge.close()
        island.close()


# ---------------------------------------------------------------- 零 Qt 守卫
def test_qt_import_is_confined_to_lazy_event_filter():
    """核心桥零 Qt：全模块只有一处 PySide6 import，且在惰性过滤器函数体内。"""
    src = Path(island_bridge_mod.__file__).read_text(encoding="utf-8")
    qt_imports = [ln for ln in src.splitlines()
                  if ln.lstrip().startswith(("import PySide6", "from PySide6"))]
    assert len(qt_imports) == 1, f"PySide6 只应有一处惰性 import：{qt_imports}"
    assert qt_imports[0].startswith("    ")  # 缩进 = 函数体内，非模块级
    assert "import QEvent, QObject" in qt_imports[0]
