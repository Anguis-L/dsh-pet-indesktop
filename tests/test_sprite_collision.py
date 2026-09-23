# -*- coding: utf-8 -*-
"""pet/sprite_collision.py 的纯逻辑测试（零 Qt：轻量假 sprite 鸭子类型）。

覆盖：两体弹性碰撞、拖拽方无限质量、扫掠防穿透、位置分离去抖、真撞击
置 thrown + 限速、静态成员反弹（灵动岛预留 API）、on_collision 回调。
"""

import math

from pet import collision
from pet.sprite_collision import (
    INTERACTION_DRAG,
    INTERACTION_NORMAL,
    INTERACTION_THROWN,
    SpriteCollisionWorld,
)


class FakePoint:
    """QPointF 的鸭子类型替身（world 用 type(pos)(x, y) 构造，保持零 Qt）。"""

    def __init__(self, x=0.0, y=0.0):
        self._x = float(x)
        self._y = float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class FakeRect:
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = x, y, w, h

    def x(self):
        return self._x

    def y(self):
        return self._y

    def width(self):
        return self._w

    def height(self):
        return self._h


class FakeSprite:
    """协议鸭子类型：pos/set_pos/velocity/set_velocity/rect/center/radius/
    dragging/interaction_state/scale/facing（+ 可选 collision_id）。"""

    def __init__(self, x, y, w=100.0, h=100.0, vx=0.0, vy=0.0,
                 scale=0.72, collision_id=""):
        self.pos = FakePoint(x, y)
        self.velocity = FakePoint(vx, vy)
        self._w, self._h = float(w), float(h)
        self.scale = float(scale)
        self.facing = "left"
        self.dragging = False
        self.interaction_state = INTERACTION_NORMAL
        self.collision_id = collision_id

    def rect(self):
        return FakeRect(self.pos.x(), self.pos.y(), self._w, self._h)

    def center(self):
        return FakePoint(self.pos.x() + self._w / 2.0, self.pos.y() + self._h / 2.0)

    def radius(self):
        return 0.45 * min(self._w, self._h)

    def set_pos(self, pos):
        self.pos = FakePoint(pos.x(), pos.y())

    def set_velocity(self, velocity):
        self.velocity = FakePoint(velocity.x(), velocity.y())

    def speed(self):
        return math.hypot(self.velocity.x(), self.velocity.y())


def test_head_on_elastic_collision_exchanges_velocity():
    """两体正面对撞（等质量、e=1、无摩擦）：速度交换。"""
    world = SpriteCollisionWorld(restitution=1.0, friction=0.0)
    a = FakeSprite(0, 0, vx=500, collision_id="a")
    b = FakeSprite(90, 0, vx=-500, collision_id="b")  # 圆链重叠 10px
    world.tick([a, b], 0.016)
    assert a.velocity.x() == -500.0
    assert b.velocity.x() == 500.0
    assert abs(a.velocity.y()) < 1e-9
    assert abs(b.velocity.y()) < 1e-9


def test_momentum_conserved_with_default_restitution():
    """默认恢复系数 0.82：动量守恒、动能损失、方向对调。"""
    world = SpriteCollisionWorld()  # e=0.82, friction=0.08
    a = FakeSprite(0, 0, vx=500, collision_id="a")
    b = FakeSprite(90, 0, vx=-500, collision_id="b")
    world.tick([a, b], 0.016)
    # 等质量正碰：动量（初态为 0）守恒
    assert abs(a.velocity.x() + b.velocity.x()) < 1e-6
    assert a.velocity.x() < 0 < b.velocity.x()
    # e<1：末速小于初速
    assert abs(a.velocity.x()) < 500.0


def test_dragged_sprite_is_infinite_mass():
    """拖拽中 = 无限质量：撞来的停住（非静态无限质量 e=0 吸能），被握的岿然不动。"""
    world = SpriteCollisionWorld()
    a = FakeSprite(0, 0, vx=500, collision_id="a")
    b = FakeSprite(90, 0, vx=0, collision_id="b")
    b.dragging = True
    b.interaction_state = INTERACTION_DRAG
    world.tick([a, b], 0.016)
    # 撞来的一方速度被吸停（e=0：贴停不弹飞）
    assert abs(a.velocity.x()) < 1e-9
    assert a.interaction_state == INTERACTION_THROWN  # dv=500 >= 300 → 真撞击
    # 被拖拽的一方：速度与位置都不动、状态不被改写
    assert b.velocity.x() == 0.0 and b.velocity.y() == 0.0
    assert b.pos.x() == 90.0 and b.pos.y() == 0.0
    assert b.interaction_state == INTERACTION_DRAG


