# -*- coding: utf-8 -*-
"""行为状态机（单合成窗架构 Phase 1b）：游荡/走路/待机/转向/点击反应。

架构背景见 .scratch/single-overlay-window/spec.md。本控制器在 overlay 的
before_sprites_advance(dt) 阶段运行（位置积分仍由 sprite.advance 统一完成）：

    class PetOverlay(OverlayWindow):
        def before_sprites_advance(self, dt):
            self.behavior.tick(self.sprites, dt)

只在 sprite.interaction_state == "normal" 时驱动（写 velocity/facing/
bind_clip）；drag/thrown 直接跳过——拖拽由鼠标路由、抛掷由物理控制器
接管，落地静止切回 "normal" 后本控制器从当前姿态继续。

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
  velocity = 位移/时长 → 平均速度恒等于动画步态速度（防脚滑）。走路
  素材 24fps vs tick 6ms，velocity 积分天然平滑，不做帧锚定；
- 点击反应 = 打断当前行为播 click 池随机 clip，播完回待机
  （window.py:3438 _on_click 语义；音效/黄金回旋等外围效果不在本层）。

完成判定用墙钟（tick 累加 dt ≥ clip 时长），不连 clip.finished：clip 是
library 缓存的共享资源，bind_clip 已负责启停；墙钟与解码进度的漂移
上界是一个 clip 时长内的解码节流误差，到点统一 snap 收口，误差 ≤ 一个
tick 的位移（亚像素）。
"""

from __future__ import annotations

import random
from weakref import WeakKeyDictionary

from PySide6.QtCore import QPointF, QRect

from . import catalog, movement
from .pet_sprite import INTERACTION_NORMAL
from .predictive_prewarm import PredictivePrewarm

STATE_IDLE = "idle"
STATE_MOVE = "move"
STATE_TURN = "turn"
STATE_CLICK = "click"
STATE_ACTS = "acts"


