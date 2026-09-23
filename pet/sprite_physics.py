# -*- coding: utf-8 -*-
"""抛掷物理控制器（单合成窗架构 Phase 1b）："thrown" sprite 的子步积分。

直接复用 pet/physics.py 的纯函数（throw_step / is_at_rest /
soft_clamp_speed，见 .scratch/single-overlay-window/spec.md「直接复用」
表），语义对齐旧窗口路径 window.py 的 _tick_throw_physics（:4414-4461）：
≤8ms 子步积分 + 四边反弹 + 地面摩擦，落地静止（is_at_rest）后停住并把
interaction_state 切回 "normal"（行为控制器随后接管）。

位置积分归属：本控制器在 overlay 的 before_sprites_advance 阶段运行，
直接写 sprite 的 pos/velocity；PetSprite.advance 对 "thrown" 状态不再
积分（否则同一 tick 双重积分）。拖拽轨迹采样与松手抛掷判定在
PetSprite 侧（on_press/on_move/on_release），本模块只管飞行段。
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from PySide6.QtCore import QPointF, QRect, QRectF

from . import physics as physics_mod
from .pet_sprite import INTERACTION_NORMAL, INTERACTION_THROWN


class ThrowPhysicsController:
    """"thrown" sprite 的抛掷物理：重力 + 边界反弹 + 地面摩擦 + 静止收尾。

    bounds 为 overlay 局部坐标下的可活动区域（QRect/QRectF，由调用方
    传入；本模块不直接查 QScreen，保持 offscreen 可测）。sprite 外接
    矩形贴到 bounds 四边即反弹——语义同旧架构 _throw_bounds（身体框
    贴边，不是中心点贴边），因此每个 sprite 的有效边界按其矩形尺寸
    内缩。
    """

    def __init__(
        self,
        bounds: QRect | QRectF,
        *,
        max_sub_dt: float = 0.008,
        gravity: float = physics_mod.GRAVITY,
    ) -> None:
        self._bounds = QRectF(bounds)
        self._max_sub_dt = float(max_sub_dt)
        self._gravity = float(gravity)

    @property
    def bounds(self) -> QRectF:
        return QRectF(self._bounds)

    def set_bounds(self, bounds: QRect | QRectF) -> None:
        """更新可活动区域（屏幕/工作区变化时由集成层调用）。"""
        self._bounds = QRectF(bounds)

    def tick(self, sprites: Iterable, dt: float) -> None:
        """推进一个 tick：只处理 interaction_state == "thrown" 的 sprite。

        "normal"（行为控制器写 velocity，advance 积分）与 "drag"
        （光标驱动位置）的 sprite 原样跳过。
        """
        if dt <= 0.0:
            return
        for sprite in list(sprites):
            if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_THROWN:
                continue
            self._advance_thrown(sprite, float(dt))

    def _sprite_bounds(self, sprite) -> tuple[float, float, float, float]:
        """sprite 左上角的可活动范围：外接矩形贴 bounds 四边（内缩尺寸）。"""
        rect = sprite.rect()
        left = self._bounds.left()
        top = self._bounds.top()
        # sprite 比区域还大时 right/bottom 会小于 left/top：钳回，防死循环
        right = max(left, self._bounds.right() - rect.width())
        bottom = max(top, self._bounds.bottom() - rect.height())
        return left, top, right, bottom

    def _advance_thrown(self, sprite, dt: float) -> None:
        left, top, right, bottom = self._sprite_bounds(sprite)
        px, py = sprite.pos.x(), sprite.pos.y()
        vx, vy = sprite.velocity.x(), sprite.velocity.y()
        remaining = dt
        bounced_any = False
        speed = math.hypot(vx, vy)

        # 子步积分（≤8ms/步，对齐 window.py:4419-4435）：单步积分整个
        # dt 会把反弹延迟到 tick 末尾才结算（穿透后的位置被钳回、剩余
        # 飞行时间丢失）；子步保证反弹发生在正确时刻，剩余时间按反弹后
        # 速度继续积分。
        while remaining > 1e-6:
            step_dt = min(self._max_sub_dt, remaining)
            px, py, vx, vy, bounced = physics_mod.throw_step(
                px, py, vx, vy, step_dt, left, top, right, bottom,
                gravity=self._gravity)
            bounced_any = bounced_any or bounced
            remaining -= step_dt
            speed = math.hypot(vx, vy)
            if physics_mod.is_at_rest(py, vx, vy, bottom, bounced_any, speed):
                break

        sprite.set_pos(QPointF(px, py))
        if physics_mod.is_at_rest(py, vx, vy, bottom, bounced_any, speed):
            # 落地静止：velocity 归零停住（advance 只对 "normal" 积分，
            # 归零后原地不动），交还行为控制器
            sprite.set_velocity(QPointF(0, 0))
            sprite.interaction_state = INTERACTION_NORMAL
        else:
            sprite.set_velocity(QPointF(vx, vy))
