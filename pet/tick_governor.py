# -*- coding: utf-8 -*-
"""TickGovernor：overlay tick 的闲置降档决策（M-1，REVIEW_VERDICT 阻断项 V-1 的解）。

问题：tick 恒按屏幕刷新率（170Hz=6ms PreciseTimer）唤醒并全量跑行为/
碰撞/物理——成本恒定，与"有没有活干"无关。电池模式下恒定成本撞上
低时钟预算 → tick 溢出 → 感知为卡；且 267 次/s（叠加穿透轮询）的唤醒
切碎 CPU C-state 下潜窗口。旧架构有 idle_low_fps 等价物，本模块补齐。

设计要点（两端评审合并）：
- 四档：T0 ACTIVE（按刷新率，Precise）/ T1 IDLE_ANIM（42ms≈24fps，
  Coarse）/ T2 IDLE_STILL（250ms 心跳，Coarse）/ T3 OCCLUDED（1000ms）。
- 不变量：velocity≠0 或 interaction_state≠normal ⇒ 必 T0。
- 升档同步立即（不等 tick）；降档要求连续静默 DOWNGRADE_HOLD_MS（滞回
  防抖，碰撞余波/动画尾声不降）。
- 重绘已与 tick 解耦（V-4 帧直驱），降档不掉动画帧率；行为控制器用
  累加 dt，降档只损失掷骰粒度（≤250ms，对 30s 量级阈值无感）。

零 Qt：时钟可注入，offscreen 单测直接驱动。
"""
from __future__ import annotations

import time

TIER_ACTIVE = 0
TIER_IDLE_ANIM = 1
TIER_IDLE_STILL = 2
TIER_OCCLUDED = 3

#: 各档 tick 间隔（T0 除外——按屏幕刷新率动态计算）
TIER_INTERVAL_MS = {
    TIER_IDLE_ANIM: 42,
    TIER_IDLE_STILL: 250,
    TIER_OCCLUDED: 1000,
}

#: 运动/输入后保持 T0 的尾巴（碰撞余波、松手后的静止判定窗口）
ACTIVE_HOLD_MS = 400.0
#: 降档滞回：条件需连续成立这么久而非单次满足（防动画/碰撞尾声抖动）
DOWNGRADE_HOLD_MS = 800.0
#: "动画在播"判定：最近一次帧到达距今不超过它（24fps → 帧间隔 42ms，
#: 放宽到覆盖解码抖动与切 clip 空窗）
ANIMATING_WINDOW_S = 0.5


class TickGovernor:
    """tick 档位状态机：输入活动信号，输出目标档位（overlay 应用到 QTimer）。"""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._tier = TIER_ACTIVE
        self._last_kinetic: float | None = None
        self._quiet_since: float | None = None

    @property
    def tier(self) -> int:
        return self._tier

    def notify_kinetic(self) -> None:
        """记录一次运动/输入（set_pos 位移、velocity 写入、鼠标事件）。"""
        self._last_kinetic = self._clock()

    def evaluate(self, *, any_motion: bool, animating: bool, visible: bool) -> int:
        """评估并返回当前档位（升档立即、降档滞回）。

        any_motion: 任一 sprite velocity≠0 或 interaction_state≠normal；
        animating:  最近 ANIMATING_WINDOW_S 内有动画帧到达；
        visible:    overlay 可见。
        """
        now = self._clock()
        recent_kinetic = (
            self._last_kinetic is not None
            and (now - self._last_kinetic) * 1000.0 < ACTIVE_HOLD_MS
        )
        if any_motion or recent_kinetic:
            desired = TIER_ACTIVE
        elif not visible:
            desired = TIER_OCCLUDED
        elif animating:
            desired = TIER_IDLE_ANIM
        else:
            desired = TIER_IDLE_STILL

        if desired <= self._tier:
            # 升档（或平档）：立即；静默计时作废
            self._tier = desired
            self._quiet_since = None
        else:
            # 降档（含遮挡）：要求条件连续成立 DOWNGRADE_HOLD_MS——遮挡
            # 不特殊化立即降档：可见性瞬态抖动/未 show 即驱动 tick 的
            # 嵌入方（含 offscreen 测试）不能被跳过仿真
            if self._quiet_since is None:
                self._quiet_since = now
            elif (now - self._quiet_since) * 1000.0 >= DOWNGRADE_HOLD_MS:
                self._tier = desired
                self._quiet_since = None
        return self._tier