class _SpriteState:
    """单个 sprite 的行为状态（控制器私有，不落在 sprite 上）。"""

    __slots__ = ("state", "anim", "elapsed", "duration", "move_target",
                 "pending_move", "predictor")

    def __init__(self) -> None:
        self.state = STATE_IDLE
        self.anim: str | None = None      # 当前绑定的 clip 名（None = 尚未起播）
        self.elapsed = 0.0                # 当前状态已流逝（tick 累加的 dt）
        self.duration = 0.0               # 当前 clip 时长（秒）
        self.move_target: QPointF | None = None
        self.pending_move: dict | None = None  # 反向前先转向的移动计划
        self.predictor = None                  # tick 创建状态时挂 PredictivePrewarm


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
        """推进一轮：只驱动 normal 状态的 sprite，其余（drag/thrown）跳过。"""
        for sprite in sprites:
            if getattr(sprite, "interaction_state", INTERACTION_NORMAL) != INTERACTION_NORMAL:
                continue
            st = self._states.get(sprite)
            if st is None:
                st = self._states[sprite] = _SpriteState()
                st.predictor = self._make_predictor(sprite)
            self._tick_sprite(sprite, st, dt)

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
        st.move_target = None
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
        st.move_target = None
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
        """当前行为状态（idle/move/turn/click）；未接管过返回 None。"""
        st = self._states.get(sprite)
        return st.state if st is not None else None

    def forget(self, sprite) -> None:
        """sprite 从 overlay 移除时清理其状态（可选，防状态表只增不减）。"""
        self._states.pop(sprite, None)

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
            if st.elapsed >= st.duration:
                if st.move_target is not None:
                    sprite.set_pos(st.move_target)  # 到点 snap，消除积分残差
                sprite.set_velocity(QPointF(0, 0))
                self._enter_idle(sprite, st, self._categories(sprite.library))
        elif st.state == STATE_TURN:
            if st.elapsed >= st.duration:
                # 转向播完才翻朝向（window.py:2631-2633）：turn clip 播完
                # 即画面已转向，此刻翻 facing 无跳变。
                sprite.facing = "right" if sprite.facing == "left" else "left"
                pending = st.pending_move
                st.pending_move = None
                if pending is not None:
                    self._start_move(sprite, st, pending)
                else:
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

    def _bind_with_gen(self, sprite, st: _SpriteState, name: str) -> None:
        """bind + 预测代次推进（begin_anim 每次切换自增；作废由 consume 的
        context/gen 校验完成，不手动清预测——GLM A4 单规则）。"""
        sprite.bind_clip(name)
        if st.predictor is not None:
            st.predictor.begin_anim(name)

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
    def _enter_idle(self, sprite, st: _SpriteState, cats: dict,
                    forced_name: str | None = None) -> None:
        st.state = STATE_IDLE
        st.pending_move = None
        st.move_target = None
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

    def _enter_acts(self, sprite, st: _SpriteState, cats: dict,
                    forced_name: str | None = None) -> None:
        """随机动作（40% acts 桶）：acts 池随机一段，播完回掷骰
        （window.py _pick_next 的 acts 分支语义）；池空回退待机。"""
        name = forced_name if forced_name is not None else self._pick(cats["acts"], exclude=st.anim)
        if name is None:
            self._enter_idle(sprite, st, cats)
            return
        st.state = STATE_ACTS
        st.pending_move = None
        st.move_target = None
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
        st.move_target = None
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
        _loops, distance, duration = movement.quantize_move(distance, stride, room, loop_duration)
        target_cx = cx + dir_sign * distance
        target_x = target_cx - bw / 2 - off_x
        target_y = movement.wander_target_y(
            sprite.pos.y() + off_y, self.bounds.top(), self.bounds.bottom(),
            bh, self.margin, self.rng) - off_y
        plan = {
            "anim": name,
            "target": QPointF(target_x, target_y),
            "duration": duration,
            "facing": "right" if dir_sign > 0 else "left",
        }
        if plan["facing"] != sprite.facing and cats["turns"]:
            # 需要反向：先播 turn clip，翻朝向后由 turn 完成分支执行本计划
            self._enter_turn(sprite, st, cats, pending_move=plan)
        else:
            self._start_move(sprite, st, plan)
        return True

    def _start_move(self, sprite, st: _SpriteState, plan: dict) -> None:
        st.state = STATE_MOVE
        st.anim = plan["anim"]
        st.elapsed = 0.0
        st.duration = plan["duration"]
        st.move_target = plan["target"]
        st.pending_move = None
        # 朝向先于 bind：bind 重建首帧时已按新朝向镜像，无首帧镜像错误
        sprite.facing = plan["facing"]
        self._bind_with_gen(sprite, st, plan["anim"])
        if plan["duration"] > 0:
            vx = (plan["target"].x() - sprite.pos.x()) / plan["duration"]
            vy = (plan["target"].y() - sprite.pos.y()) / plan["duration"]
            sprite.set_velocity(QPointF(vx, vy))
        else:
            sprite.set_pos(plan["target"])
            sprite.set_velocity(QPointF(0, 0))

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
        """角色的 idle/turn/move/click 池（按库缓存）。

        真实 MovieLibrary 走 catalog.build_categories（与 window.py:406 同一
        入口，folder_map/folder_files/manifest 全透传）；测试假库直接暴露
        idles/turns/moves/clicks 四个池属性。
        """
        cats = self._cats_cache.get(lib)
        if cats is None:
            names = getattr(lib, "names", None)
            if callable(names):
                raw = catalog.build_categories(
                    names(), getattr(lib, "manifest", None),
                    getattr(lib, "folder_map", None), getattr(lib, "folder_files", None))
                cats = {k: list(raw[k]) for k in ("idles", "turns", "moves", "clicks", "acts")}
            else:
                cats = {k: list(getattr(lib, k, None) or []) for k in ("idles", "turns", "moves", "clicks", "acts")}
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
