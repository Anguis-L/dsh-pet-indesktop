# -*- coding: utf-8 -*-
"""行为状态机（单合成窗架构 Phase 1b）：游荡/走路/待机/转向/点击反应。

架构背景见 .scratch/single-overlay-window/spec.md。本控制器在 overlay 的
before_sprites_advance(dt) 阶段运行（位置积分仍由 sprite.advance 统一完成）：

    class PetOverlay(OverlayWindow):
        def before_sprites_advance(self, dt):
            self.behavior.tick(self.sprites, dt)

只在 sprite.interaction_state == "normal" 时驱动掷骰链（写 velocity/facing/
bind_clip）；drag/thrown 期间位置归鼠标路由与物理控制器，本控制器只接管
画面——拖拽绑 drag 悬空动画（F1，角色包无 drag 素材回退 idle 池），抛掷
绑定飞行动画（F4）；落地静止切回 "normal" 后本控制器从当前姿态继续。

与旧窗口路径（pet/window.py，仅语义参考、禁止 import）的对应关系：
- 掷骰节奏沿用 catalog 概率：30% 待机 / 10% 转向 / 20% 移动；40% 随机
  动作池（acts）：40% 概率桶已移植（池空回退待机，语义同 window.py
  _pick_next 的 acts 分支）；
- 转向闸门沿用 inward_facing 中线滞回：掷中转向但无需纠正（带内或已朝
  内）降级待机；掷中待机但朝外且有转向素材时改播转向
  （window.py:2735-2745 语义）；
- 需要反向的移动先播 turn clip 再翻 facing（window.py:2631-2633 语义），
  翻向后立即执行已排定的移动计划（pending_move）；
- 走路目标先于朝向、步幅整圈量化（movement.quantize_move），
  velocity = 位移/时长 → 平均速度恒等于动画步态速度（防脚滑）。有
  「圈内逐帧位移曲线」（move_strides.json 的 curve）的素材改由曲线驱动
  每 tick 目标位置（F3）：静帧段不位移、动帧段推进；
- 拖拽悬空动画（F1）：按下命中即绑 drag（无素材回退 idle 池），松手切回
  待机池（window.py:3175-3176 / 3260-3264 语义）；
- 点击反应 = 打断当前行为播 click 池随机 clip，播完回待机
  （window.py:3438 _on_click 语义；音效/黄金回旋等外围效果不在本层）。

完成判定用墙钟（tick 累加 dt ≥ clip 时长），不连 clip.finished 做状态
推进：clip 是 library 缓存的共享资源，bind_clip 已负责启停；墙钟与解码
进度的漂移上界是一个 clip 时长内的解码节流误差，到点统一 snap 收口，
误差 ≤ 一个 tick 的位移（亚像素）。但圈末 re-arm 必须接 finished（F2）：
WebMClip 圈末交付结束标记后停表，只有 start() 能续圈——多圈移动/长拖拽
的第二个圈起会冻结，故 PetSprite 转发 finished、控制器在「本状态仍有
剩余时长」时调 restart_clip()（等价 window.py:1802-1821）。
"""

from __future__ import annotations

import random
from weakref import WeakKeyDictionary

from PySide6.QtCore import QPointF, QRect

from . import catalog, movement
from .pet_sprite import INTERACTION_DRAG, INTERACTION_NORMAL, INTERACTION_THROWN
from .predictive_prewarm import PredictivePrewarm

STATE_IDLE = "idle"
STATE_MOVE = "move"
STATE_TURN = "turn"
STATE_CLICK = "click"
STATE_ACTS = "acts"
#: 拖拽接管态（F1）：画面 = drag 悬空动画，位置归鼠标；不属于掷骰链
STATE_DRAG = "drag"
#: 抛掷飞行接管态（F4）：画面 = drag（悬空）动画循环 + 物理按速度加速播放；
#: 位置归 sprite_physics；落地回 normal 后由 tick 收口回待机
STATE_THROWN = "thrown"

#: 本控制器「画面归它、位置不归它」的状态（F1/F4）：sprite 已回 normal
#: 却停在这些态 = 收尾接线缺失（看门狗/shell 分支没走到），必须自愈回待机
_CAPTURED_STATES = (STATE_DRAG, STATE_THROWN)

#: 圈末 finished 需要原地续播（re-arm）的状态（F2/F4）：这些状态的时长可以
#: 跨多圈（多圈移动 / 循环拖拽 / 飞行悬空循环），中间圈结束必须重播，
#: 否则第 2 圈起冻结
_REARM_STATES = (STATE_MOVE, STATE_DRAG, STATE_ACTS, STATE_THROWN)


