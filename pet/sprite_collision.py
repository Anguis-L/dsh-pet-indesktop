# -*- coding: utf-8 -*-
"""进程内碰撞世界（单合成窗架构 Phase 2）：多 sprite 碰撞的每 tick 直调结算。

架构背景见 .scratch/single-overlay-window/spec.md：碰撞从"跨进程 QLocal
协调者（选举/心跳/快照/冲量编解码转发）"退化为进程内直调——overlay 的
before_sprites_advance 钩子里调用 tick(sprites, dt)，无 IPC、无选举、
无快照版本协商，数学全部复用 pet/collision.py（零 Qt 纯 Python）。

与现架构（collision_ipc._coordinator_tick + collision_client._on_collision_impulse）
的语义对照：

保留：
- 拖拽中的 sprite 视为无限质量（撞得动别人，自己不动；FLAG_DRAGGING 语义）；
- 静态成员（FLAG_STATIC 语义，为灵动岛预留）无限质量 + STATIC_RESTITUTION
  果冻墙弹性（collision.py 的 solve_collision_impulse 内部按 FLAG_STATIC 分支）；
- 高速防穿透：帧间圆链扫掠（swept_circle_chain_collision，TOI 语义）；
- 纯位置分离（j==0 且 sep>0）按 pair 去抖 15 tick（防贴贴抖动）；
- 真撞击阈值：普通对 dv >= 300px/s、撞静态成员放宽到 60px/s、已 thrown
  成员继续吸收冲量的下限 50px/s（对齐 window.py 的 COLLISION_HIT_MIN_DV /
  COLLISION_CONTACT_DV_FLOOR 与 collision_client 的静态放宽分支）；
- 速度写回过 soft_clamp_speed 软上限（对齐 collision_client 的限速分支）。

简化（进程内直调后自然消亡）：
- 无 epoch/watermark 去重、无 seq 版本化扫掠（每 tick 全量重算，N 很小）、
  无 contact deviation 偏差豁免（协调者与客户端不再有时序差）、
  无 predicted bounce 对账、无快照发布/编解码。

本模块刻意保持零 Qt：交互状态协议字面量（"normal"/"drag"/"thrown"）与
pet/pet_sprite.py 的 INTERACTION_* 常量保持一致，但不 import pet_sprite
（它依赖 PySide6）；sprite 按鸭子类型消费（pos/set_pos/velocity/set_velocity/
rect()/interaction_state/dragging/scale），纯逻辑可脱离 QApplication 单测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Tuple

from . import collision
from . import physics

# 交互状态协议字面量：与 pet/pet_sprite.py 的 INTERACTION_NORMAL/DRAG/THROWN
# 一一对应（不 import 以保持零 Qt，见模块 docstring）。
INTERACTION_NORMAL = "normal"
INTERACTION_DRAG = "drag"
INTERACTION_THROWN = "thrown"

# 岛胶囊视觉高度（口径源 island_collision._CAPSULE_HEIGHT=44，两边必须同步——
# 体育场碰撞体的高度上限：展开卡片时只覆盖胶囊本体，卡片区域不设幽灵墙）
ISLAND_CAPSULE_HEIGHT = 44


def capsule_circles(left: float, top: float, width: float, height: float,
                    *, height_cap: float = ISLAND_CAPSULE_HEIGHT) -> list[list[float]]:
    """体育场（胶囊）等效圆链：对齐旧架构 island_collision._island_stadium。

    高度先按 height_cap 截断（展开卡片时卡片区域不设墙），半径 r=h/2，
    轴线 y=top+r、x∈[left+r, left+width-r]，圆沿轴线以 ≤r 间距铺满——
    circles_from_rect 的内切三圆在宽扁矩形（如岛 400×80）上两端圆心
    间距可达 3r 以上，胶囊中段会出现可穿入的空档；本函数的圆链连续
    覆盖整条轴线，与旧 stadium 钳制的几何口径一致。
    """
    h = min(float(height), float(height_cap))
    r = h / 2.0
    axis_y = float(top) + r
    x0 = float(left) + r
    x1 = float(left) + float(width) - r
    if x1 <= x0:
        return [[float(left) + float(width) / 2.0, axis_y, r]]
    span = x1 - x0
    n = max(2, int(math.ceil(span / r)) + 1)
    step = span / (n - 1)
    return [[x0 + i * step, axis_y, r] for i in range(n)]

# 真撞击阈值（语义对齐现架构 window.py:147-150 / collision_client.py:373-381）
HIT_MIN_DV = 300.0          # COLLISION_HIT_MIN_DV：普通对 |dv| 阈值 (px/s)
STATIC_HIT_MIN_DV = 60.0    # 撞静态布景放宽（岛的语义就是"撞上去会弹"）
CONTACT_DV_FLOOR = 50.0     # 已 thrown 成员继续吸收冲量的下限 (px/s)

# 纯位置分离去抖窗口（对齐 collision_ipc._coordinator_tick 的 15 tick 去抖）
SEPARATION_DEBOUNCE_TICKS = 15


@dataclass(frozen=True)
class CollisionEvent:
    """一次真撞击事件（on_collision 回调负载）。

    只对冲量非零且至少一侧达到真撞击阈值的 pair 触发；轻触（仅位置分离）
    不触发（对齐现架构"只有有分量的撞击才响"的音效纪律）。
    """
    tick: int
    pair: str           # "a|b"（字典序）
    a: str
    b: str
    j: float            # 法向冲量大小
    nx: float           # 碰撞法线（从 a 指向 b）
    ny: float
    contact_x: float    # 接触点（世界/overlay 局部坐标）
    contact_y: float


class SpriteCollisionWorld:
    """进程内碰撞世界：每 tick 把 sprites 快照成 MemberState，求解后写回。"""

    def __init__(
        self,
        *,
        restitution: float = collision.DEFAULT_RESTITUTION,
        friction: float = collision.DEFAULT_FRICTION,
        impulse_cap: float = collision.DEFAULT_IMPULSE_CAP,
        mass_scale: float = collision.DEFAULT_MASS_SCALE,
        hit_min_dv: float = HIT_MIN_DV,
        static_hit_min_dv: float = STATIC_HIT_MIN_DV,
        contact_dv_floor: float = CONTACT_DV_FLOOR,
        speed_cap: float = physics.MAX_THROW_SPEED,
        separation_debounce_ticks: int = SEPARATION_DEBOUNCE_TICKS,
        max_separation_iterations: int = 4,
    ) -> None:
        self.restitution = float(restitution)
        self.friction = float(friction)
        self.impulse_cap = float(impulse_cap)
        self.mass_scale = float(mass_scale)
        self.hit_min_dv = float(hit_min_dv)
        self.static_hit_min_dv = float(static_hit_min_dv)
        self.contact_dv_floor = float(contact_dv_floor)
        self.speed_cap = float(speed_cap)
        self.separation_debounce_ticks = int(separation_debounce_ticks)
        self.max_separation_iterations = int(max_separation_iterations)

        self._tick = 0
        self._overlap_history: Dict[str, int] = {}
        # 上一 tick 的圆链快照（member_id -> circles），供帧间扫掠防穿透
        self._prev_circles: Dict[str, Sequence[Sequence[float]]] = {}
        # 纯位置分离去抖：pair -> 最近一次实际应用分离的 tick
        self._position_only_ticks: Dict[str, int] = {}
        # 静态成员（灵动岛预留）：member_id -> (left, top, width, height)
        self._static_members: Dict[str, Tuple[float, float, float, float]] = {}
        self._listeners: List[Callable[[CollisionEvent], None]] = []
        # 静态成员自定义圆链（岛 stadium 口径，member_id -> circles）
        self._static_member_circles: Dict[str, list] = {}
        # 静止豁免（P1/③-1）：上 tick 的运动签名 + 静态成员脏标记。
        # 签名一致且无未结清交互时，求解结果可证不变，整 tick 跳过
        self._last_motion_sig: tuple | None = None
        self._static_dirty = False

    # ---------------------------------------------------------------- 静态成员（灵动岛预留 API）
    def add_static_member(self, member_id: str, left: float, top: float,
                          width: float, height: float,
                          *, circles: Sequence[Sequence[float]] | None = None) -> None:
        """注册静态碰撞成员（FLAG_STATIC 语义的矩形障碍/体育场）。

        默认碰撞体 = circles_from_rect 内切三圆；岛等宽扁胶囊应经
        circles=capsule_circles(...) 传入体育场等效圆链（D9 stadium 口径）。
        无限质量且保留 STATIC_RESTITUTION 果冻墙弹性（collision.py 内部分支）。
        """
        self._static_members[str(member_id)] = (
            float(left), float(top), float(width), float(height))
        if circles is not None:
            self._static_member_circles[str(member_id)] = [
                [float(c[0]), float(c[1]), float(c[2])] for c in circles]
        else:
            self._static_member_circles.pop(str(member_id), None)
        self._static_dirty = True

    def remove_static_member(self, member_id: str) -> None:
        """注销静态成员；未注册过是 no-op。"""
        self._static_members.pop(str(member_id), None)
        self._static_member_circles.pop(str(member_id), None)
        self._prev_circles.pop(str(member_id), None)
        self._static_dirty = True

    # ---------------------------------------------------------------- 事件回调
    def add_collision_listener(self, listener: Callable[[CollisionEvent], None]) -> None:
        """注册 on_collision 监听器（音效/UI 反馈由集成层接线）；重复注册是 no-op。"""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_collision_listener(self, listener: Callable[[CollisionEvent], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # ---------------------------------------------------------------- 主入口
    def tick(self, sprites: Sequence, dt: float) -> List[collision.ImpulseResult]:
        """结算一 tick：sprites → MemberState → 扫掠+多体求解 → 写回 sprite。

        在 overlay 的 before_sprites_advance 阶段调用（行为控制器之后、抛掷
        物理控制器之前）；本方法只改 sprite 的 velocity/pos/interaction_state，
        位置积分仍由 sprite.advance(dt) 统一完成。dt 当前不直接参与结算
        （扫掠用帧间位置快照而非速度外推），保留在签名里对齐 tick 协议。
        """
        del dt  # 见 docstring：结算不依赖 dt
        sig = self._motion_signature(sprites)
        if (sig == self._last_motion_sig
                and not self._static_dirty
                and not self._overlap_history
                and not self._position_only_ticks):
            # 静止豁免（P1/③-1）：无任何成员运动（位置/速度/交互态/缩放/
            # 成员集合全未变）、无静态成员变更、无未结清的重叠/分离去抖——
            # 求解结果可证与上 tick 相同，整 tick 跳过。静止期间岛（静态
            # 成员）变更经 _static_dirty 唤醒；外部 set_pos 经签名唤醒
            return []
        self._tick += 1
        members: List[collision.MemberState] = []
        sprite_by_id: Dict[str, object] = {}
        for sprite in sprites:
            member = self._member_from_sprite(sprite)
            members.append(member)
            sprite_by_id[member.runtime_id] = sprite
        for member_id, rect in self._static_members.items():
            members.append(self._static_member_state(member_id, rect))

        results: List[collision.ImpulseResult] = []
        if len(members) >= 2:
            swept = self._swept_collisions(members)
            results, _, self._overlap_history = collision.solve_multi_body_collision(
                members,
                tick=self._tick,
                overlap_history=self._overlap_history,
                restitution=self.restitution,
                friction=self.friction,
                impulse_cap=self.impulse_cap,
                max_separation_iterations=self.max_separation_iterations,
                swept_collisions=swept,
            )
            self._apply_results(results, sprite_by_id)
        # 帧末快照：下一 tick 扫掠的 prev（存的是本 tick 结算前的真实位置，
        # 与 coordinator 比较连续客户端快照的语义一致——位移/积分的实际
        # 轨迹段整体被下一帧扫掠覆盖，方向保守，绝不提前穿透）
        self._prev_circles = {
            m.runtime_id: m.circles for m in members if m.circles is not None
        }
        self._prune_position_only_ticks()
        self._last_motion_sig = sig
        self._static_dirty = False
        return results

    # ---------------------------------------------------------------- 内部：快照构造
    @staticmethod
    def _field(obj, name: str) -> float:
        """读数值字段（QPointF/FakePoint 的 x()/y() 方法或裸属性）。"""
        v = getattr(obj, name, None)
        if callable(v):
            return float(v())
        return float(v) if v is not None else 0.0

    @classmethod
    def _motion_signature(cls, sprites: Sequence) -> tuple:
        """成员运动签名（静止豁免判据）：成员集合 + 位置 + 速度 + 交互态 +
        缩放——结算读取的全部动态输入；任一变化即重新求解。"""
        items = []
        for sprite in sprites:
            pos = getattr(sprite, "pos", None)
            vel = getattr(sprite, "velocity", None)
            items.append((
                cls._member_id(sprite),
                cls._field(pos, "x"), cls._field(pos, "y"),
                cls._field(vel, "x"), cls._field(vel, "y"),
                getattr(sprite, "interaction_state", None),
                cls._is_dragging(sprite),
                float(getattr(sprite, "scale", 0.0) or 0.0),
            ))
        return tuple(items)

    @staticmethod
    def _member_id(sprite) -> str:
        """sprite 的碰撞世界成员 id：优先用 sprite.collision_id（若暴露），
        否则回退对象身份（进程内生命周期内稳定）。"""
        override = getattr(sprite, "collision_id", None)
        return str(override) if override else f"sprite-{id(sprite)}"

    @staticmethod
    def _is_dragging(sprite) -> bool:
        return bool(getattr(sprite, "dragging", False)) or \
            getattr(sprite, "interaction_state", INTERACTION_NORMAL) == INTERACTION_DRAG

    def _member_from_sprite(self, sprite) -> collision.MemberState:
        # V-2：碰撞体口径 = 稳定身体框（body_box×scale），与旧架构
        # 7ee8a34「鱼-鱼碰撞体改用身体框」一致；此前用整 canvas 矩形，
        # 含透明边，两鱼隔一个画布边距就弹开。无 body_rect 的鸭式
        # sprite（测试假对象/未声明 body_box 角色）回退整矩形。
        rect = sprite.rect()
        body = sprite.body_rect() if hasattr(sprite, "body_rect") else rect
        left, top = float(body.x()), float(body.y())
        w, h = float(body.width()), float(body.height())
        circles = collision.circles_from_rect(left, top, w, h)
        dragging = self._is_dragging(sprite)
        flags = collision.FLAG_VISIBLE | collision.FLAG_COLLISION_ENABLED
        if dragging:
            flags |= collision.FLAG_DRAGGING
        scale = float(getattr(sprite, "scale", 0.0) or collision.DEFAULT_BASE_SCALE)
        velocity = sprite.velocity
        return collision.MemberState(
            runtime_id=self._member_id(sprite),
            x=left + w / 2.0,
            y=top + h / 2.0,
            radius_x=w / 2.0,
            radius_y=h / 2.0,
            vx=float(velocity.x()),
            vy=float(velocity.y()),
            mass=collision.calculate_mass(
                w / 2.0, h / 2.0, scale=scale,
                collision_mass_scale=self.mass_scale),
            is_infinite_mass=dragging,  # 拖拽中 = 无限质量（FLAG_DRAGGING 语义）
            flags=flags,
            scale=scale,
            w=w,
            h=h,
            circles=circles,
        )

    def _static_member_state(self, member_id: str,
                             rect: Tuple[float, float, float, float]) -> collision.MemberState:
        left, top, w, h = rect
        return collision.MemberState(
            runtime_id=member_id,
            x=left + w / 2.0,
            y=top + h / 2.0,
            radius_x=w / 2.0,
            radius_y=h / 2.0,
            vx=0.0,
            vy=0.0,
            mass=collision.calculate_mass(
                w / 2.0, h / 2.0, collision_mass_scale=self.mass_scale),
            is_infinite_mass=True,
            flags=(collision.FLAG_VISIBLE | collision.FLAG_COLLISION_ENABLED
                   | collision.FLAG_STATIC),
            w=w,
            h=h,
            circles=(self._static_member_circles.get(member_id)
                     or collision.circles_from_rect(left, top, w, h)),
        )

    # ---------------------------------------------------------------- 内部：扫掠
    def _swept_collisions(self, members: Sequence[collision.MemberState]
                          ) -> Dict[str, tuple]:
        """帧间圆链扫掠（高速防穿透）。

        coordinator 用 seq 版本号跳过未变 pair 的扫掠重算；进程内 N 很小，
        每 tick 全量重算（语义等价，省掉版本簿记）。pair key 与
        solve_multi_body_collision 内部的排序口径一致（runtime_id 字典序）。
        """
        prev = self._prev_circles
        if not prev:
            return {}
        ordered = sorted(members, key=lambda m: m.runtime_id)
        swept: Dict[str, tuple] = {}
        for i, a in enumerate(ordered):
            if a.circles is None:
                continue
            prev_a = prev.get(a.runtime_id)
            if prev_a is None:
                continue
            for b in ordered[i + 1:]:
                if b.circles is None:
                    continue
                prev_b = prev.get(b.runtime_id)
                if prev_b is None:
                    continue
                hit = collision.swept_circle_chain_collision(
                    prev_a, a.circles, prev_b, b.circles)
                if hit[0]:
                    swept[f"{a.runtime_id}|{b.runtime_id}"] = hit
        return swept

    # ---------------------------------------------------------------- 内部：结果写回
    def _apply_results(self, results: Sequence[collision.ImpulseResult],
                       sprite_by_id: Dict[str, object]) -> None:
        # sprite 身份 -> [sprite, dvx 累加, dvy 累加]：多 pair 冲量向量合并后
        # 一次性写回并限速（对齐 coordinator 的 combined 语义 + 客户端单点限速）
        dv_acc: Dict[int, list] = {}
        fired: List[collision.ImpulseResult] = []

        for res in results:
            sprite_a = sprite_by_id.get(res.a)
            sprite_b = sprite_by_id.get(res.b)
            if sprite_a is None and sprite_b is None:
                continue  # 静态对静态（理论不产生，防御）
            # 纯位置分离去抖（对齐 coordinator 15 tick 去抖）：j==0 且 sep>0
            # 的 pair 在去抖窗口内整条跳过，防贴贴抖动
            if res.j == 0.0 and res.sep > 0.0:
                last = self._position_only_ticks.get(res.pair)
                if last is not None and self._tick - last < self.separation_debounce_ticks:
                    continue
                self._position_only_ticks[res.pair] = self._tick

            real_hit = False
            for sprite, other_id, dvx, dvy in (
                (sprite_a, res.b, res.dvx_a, res.dvy_a),
                (sprite_b, res.a, res.dvx_b, res.dvy_b),
            ):
                if sprite is None or self._is_dragging(sprite):
                    continue  # 拖拽中无限质量：求解器本就给 0，这里双保险
                hit_dv = math.hypot(dvx, dvy)
                # 撞静态成员放宽命中阈值（对齐 collision_client 的岛分支）
                floor = self.static_hit_min_dv \
                    if other_id in self._static_members else self.hit_min_dv
                is_real_hit = hit_dv >= floor
                already_thrown = getattr(
                    sprite, "interaction_state", INTERACTION_NORMAL) == INTERACTION_THROWN
                # dv 应用口径对齐客户端：真撞击才吸收；已 thrown 的放宽到
                # contact 下限（静置接触的微冲量不吸收，防自供能原地抖动）
                if is_real_hit or (already_thrown and hit_dv >= self.contact_dv_floor):
                    acc = dv_acc.setdefault(id(sprite), [sprite, 0.0, 0.0])
                    acc[1] += dvx
                    acc[2] += dvy
                if is_real_hit:
                    real_hit = True
                    if not already_thrown:
                        # 真撞击 → 抛掷态：抛掷物理控制器接管后续重力/反弹/
                        # 落地，落地后由它切回 "normal"
                        sprite.interaction_state = INTERACTION_THROWN

            # 位置分离：按 ImpulseResult 的 pair 级位移写回（与现架构客户端
            # 消费同一份 dx 的口径一致）；拖拽中位置由光标驱动，不动
            if sprite_a is not None and not self._is_dragging(sprite_a):
                self._translate(sprite_a, res.dx_a, res.dy_a)
            if sprite_b is not None and not self._is_dragging(sprite_b):
                self._translate(sprite_b, res.dx_b, res.dy_b)

            if real_hit:
                fired.append(res)

        # 速度写回 + 软限速（对齐 collision_client：超过 cap 才过软膝曲线）
        for sprite, dvx, dvy in dv_acc.values():
            velocity = sprite.velocity
            vx = float(velocity.x()) + dvx
            vy = float(velocity.y()) + dvy
            speed = math.hypot(vx, vy)
            if self.speed_cap > 0.0 and speed > self.speed_cap:
                clamped = physics.soft_clamp_speed(speed, self.speed_cap)
                vx *= clamped / speed
                vy *= clamped / speed
            sprite.set_velocity(type(velocity)(vx, vy))

        if self._listeners:
            for res in fired:
                event = CollisionEvent(
                    tick=res.tick, pair=res.pair, a=res.a, b=res.b, j=res.j,
                    nx=res.nx, ny=res.ny,
                    contact_x=res.contact_x, contact_y=res.contact_y)
                for listener in list(self._listeners):
                    listener(event)

    @staticmethod
    def _translate(sprite, dx: float, dy: float) -> None:
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return
        pos = sprite.pos
        sprite.set_pos(type(pos)(float(pos.x()) + dx, float(pos.y()) + dy))

    def _prune_position_only_ticks(self) -> None:
        """去抖表防漏：远超窗口的陈旧条目清掉（sprite 进出/churn 不积累）。"""
        if not self._position_only_ticks:
            return
        cutoff = self._tick - self.separation_debounce_ticks * 4
        stale = [p for p, t in self._position_only_ticks.items() if t < cutoff]
        for pair in stale:
            del self._position_only_ticks[pair]