def test_swept_collision_prevents_tunneling():
    """高速小球一 tick 飞越静止大球：两帧快照都不重叠，扫掠仍命中。"""
    world = SpriteCollisionWorld()
    ball = FakeSprite(0, 0, w=20, h=20, vx=20000, collision_id="ball")
    wall = FakeSprite(400, 0, w=100, h=100, vx=0, collision_id="wall")
    events = []
    world.add_collision_listener(events.append)
    # tick 1：相距远，无碰撞；登记帧末快照
    results = world.tick([ball, wall], 0.016)
    assert results == []
    # 模拟 advance：20000px/s × 0.03s = 600px——直接飞越 wall（400..500）
    ball.set_pos(FakePoint(600, 0))
    # tick 2：当前快照 ball 在 wall 右侧 100px 外，不重叠；上一帧在左侧——
    # 只有扫掠能抓到这次穿越
    world.tick([ball, wall], 0.03)
    assert wall.velocity.x() > 0.0        # 静止大球被撞动 = 扫掠命中
    assert ball.velocity.x() < 20000.0    # 小球被减速（未穿透了事）
    assert wall.interaction_state == INTERACTION_THROWN
    assert len(events) == 1
    assert events[0].pair == "ball|wall"


def test_position_only_separation_is_debounced():
    """纯位置分离（j=0）按 pair 去抖 15 tick：窗口内不反复推，防抖动。"""
    world = SpriteCollisionWorld()
    a = FakeSprite(0, 0, collision_id="a")
    b = FakeSprite(80, 0, collision_id="b")  # 静止重叠 20px，vn=0 → j=0
    world.tick([a, b], 0.016)  # tick 1：首次分离生效
    ax1, bx1 = a.pos.x(), b.pos.x()
    assert ax1 < 0.0 and bx1 > 80.0  # 确实被推开了
    for _ in range(14):  # tick 2..15：去抖窗口内，位置不许再动
        world.tick([a, b], 0.016)
    assert a.pos.x() == ax1 and b.pos.x() == bx1
    world.tick([a, b], 0.016)  # tick 16：窗口届满，分离再次生效
    assert a.pos.x() < ax1 and b.pos.x() > bx1


def test_light_touch_separates_without_thrown():
    """轻触（dv 低于真撞击阈值）：只分离，速度/状态都不变，不触发回调。"""
    world = SpriteCollisionWorld()
    a = FakeSprite(0, 0, vx=50, collision_id="a")   # 慢速接近（vn=-50 > -80 → e=0）
    b = FakeSprite(95, 0, vx=0, collision_id="b")
    events = []
    world.add_collision_listener(events.append)
    world.tick([a, b], 0.016)
    assert a.velocity.x() == 50.0  # 微冲量不吸收（非 thrown 且未达 300 阈值）
    assert b.velocity.x() == 0.0
    assert a.interaction_state == INTERACTION_NORMAL
    assert b.interaction_state == INTERACTION_NORMAL
    assert events == []


def test_real_hit_marks_thrown_and_soft_clamps_speed():
    """真撞击置 thrown；被撞方末速过 soft_clamp_speed 软上限（不超 cap）。"""
    world = SpriteCollisionWorld()
    a = FakeSprite(0, 0, vx=20000, collision_id="a")
    b = FakeSprite(90, 0, vx=0, collision_id="b")
    world.tick([a, b], 0.016)
    assert a.interaction_state == INTERACTION_THROWN
    assert b.interaction_state == INTERACTION_THROWN
    cap = 6000.0  # physics.MAX_THROW_SPEED
    assert b.speed() <= cap
    assert b.speed() > 0.0
    # 软膝曲线是渐近的：未被硬钳成恰等于 cap
    assert b.speed() < cap