class _SpriteState:
    """单个 sprite 的行为状态（控制器私有，不落在 sprite 上）。"""

    __slots__ = ("state", "anim", "elapsed", "duration", "move_target",
                 "pending_move", "predictor", "curve", "frames_per_loop",
                 "loops", "loop_duration", "move_start", "suspended")

    def __init__(self) -> None:
        self.state = STATE_IDLE
        self.anim: str | None = None      # 当前绑定的 clip 名（None = 尚未起播）
        self.elapsed = 0.0                # 当前状态已流逝（tick 累加的 dt）
        self.duration = 0.0               # 当前 clip 时长（秒）
        self.move_target: QPointF | None = None
        self.pending_move: dict | None = None  # 反向前先转向的移动计划
        self.predictor = None                  # tick 创建状态时挂 PredictivePrewarm
        # 移动计划（F3）：圈内逐帧位移曲线的位置解算输入。无 curve 的角色
        # 保持线性（curve=None，velocity 在 _start_move 一次算好）
        self.curve: list | None = None    # 圈内累计进度曲线（curve[i] = 源帧 i）
        self.frames_per_loop = 0          # 每圈源帧数（曲线相位折算用）
        self.loops = 1                    # 计划整圈数
        self.loop_duration = 0.0          # 单圈墙钟时长（秒）
        self.move_start: QPointF | None = None  # 计划起点（曲线绝对位置锚点）
        # 被接管标记（F5）：tick 见过非 normal 即置位；回到 normal 的那一
        # tick 据此判断「接管前的移动/转向计划必须撤销，绝不 snap」
        self.suspended = False