def test_already_thrown_absorbs_contact_impulse_above_floor():
    """已 thrown 的成员继续吸收 >= 50px/s 的接触冲量（CONTACT_DV_FLOOR）。"""
    world = SpriteCollisionWorld()
    a = FakeSprite(0, 0, vx=0, collision_id="a")
    b = FakeSprite(90, 0, vx=-150, collision_id="b")  # vn=-150：e=0.82 真撞前…dv≈136<300
    b.interaction_state = INTERACTION_THROWN
    world.tick([a, b], 0.016)
    # b 的 dv ≈ -(1.82)(-150)/2 = 136.5：未达 300 但 >= 50 且已 thrown → 吸收
    assert b.velocity.x() > -150.0
    # a 仍是 normal 且 dv < 300 → 不吸收、不置 thrown
    assert a.velocity.x() == 0.0
    assert a.interaction_state == INTERACTION_NORMAL


def test_static_member_bounces_with_trampoline_restitution():
    """静态成员（灵动岛预留 API）：STATIC_RESTITUTION=1.3 果冻墙加速弹开。"""
    world = SpriteCollisionWorld()
    world.add_static_member("island", 300, 0, 40, 200)
    # A 圆心 (280,100) 正对岛中圆 (320,100)：法线纯 +x，切向速度为零不吃摩擦
    a = FakeSprite(230, 50, vx=500, collision_id="a")
    events = []
    world.add_collision_listener(events.append)
    world.tick([a], 0.016)
    # e=1.3：vn=-500 → dv_a=-1150，末速 500-1150=-650（出射/入射 = 1.3，加速弹开）
    assert a.velocity.x() == -650.0
    assert a.interaction_state == INTERACTION_THROWN
    # 撞静态成员命中阈值放宽到 60：dv=1150 >> 60，回调触发
    assert len(events) == 1
    assert events[0].pair == "a|island"
    # 注销后不再参与结算
    world.remove_static_member("island")
    a.set_velocity(FakePoint(500, 0))
    a.set_pos(FakePoint(230, 50))
    a.interaction_state = INTERACTION_NORMAL
    world.tick([a], 0.016)
    assert a.velocity.x() == 500.0
    assert a.interaction_state == INTERACTION_NORMAL
    assert len(events) == 1


def test_on_collision_listener_receives_hit_details():
    """on_collision 回调负载：撞击对、冲量大小、接触点、法线。"""
    world = SpriteCollisionWorld()
    a = FakeSprite(0, 0, vx=500, collision_id="a")
    b = FakeSprite(90, 0, vx=-500, collision_id="b")
    events = []
    world.add_collision_listener(events.append)
    world.tick([a, b], 0.016)
    assert len(events) == 1
    ev = events[0]
    assert ev.a == "a" and ev.b == "b"
    assert ev.j > 0.0
    assert ev.nx > 0.0 and abs(ev.ny) < 1e-9  # 法线从 a 指向 b（+x）
    # 接触点在两圆心之间（50 与 140 之间）
    assert 50.0 < ev.contact_x < 140.0
    assert ev.tick == 1
    # 注销后不再触发
    world.remove_collision_listener(events.append)
    a.set_velocity(FakePoint(500, 0))
    b.set_velocity(FakePoint(-500, 0))
    world.tick([a, b], 0.016)
    assert len(events) == 1


def test_member_flags_match_coordinator_semantics():
    """快照语义：拖拽 → FLAG_DRAGGING + 无限质量；静态 → FLAG_STATIC + 无限质量。"""
    world = SpriteCollisionWorld()
    sprite = FakeSprite(0, 0, collision_id="s")
    member = world._member_from_sprite(sprite)
    assert member.flags & collision.FLAG_VISIBLE
    assert member.flags & collision.FLAG_COLLISION_ENABLED
    assert not member.is_infinite_mass
    sprite.dragging = True
    dragged = world._member_from_sprite(sprite)
    assert dragged.flags & collision.FLAG_DRAGGING
    assert dragged.is_infinite_mass
    world.add_static_member("isle", 0, 0, 40, 200)
    static = world._static_member_state("isle", world._static_members["isle"])
    assert static.flags & collision.FLAG_STATIC
    assert static.is_infinite_mass