class BehaviorController:
    """一组 sprite 的行为驱动：tick 推进状态机，on_sprite_clicked 接点击路由。

    bounds：活动边界（overlay 局部坐标的 QRect，语义 = 旧架构的
    availableGeometry）。本模块不查 QScreen，边界由集成层传入，保持
    offscreen 可测。rng：可注入随机源（需有 random/randint/choice，
    默认 random 模块），测试注入确定性实现。
    """

    def __init__(
        self,
        bounds: QRect,
        *,
        rng=None,
        margin: int = catalog.MOVE_MARGIN,
        min_distance: int = catalog.MOVE_MIN_PX,
        max_distance: int = catalog.MOVE_MAX_PX,
    ) -> None:
        self.bounds = QRect(bounds)
        self.rng = rng if rng is not None else random
        self.margin = margin
        self.min_distance = min_distance
        self.max_distance = max_distance
        # 不移动开关（菜单/config 写入口）：移动桶并入动作池（window.py
        # _pick_next 的 no_move 语义）
        self.no_move = False
        # 预测式预热（批10-A1 语义移植）：每 sprite 一个 PredictivePrewarm
        # （预测是按 sprite 的素材池掷的，控制器级共享会跨池串名）。
        # 提前量沿用旧默认 350ms；消费规则单源在 predictive_prewarm.consume
        self._predict_lead_s = 0.35
        # 预测预热总开关（config predict_prewarm_lead_ms>0 映射；测试可关）
        self.predict_enabled = True
        self._states: dict = {}
        # V-9：按库对象弱引用缓存——旧实现以 id(lib) 为键，库销毁后地址
        # 被新库复用会命中陈旧分类池（换角色/多宠生灭时拿到错素材名），
        # 且条目只增不减；弱键字典在库销毁时自动回收
        self._cats_cache: WeakKeyDictionary = WeakKeyDictionary()

    # ---------------------------------------------------------------- 对外 API
    def tick(self, sprites, dt: float) -> None:
        """推进一轮：normal 走掷骰链，drag/thrown 只做画面接管。

        非 normal 的 sprite 不推进状态机（位置归鼠标/物理），但画面必须
        绑对动画（F1 拖拽悬空 / F4 飞行循环），故仍进入本循环。
        """
        for sprite in sprites:
            self._ensure_hooks(sprite)
            st = self._states.get(sprite)
            if st is None:
                st = self._states[sprite] = _SpriteState()
                st.predictor = self._make_predictor(sprite)
            if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_NORMAL:
                st.suspended = True
                self._tick_captured(sprite, st)
                continue
            if st.suspended:
                # 接管结束后的收口（F5，window.py:4174-4177 _enter_physics_mode
                # →_cancel_move 的 sprite 版）：接管期间位置已被鼠标/物理改写，
                # 接管前的移动/转向计划一律作废——绝不 set_pos(旧目标) snap，
                # 否则松手后一到 duration 就瞬移回原路线终点
                st.suspended = False
                if st.state in (STATE_MOVE, STATE_TURN):
                    self._enter_idle(sprite, st, self._categories(sprite.library))
            if st.state in _CAPTURED_STATES:
                # 接管已结束却停在接管态（看门狗收尾/shell 分支没走到）：
                # 自愈回待机链，否则悬空动画无限循环
                self._enter_idle(sprite, st, self._categories(sprite.library))
            self._tick_sprite(sprite, st, dt)

    def on_drag_started(self, sprite) -> None:
        """鼠标按下命中 sprite（overlay 拖拽接线入口）：播 drag 悬空动画。

        进 STATE_DRAG 并绑 drag clip；角色包未提供 drag 素材时回退 idle 池
        （旧机 ``if self.drag:`` 同款回退，window.py:3175-3176）。拖拽期间
        位置由鼠标驱动，本方法绝不改写 velocity/pos。
        """
        self._ensure_hooks(sprite)
        st = self._states.get(sprite)
        if st is None:
            st = self._states[sprite] = _SpriteState()
            st.predictor = self._make_predictor(sprite)
        self._enter_drag(sprite, st)

    def on_drag_released(self, sprite) -> None:
        """真拖拽松手（非点击候选）：切回待机池。

        松手被判为甩出（interaction_state == "thrown"）时不得收尾——飞行段
        归 sprite_physics，落地回 normal 后由 tick 自愈回待机
        （window.py:3260-3264 只处理原地放下分支，同语义）。
        """
        if getattr(sprite, "interaction_state", INTERACTION_NORMAL) == INTERACTION_THROWN:
            return
        st = self._states.get(sprite)
        if st is None:
            return
        self._enter_idle(sprite, st, self._categories(sprite.library))

    def on_sprite_clicked(self, sprite) -> bool:
        """点击反应（overlay 鼠标路由接线入口）：播 click 池随机 clip。

        可打断当前任何行为（含进行中的移动/另一次点击）；播完回待机。
        非 normal 状态或无 click 素材返回 False（调用方可据此放行穿透）。
        """
        if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_NORMAL:
            return False
        cats = self._categories(sprite.library)
        if not cats["clicks"]:
            return False
        st = self._states.get(sprite)
        if st is None:
            st = self._states[sprite] = _SpriteState()
        name = self._pick(cats["clicks"], exclude=st.anim)
        st.state = STATE_CLICK
        st.anim = name
        st.elapsed = 0.0
        st.duration = self._clip_duration(sprite.library, name)
        st.pending_move = None
        self._clear_move_plan(st)
        sprite.set_velocity(QPointF(0, 0))
        self._bind_with_gen(sprite, st, name)
        return True

    def play_once(self, sprite, name: str) -> bool:
        """一次性播放指定动画，播完回掷骰链（菜单「播放动画」入口，
        window.py switch_clip 语义）。"""
        if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_NORMAL:
            return False
        st = self._states.setdefault(sprite, _SpriteState())
        st.state = STATE_ACTS
        st.pending_move = None
        self._clear_move_plan(st)
        sprite.set_velocity(QPointF(0, 0))
        st.elapsed = 0.0
        st.anim = name
        st.duration = self._clip_duration(sprite.library, name)
        sprite.bind_clip(name)
        return True

    def play_move_once(self, sprite, name: str) -> bool:
        """以指定移动素材触发一次移动（菜单「移动」类入口；无可达空间
        回退动作池，window.py trigger_move 语义）。"""
        if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_NORMAL:
            return False
        cats = self._categories(sprite.library)
        if name not in cats["moves"]:
            return False
        st = self._states.setdefault(sprite, _SpriteState())
        if not self._plan_move(sprite, st, cats, anim_override=name):
            self._enter_acts(sprite, st, cats)
            return False
        return True

    def state_of(self, sprite) -> str | None:
        """当前行为状态（idle/move/turn/click/acts/drag）；未接管过返回 None。"""
        st = self._states.get(sprite)
        return st.state if st is not None else None

    def anim_of(self, sprite) -> str | None:
        """当前绑定的 clip 名（None = 尚未接管/未起播）。

        只读访问器：点击台词绑定（``config.click_talk_texts_for``）要按"这次
        点中了哪条 click 动画"查表，而 ``on_sprite_clicked`` 的返回值是
        「是否消费点击」契约，不能挪用；故单开这一面，不改变点击语义。
        """
        st = self._states.get(sprite)
        return st.anim if st is not None else None

    def on_clip_finished(self, sprite) -> None:
        """clip 圈末结束（PetSprite.finished 转发）：本状态还有剩余时长就续圈。

        WebMClip 圈末交付结束标记后停表，只有 start() 能 re-arm
        （webm_clip.py:1521-1567），不续则多圈移动的第 2 圈起、长拖拽过圈末
        全部冻结在末帧。等价旧 window.py:1802-1821 _restart_current_clip。

        续圈判据用剩余时长（墙钟口径，见模块头）：末圈结束（剩余 ≤ 0）绝不
        续——收口交给 tick 的到点 snap，否则已完成的移动会被重新起播一整圈。
        """
        st = self._states.get(sprite)
        if st is None or st.state not in _REARM_STATES:
            return
        if st.duration - st.elapsed <= 0.0:
            return
        sprite.restart_clip()

    def forget(self, sprite) -> None:
        """sprite 从 overlay 移除时清理其状态（可选，防状态表只增不减）。"""
        self._states.pop(sprite, None)

    def _ensure_hooks(self, sprite) -> None:
        """把 clip 圈末回调挂到 sprite（F2），每个 sprite 只挂一次。

        挂接方必须是本控制器：只有它知道当前状态的剩余时长（多圈移动的
        中间圈续、末圈不续），sprite 侧只做转发（PetSprite._on_clip_finished）。
        """
        if getattr(sprite, "_clip_finished_owner", None) is self:
            return
        sprite._clip_finished_cb = self.on_clip_finished
        sprite._clip_finished_owner = self

    # ---------------------------------------------------------------- 接管态画面
    def _tick_captured(self, sprite, st: _SpriteState) -> None:
        """非 normal（drag/thrown）阶段的画面接管（F1/F4）。

        位置由鼠标路由与物理控制器负责，本控制器只保证「当前该播什么」：
        拖拽 = 悬空 clip（window.py:3175-3176 进拖拽切 drag），抛掷 = 飞行
        循环 clip（window.py:2487-2499）。绝不推进掷骰链、绝不改写
        velocity——旧断言「非 normal 从不被接管」在此扩为「只接受 drag
        绑定，不接受掷骰驱动」。
        """
        istate = getattr(sprite, "interaction_state", INTERACTION_NORMAL)
        if istate == INTERACTION_DRAG:
            if st.state != STATE_DRAG:
                self._enter_drag(sprite, st)
        elif istate == INTERACTION_THROWN and st.state != STATE_THROWN:
            self._enter_thrown(sprite, st)

    # ---------------------------------------------------------------- 状态机
    def _tick_sprite(self, sprite, st: _SpriteState, dt: float) -> None:
        # 边缘探头会话（sprite.probe_active = 曝光<1）的 sprite：它自己的钳制
        # 域已被放宽（身体按曝光比例藏出屏幕缘），这里不能再按常规 bounds 钳，
        # 否则探头姿态每 tick 被拉回屏内。游荡同样要拦（见 _plan_move）：位置
        # 归探头控制器唯一所有。
        probing = bool(getattr(sprite, "probe_active", False))
        if probing and st.state == STATE_MOVE:
            # 进场时若恰在移动态（速度刚好低于静止阈值）：先收尾回待机，否则
            # 到点 snap 会把探头姿态一脚踹回走路目标点
            self._enter_idle(sprite, st, self._categories(sprite.library))
        if not probing:
            self._clamp_into_bounds(sprite)
        st.elapsed += dt
        if st.state == STATE_IDLE:
            if st.anim is None:
                self._enter_idle(sprite, st, self._categories(sprite.library))
            elif st.elapsed >= st.duration:
                self._roll_next(sprite, st)
            else:
                self._maybe_predict(sprite, st)
        elif st.state == STATE_MOVE:
            self._apply_move_curve(sprite, st, dt)
            if st.elapsed >= st.duration:
                if st.move_target is not None:
                    sprite.set_pos(st.move_target)  # 到点 snap，消除积分残差
                sprite.set_velocity(QPointF(0, 0))
                self._enter_idle(sprite, st, self._categories(sprite.library))
        elif st.state == STATE_TURN:
            if st.elapsed >= st.duration:
                if probing:
                    # 探头会话只允许待机/转向且冻结朝向（F7，
                    # window_optional_services.py:223-233/349-350）：turn 播完
                    # 不翻 facing；排定的移动计划一并作废（会话期间位置归
                    # 探头控制器，绝不起步——起步会同时把朝向翻过去）
                    st.pending_move = None
                    self._enter_idle(sprite, st, self._categories(sprite.library))
                else:
                    # 转向播完才翻朝向（window.py:2631-2633）：turn clip 播完
                    # 即画面已转向，此刻翻 facing 无跳变。
                    sprite.facing = "right" if sprite.facing == "left" else "left"
                    pending = st.pending_move
                    st.pending_move = None
                    if pending is None:
                        self._enter_idle(sprite, st, self._categories(sprite.library))
                    elif not self._start_move(sprite, st, pending):
                        # 移动素材开播被拒（F6）：绝不能留在 turn 态——下一个
                        # 到点分支会把朝向再翻一次。直接回收待机
                        self._enter_idle(sprite, st, self._categories(sprite.library))
        elif st.state == STATE_ACTS:
            if st.elapsed >= st.duration:
                self._roll_next(sprite, st)
            else:
                self._maybe_predict(sprite, st)
        elif st.state == STATE_CLICK:
            if st.elapsed >= st.duration:
                self._enter_idle(sprite, st, self._categories(sprite.library))

    # ---------------------------------------------------------------- 预测式预热
    def _make_predictor(self, sprite) -> PredictivePrewarm:
        """每 sprite 一个 PredictivePrewarm（roll/warm 闭包绑定该 sprite 的库）。

        should_predict 恒 True：帧序列时代预热 ~2.5ms 一帧，webm 路径的
        warm_first_frame 本就是旧架构的预热入口；预测掷骰只掷一次、产物
        照存（盲审 P1-1：否则稳态分布漂离 30/10/40/20）。
        """
        def _roll(exclude):
            cats = self._categories(sprite.library)
            pools = {k: cats[k] for k in ("idles", "turns", "moves", "acts", "clicks")}
            from .predictive_prewarm import roll_next as _pp_roll_next
            return _pp_roll_next(pools, exclude, rng=self.rng)

        def _warm(name):
            try:
                clip = sprite.library.movie(name)
                warm = getattr(clip, "warm_first_frame", None)
                if callable(warm):
                    warm()
            except Exception:
                pass  # 预热失败静默（播放时按需同步解码，语义同旧路径）

        return PredictivePrewarm(roll=_roll, warm=_warm,
                                 should_predict=lambda name: True)

    def _maybe_predict(self, sprite, st: _SpriteState) -> None:
        """墙钟适配的预测触发（语义 = PredictivePrewarm.on_frame 的
        wall_remaining ≤ lead）：行为控制器以 elapsed/duration 墙钟推进，
        等效换算进 on_frame 的帧口径（frames=1000/fps=1000/divisor=1 →
        n = 999 - remaining*1000），不复制其触发逻辑。"""
        if st.predictor is None or not self.predict_enabled or st.duration <= 0:
            return
        remaining = st.duration - st.elapsed
        if remaining > self._predict_lead_s or remaining < 0:
            return
        n = max(0, 999 - int(round(remaining * 1000)))
        st.predictor.on_frame(
            st.anim, n, 1000, 1000.0, 1, self._predict_lead_s,
            exclude=st.anim)

    def _bind_with_gen(self, sprite, st: _SpriteState, name: str) -> bool:
        """bind + 预测代次推进（begin_anim 每次切换自增；作废由 consume 的
        context/gen 校验完成，不手动清预测——GLM A4 单规则）。

        返回 ``sprite.bind_clip`` 是否被接受（F6）：起播被拒时不推进预测
        代次，调用方据此放弃依赖该动画的状态（移动计划等）。
        """
        ok = sprite.bind_clip(name)
        if ok and st.predictor is not None:
            st.predictor.begin_anim(name)
        return bool(ok)

    def _roll_next(self, sprite, st: _SpriteState) -> None:
        """待机播完掷骰：30% 待机 / 10% 转向 / 40% 待机（acts 桶让位）/ 20% 移动。

        探头会话期间移动桶必然落空（_plan_move 闸门），沿既有回退链进动作池/
        待机——「只允许待机/转向」的位移语义由此保证；动作池 clip 只播原地动画，
        不改位置。
        """
        cats = self._categories(sprite.library)
        # 批10-A1：先消费预测（context/gen 校验单规则，不符即弃 → 现场掷骰）
        if st.predictor is not None and self.predict_enabled:
            predicted = st.predictor.consume(
                context_anim=st.anim, exclude=st.anim,
                gap_active=False, moves=set(cats["moves"]))
            if predicted is not None:
                self._play_predicted(sprite, st, cats, predicted)
                return
        roll = self.rng.random()
        if roll < catalog.P_TURN:  # P_IDLE 与 P_TURN 是累计阈值（<0.3 待机，<0.4 转向）
            action = STATE_IDLE if roll < catalog.P_IDLE else STATE_TURN
        elif roll < catalog.P_ACTS:
            action = STATE_ACTS  # 40% 随机动作池（acts 为空时 enter 内回退待机）
        else:
            action = STATE_MOVE
        if action == STATE_MOVE:
            if self.no_move or not self._plan_move(sprite, st, cats):
                # 不移动/移动失败回退动作池（window.py:2735 语义，acts 空回待机）
                self._enter_acts(sprite, st, cats)
            return
        # 朝向闸门（window.py:2735-2745）：需要纠正朝向时一律播转向；
        # 掷中转向但无需纠正 → 降级待机。朝向绝不由随机数凭空翻转。
        off_x, _off_y, bw, _bh = self._body_geometry(sprite)
        cx, left, right = movement.body_reach(
            self.bounds.left(), self.bounds.right(), sprite.pos.x() + off_x, bw, self.margin)
        want = movement.inward_facing(cx, left, right)
        if action == STATE_ACTS:
            self._enter_acts(sprite, st, cats)
        elif want is not None and want != sprite.facing and cats["turns"]:
            self._enter_turn(sprite, st, cats)
        else:
            self._enter_idle(sprite, st, cats)

    # ---------------------------------------------------------------- 状态进入
    @staticmethod
    def _clear_move_plan(st: _SpriteState) -> None:
        """清移动计划残留（含 curve 通道）。

        任何非移动态都必须调它：残留的 move_start/curve 会让「回到移动态
        之前」的路径读到上一段计划的曲线相位（算出错位置），F5 的「接管后
        绝不 snap」也依赖 move_target 已被清掉。
        """
        st.move_target = None
        st.move_start = None
        st.curve = None
        st.frames_per_loop = 0
        st.loops = 1
        st.loop_duration = 0.0

    def _enter_idle(self, sprite, st: _SpriteState, cats: dict,
                    forced_name: str | None = None) -> None:
        st.state = STATE_IDLE
        st.pending_move = None
        self._clear_move_plan(st)
        sprite.set_velocity(QPointF(0, 0))
        name = forced_name if forced_name is not None else self._pick(cats["idles"], exclude=st.anim)
        st.elapsed = 0.0
        st.anim = name
        st.duration = self._clip_duration(sprite.library, name) if name else 0.0
        if name is not None:
            self._bind_with_gen(sprite, st, name)

    def _play_predicted(self, sprite, st: _SpriteState, cats: dict, name: str) -> None:
        """执行预测产物（window.py _play_roll 语义）：move 名走移动计划
        （失败回退动作池）；其余按归属池进入对应状态。"""
        if name in cats["moves"]:
            if self.no_move or not self._plan_move(sprite, st, cats, anim_override=name):
                self._enter_acts(sprite, st, cats)
            return
        if name in cats["turns"] and cats["turns"]:
            self._enter_turn(sprite, st, cats, forced_name=name)
        elif name in cats["acts"]:
            self._enter_acts(sprite, st, cats, forced_name=name)
        else:
            self._enter_idle(sprite, st, cats, forced_name=name)

    def _enter_drag(self, sprite, st: _SpriteState) -> None:
        """进入拖拽接管态（F1）：绑 drag 悬空 clip（缺素材回退 idle 池）。

        velocity 不动（拖拽位置归鼠标；``on_press`` 已归零）。探针/掷骰链
        的一切排定计划在此作废。
        """
        cats = self._categories(sprite.library)
        name = cats["drag"][0] if cats["drag"] else self._pick(cats["idles"])
        st.state = STATE_DRAG
        st.pending_move = None
        self._clear_move_plan(st)
        st.elapsed = 0.0
        st.anim = name
        st.duration = self._clip_duration(sprite.library, name) if name else 0.0
        if name is not None:
            self._bind_with_gen(sprite, st, name)

    def _enter_thrown(self, sprite, st: _SpriteState) -> None:
        """进入抛掷飞行接管态（F4）：绑 drag（缺素材回退 idle 池）并循环。

        旧实现 window.py:2487-2499：飞行途中当前动作播完固定切「悬空」动画
        并循环（首帧必热的 drag clip，视觉契合被击飞），落地停稳才切回待机
        ——收口由 tick 的接管态自愈完成，本方法不碰速度/位置。播放速率由
        sprite_physics 每 tick 按速度叠加（physics.flight_anim_speed，
        window.py:4328-4340）。
        """
        cats = self._categories(sprite.library)
        name = cats["drag"][0] if cats["drag"] else self._pick(cats["idles"])
        st.state = STATE_THROWN
        st.pending_move = None
        self._clear_move_plan(st)
        st.elapsed = 0.0
        st.anim = name
        st.duration = self._clip_duration(sprite.library, name) if name else 0.0
        if name is not None:
            # 起播被拒也静默：飞行段的位置积分不能因动画失败而中断
            self._bind_with_gen(sprite, st, name)

    def _enter_acts(self, sprite, st: _SpriteState, cats: dict,
                    forced_name: str | None = None) -> None:
        """随机动作（40% acts 桶）：acts 池随机一段，播完回掷骰
        （window.py _pick_next 的 acts 分支语义）；池空回退待机。

        探头会话（F7）把动作桶整体降级待机：会话期间只允许待机/转向
        （window_optional_services.py:223-233 _effects_filter_switch 的 sprite
        版）。闸门收在这一处而不是只收在掷骰分支——预测产物、移动失败回退
        同样经此进入动作池，三处口径必须一致。
        """
        if getattr(sprite, "probe_active", False):
            self._enter_idle(sprite, st, cats)
            return
        name = forced_name if forced_name is not None else self._pick(cats["acts"], exclude=st.anim)
        if name is None:
            self._enter_idle(sprite, st, cats)
            return
        st.state = STATE_ACTS
        st.pending_move = None
        self._clear_move_plan(st)
        sprite.set_velocity(QPointF(0, 0))
        st.elapsed = 0.0
        st.anim = name
        st.duration = self._clip_duration(sprite.library, name)
        self._bind_with_gen(sprite, st, name)

    def _enter_turn(self, sprite, st: _SpriteState, cats: dict,
                    pending_move: dict | None = None,
                    forced_name: str | None = None) -> None:
        name = forced_name if forced_name is not None else self._pick(cats["turns"], exclude=st.anim)
        if name is None:  # 调用方已保证 turns 非空；防御性回退
            self._enter_idle(sprite, st, cats)
            return
        st.state = STATE_TURN
        st.anim = name
        st.elapsed = 0.0
        st.duration = self._clip_duration(sprite.library, name)
        st.pending_move = pending_move
        self._clear_move_plan(st)
        sprite.set_velocity(QPointF(0, 0))
        self._bind_with_gen(sprite, st, name)

    def _plan_move(self, sprite, st: _SpriteState, cats: dict,
                   anim_override: str | None = None) -> bool:
        """排定一次移动（window.py _try_move 语义）；返回 False = 未建立计划。
        anim_override：菜单「移动」类指定素材（trigger_move），None = 掷骰随机。

        探头会话（sprite.probe_active）一律拒绝：旧机在 PetWindow._try_move
        入口用 _effects_probe_active 整体拦住位移（防止挂着探头姿态被平移出
        屏幕边缘），sprite 世界把闸门收在这一处——掷骰、预测产物、菜单移动
        三条路径都经此，调用方按既有回退链进动作池/待机。

        计划带上「圈内逐帧位移曲线」的全部解算输入（curve / frames_per_loop
        / loops / loop_duration，F3）：旧架构 window.py:2753-2768 的计划键
        同款，位置由曲线（而非平均速度）决定，静帧段不位移。
        """
        if getattr(sprite, "probe_active", False):
            return False
        moves = cats["moves"]
        if not moves:
            return False
        off_x, off_y, bw, bh = self._body_geometry(sprite)
        cx, left, right = movement.body_reach(
            self.bounds.left(), self.bounds.right(), sprite.pos.x() + off_x, bw, self.margin)
        dir_sign = movement.choose_move_direction(cx, left, right, self.min_distance, self.rng)
        if dir_sign is None:
            return False  # 两侧空间都不足：不建立计划
        room = (cx - left) if dir_sign < 0 else (right - cx)
        far = min(self.max_distance, int(room))
        if far < self.min_distance:
            return False
        distance = self.rng.randint(self.min_distance, far)
        name = anim_override if anim_override is not None else self._pick(moves)
        if name is None:
            return False
        lib = sprite.library
        stride = ((getattr(lib, "move_strides", None) or {}).get(name, catalog.MOVE_STRIDE_DEFAULT_PX)) * float(getattr(sprite, "scale", 1.0))
        loop_duration = self._clip_duration(lib, name)
        if loop_duration <= 0:
            return False
        # 步幅整圈量化：位移锁到步态整圈，velocity=位移/时长 ⇒ 平均速度
        # 恒等于动画步态速度（防脚滑）；越界时 quantize 内部递减圈数/夹到 room。
        loops, distance, duration = movement.quantize_move(distance, stride, room, loop_duration)
        target_cx = cx + dir_sign * distance
        target_x = target_cx - bw / 2 - off_x
        target_y = movement.wander_target_y(
            sprite.pos.y() + off_y, self.bounds.top(), self.bounds.bottom(),
            bh, self.margin, self.rng) - off_y
        curve = (getattr(lib, "move_curves", None) or {}).get(name)
        plan = {
            "anim": name,
            "target": QPointF(target_x, target_y),
            "duration": duration,
            "facing": "right" if dir_sign > 0 else "left",
            # 圈内逐帧位移曲线（动帧才动、静帧不动）；无曲线 → None → 线性
            "curve": curve,
            "frames_per_loop": self._frames_per_loop(lib, name, curve),
            "loops": loops,
            "loop_duration": loop_duration,
        }
        if plan["facing"] != sprite.facing and cats["turns"]:
            # 需要反向：先播 turn clip，翻朝向后由 turn 完成分支执行本计划
            self._enter_turn(sprite, st, cats, pending_move=plan)
            return True
        return self._start_move(sprite, st, plan)

    @staticmethod
    def _frames_per_loop(lib, name: str, curve) -> int:
        """每圈源帧数：优先库的权威帧数，缺该接口时回退曲线长度。

        曲线本就是「逐源帧」等长的（curve[i] = 源帧 i 的圈内进度），长度即
        每圈帧数；仅当库未暴露 frames() 时才用（测试假库/轻量替身）。
        """
        frames_fn = getattr(lib, "frames", None)
        if callable(frames_fn):
            try:
                count = int(frames_fn(name) or 0)
            except (TypeError, ValueError, KeyError):
                count = 0
            if count > 0:
                return count
        return len(curve) if curve else 0

    def _start_move(self, sprite, st: _SpriteState, plan: dict) -> bool:
        """启动移动计划；返回 False = 开播被拒、计划未建立（F6）。

        无 curve：velocity = 位移/时长 一次算好的匀速直线（旧语义不变）。
        有 curve（F3）：本 tick 先不动，下一 tick 起由 _apply_move_curve 按
        曲线**绝对目标位置**反算 velocity——起步若先按平均速度积分一帧，
        静帧段会被提前推开一帧位移，而曲线位置只增不减、永不回退。
        """
        # 开播确认前不提交任何计划字段：被拒时调用方按既有回退链进动作池/
        # 待机，绝不会出现「动画没播却在按它的 duration 位移」（window.py:
        # 2739-2744 的 _switch 失败语义）
        if not self._bind_with_gen(sprite, st, plan["anim"]):
            return False
        # 开播确认后才提交朝向（朝向只跟随真实发生的移动）。镜像在 paint
        # 重建首帧时才取用 facing，此刻写入不产生首帧镜像错误。
        sprite.facing = plan["facing"]
        st.state = STATE_MOVE
        st.anim = plan["anim"]
        st.elapsed = 0.0
        st.duration = plan["duration"]
        st.move_target = plan["target"]
        st.move_start = QPointF(sprite.pos)
        st.pending_move = None
        st.curve = plan.get("curve")
        st.frames_per_loop = plan.get("frames_per_loop", 0)
        st.loops = plan.get("loops", 1)
        st.loop_duration = plan.get("loop_duration", 0.0)
        if plan["duration"] <= 0:
            sprite.set_pos(plan["target"])
            sprite.set_velocity(QPointF(0, 0))
        elif st.curve:
            sprite.set_velocity(QPointF(0, 0))  # 下一 tick 起由曲线接管
        else:
            vx = (plan["target"].x() - sprite.pos.x()) / plan["duration"]
            vy = (plan["target"].y() - sprite.pos.y()) / plan["duration"]
            sprite.set_velocity(QPointF(vx, vy))
        return True

    def _apply_move_curve(self, sprite, st: _SpriteState, dt: float) -> None:
        """曲线驱动每 tick 目标位置 → 反算 velocity（F3）。

        desired 由曲线进度（锚在计划起点的**绝对**位置）算得，故
        ``v = (desired - pos) / dt`` 积分后恰好落在 desired：既无残差累积，
        也不会因某 tick 的 dt 抖动而失步。静帧段 desired == 当前 pos →
        velocity 归零 → 位置一丝不动（旧 window.py:1773-1778 的帧驱动位移）。

        相位源取舍见 movement.curve_progress_at_time：旧架构用解码帧号，
        新架构用墙钟，漂移上界亚像素。无 curve / 无计划起点 / 无 dt 时
        no-op（线性路径的 velocity 在 _start_move 已设好）。
        """
        if dt <= 0 or st.curve is None or st.move_target is None or st.move_start is None:
            return
        progress = movement.curve_progress_at_time(
            st.curve, st.frames_per_loop, st.loops, st.elapsed, st.loop_duration)
        sx, sy = st.move_start.x(), st.move_start.y()
        dx = st.move_target.x() - sx
        dy = st.move_target.y() - sy
        sprite.set_velocity(QPointF(
            (sx + dx * progress - sprite.pos.x()) / dt,
            (sy + dy * progress - sprite.pos.y()) / dt,
        ))

    # ---------------------------------------------------------------- 边界
    def _clamp_into_bounds(self, sprite) -> None:
        """拖拽/抛掷切回 normal 后落点在界外的兜底。

        口径与 set_pos 一致（V-2）：身体框完整落界内、画布透明边允许
        越界——不再用整 canvas 矩形钳制（那会把按身体框贴边的 sprite
        每 tick 拉回一个透明边距，造成可见瞬移）。sprite 已挂 bounds 时
        set_pos 即唯一权威，这里的计算结果落在其允许域内，不会打架。
        """
        r = sprite.rect()
        body = sprite.body_rect() if hasattr(sprite, "body_rect") else r
        off_x, off_y = body.x() - r.x(), body.y() - r.y()
        lo_x = float(self.bounds.x()) - off_x
        lo_y = float(self.bounds.y()) - off_y
        max_x = lo_x + self.bounds.width() - body.width()
        max_y = lo_y + self.bounds.height() - body.height()
        x = min(max(sprite.pos.x(), lo_x), float(max(lo_x, max_x)))
        y = min(max(sprite.pos.y(), lo_y), float(max(lo_y, max_y)))
        if x != sprite.pos.x() or y != sprite.pos.y():
            sprite.set_pos(QPointF(x, y))

    # ---------------------------------------------------------------- 素材查询
    def _categories(self, lib) -> dict:
        """角色的 idle/turn/move/click/acts/drag 池（按库缓存）。

        真实 MovieLibrary 走 catalog.build_categories（与 window.py:406 同一
        入口，folder_map/folder_files/manifest 全透传）；测试假库直接暴露
        idles/turns/moves/clicks 池属性，drag 为可选单值属性。
        drag 在 catalog 侧是**单值 name 或 None**（一个角色只有一段悬空
        素材），这里统一规范化成列表，调用方按池处理（F1）。
        """
        cats = self._cats_cache.get(lib)
        if cats is None:
            names = getattr(lib, "names", None)
            if callable(names):
                raw = catalog.build_categories(
                    names(), getattr(lib, "manifest", None),
                    getattr(lib, "folder_map", None), getattr(lib, "folder_files", None))
                cats = {k: list(raw[k]) for k in ("idles", "turns", "moves", "clicks", "acts")}
                cats["drag"] = [raw["drag"]] if raw.get("drag") else []
            else:
                cats = {k: list(getattr(lib, k, None) or []) for k in ("idles", "turns", "moves", "clicks", "acts")}
                drag = getattr(lib, "drag", None)
                cats["drag"] = [drag] if drag else []
            self._cats_cache[lib] = cats
        return cats

    def _body_geometry(self, sprite) -> tuple[float, float, float, float]:
        """身体框相对 sprite pos 的 (offset_x, offset_y, width, height)。

        优先角色 manifest 的 body_box（乘 scale）；未声明（或假库无
        character_id）回退"窗口即身体"（catalog.character_body_box 文档
        语义）。
        """
        rect = sprite.rect()
        lib = getattr(sprite, "library", None)
        cid = getattr(lib, "character_id", None)
        if cid:
            box = catalog.character_body_box(cid)
            if box is not None:
                s = float(getattr(sprite, "scale", 1.0))
                x1, y1, x2, y2 = box
                return x1 * s, y1 * s, (x2 - x1) * s, (y2 - y1) * s
        return 0.0, 0.0, float(rect.width()), float(rect.height())

    @staticmethod
    def _clip_duration(lib, name: str) -> float:
        dur = getattr(lib, "duration", None)
        return float(dur(name)) if callable(dur) else 0.0

    def _pick(self, pool, exclude: str | None = None):
        entries = [n for n in pool if n != exclude] or list(pool)
        if not entries:
            return None
        return self.rng.choice(entries)
