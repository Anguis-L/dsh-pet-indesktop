# -*- coding: utf-8 -*-
"""Phase 1b 行为状态机 offscreen 单测（QT_QPA_PLATFORM=offscreen 可跑）。

覆盖：待机→走路转移、走路到点回待机、反向先转向再改 facing、非 normal
状态不驱动、点击播 click clip（含打断移动）、目标点不出界、边缘可达性
不足回退待机、build_categories 真实分类路径。

纪律（AGENTS.md 时序测试）：注入 ScriptedRng 确定性随机源；控制器时间
完全由 tick(dt) 累加驱动，直接同步调用 tick/advance，不起真实 QTimer、
不固定 sleep。假 clip/假 library 纯 QImage，不依赖 webm 素材与 ffmpeg。
"""
from __future__ import annotations

import os
import random
from collections import deque

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from pet.pet_sprite import (
    INTERACTION_DRAG,
    INTERACTION_NORMAL,
    INTERACTION_THROWN,
    PetSprite,
)
from pet.sprite_behavior import (
    STATE_ACTS,
    STATE_CLICK,
    STATE_DRAG,
    STATE_IDLE,
    STATE_MOVE,
    STATE_THROWN,
    STATE_TURN,
    BehaviorController,
)

app = QApplication.instance() or QApplication([])

BOUNDS = QRect(0, 0, 2000, 1000)


# ---------------------------------------------------------------- 假 clip / 假 library
class FakeClip(QObject):
    """接口对齐 WebMClip 的假 clip：固定帧数 → 时长 = frames × 42ms。"""

    frameChanged = Signal(int)
    finished = Signal()

    def __init__(self, name, frames=24, accept_start=True):
        super().__init__()
        self.name = name
        self._frames = frames
        self.image = QImage(640, 360, QImage.Format.Format_ARGB32)
        self.image.fill(0xFF336699)
        self.frame = 0
        self.started = False
        self.start_count = 0
        # 开播拒绝开关（F6）：WebMClip.start() 在 reader 拒启/已 cleanup 时
        # 返回 False，调用方必须据此放弃依赖该动画的状态
        self.accept_start = accept_start

    def currentFrameNumber(self):
        return self.frame

    def currentImage(self):
        return self.image

    def frameCount(self):
        return self._frames

    def duration(self):
        return self._frames * 42 / 1000.0

    def start(self):
        self.started = True
        self.start_count += 1
        return bool(self.accept_start)

    def stop(self):
        self.started = False


class FakeLibrary:
    """轻量库协议：直接暴露 idles/turns/moves/clicks 池属性（无 names()）。"""

    def __init__(self, *, idles, turns, moves, clicks, drag=None, frames=None,
                 strides=None, curves=None):
        self.idles = list(idles)
        self.turns = list(turns)
        self.moves = list(moves)
        self.clicks = list(clicks)
        self.drag = drag
        frames = frames or {}
        self._clips = {}
        names = self.idles + self.turns + self.moves + self.clicks
        if drag:
            names = names + [drag]
        for name in names:
            self._clips[name] = FakeClip(name, frames.get(name, 24))
        self.move_strides = dict(strides or {})
        self.move_curves = dict(curves or {})
        self.no_mirror: set[str] = set()

    def movie(self, name):
        return self._clips[name]

    def duration(self, name):
        return self._clips[name].duration()

    def clip(self, name):
        return self._clips[name]


class ScriptedRng:
    """确定性随机源：rolls/ints/choices 队列消费完后回落到稳妥默认值。"""

    def __init__(self, rolls=(), ints=(), choices=()):
        self.rolls = deque(rolls)
        self.ints = deque(ints)
        self.choices = deque(choices)

    def random(self):
        return self.rolls.popleft() if self.rolls else 0.0

    def randint(self, a, b):
        return self.ints.popleft() if self.ints else a

    def choice(self, seq):
        # 脚本指定的选项只在属于本序列时才消费（无关调用落回 seq[0]），
        # 避免 _pick 等中间调用吃掉为 choose_move_direction 准备的方向
        if self.choices and self.choices[0] in seq:
            return self.choices.popleft()
        return seq[0]


def _make_library(**overrides):
    kwargs = dict(
        idles=["idle1"], turns=["turn1"], moves=["walk"], clicks=["click1"],
        frames={"idle1": 24, "turn1": 12, "walk": 48, "click1": 12},
        strides={"walk": 120},
    )
    kwargs.update(overrides)
    return FakeLibrary(**kwargs)


def _make_sprite(lib, pos=(800, 400), facing="left", scale=0.5):
    return PetSprite(lib, pos=QPointF(*pos), facing=facing, scale=scale)


def _run(controller, sprite, seconds, dt=0.05):
    """模拟 overlay tick 循环：controller.tick 在前，sprite.advance 积分在后。"""
    for _ in range(int(round(seconds / dt))):
        controller.tick([sprite], dt)
        sprite.advance(dt)


def _roll_into_move(controller, sprite, lib, *, rolls=(0.99,), ints=(100, 0), choices=(1,)):
    """从待机起步走到「移动已开始」：首 tick 进待机，播完一圈后掷中移动桶。"""
    controller.rng.rolls.extend(rolls)
    controller.rng.ints.extend(ints)
    controller.rng.choices.extend(choices)
    controller.tick([sprite], 0.016)
    sprite.advance(0.016)
    idle_dur = lib.duration("idle1")
    _run(controller, sprite, idle_dur + 0.1)


# ---------------------------------------------------------------- 基础进入与转移
def test_first_tick_enters_idle():
    lib = _make_library()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.tick([sprite], 0.016)
    assert c.state_of(sprite) == STATE_IDLE
    assert lib.clip("idle1").start_count == 1
    assert sprite.velocity == QPointF(0, 0)


def test_idle_to_move_transition():
    lib = _make_library()
    # 朝右 + 方向右：无需先转向，直接进移动
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    _roll_into_move(c, sprite, lib)

    assert c.state_of(sprite) == STATE_MOVE
    assert lib.clip("walk").start_count == 1
    assert sprite.velocity.x() > 0
    assert sprite.facing == "right"


def test_move_reaches_target_and_returns_to_idle():
    lib = _make_library()
    sprite = _make_sprite(lib, pos=(800, 400), facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    _roll_into_move(c, sprite, lib, ints=(100, 0))

    # 期望目标：cx=960, room=859；distance=100 → 量化 2 圈×60px=120px，
    # duration=2×2.016=4.032s；target_cx=1080 → target_x=920；dy=0 → y=400
    _run(c, sprite, 2.0)
    assert 800 < sprite.pos.x() < 920          # 途中：未到点、未越点
    assert c.state_of(sprite) == STATE_MOVE

    _run(c, sprite, 2.5)                        # 累计 4.5s > 4.032s
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos == QPointF(920, 400)      # 到点 snap
    assert sprite.velocity == QPointF(0, 0)
    assert lib.clip("idle1").start_count == 2   # 回待机重播 idle


def test_turn_before_reverse_move():
    lib = _make_library()
    sprite = _make_sprite(lib, facing="left")   # 朝左却要向右走 → 先转向
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, choices=(1,))

    assert c.state_of(sprite) == STATE_TURN
    assert lib.clip("turn1").start_count == 1
    assert lib.clip("walk").start_count == 0
    assert sprite.facing == "left"              # turn 播完前朝向不变
    assert sprite.velocity == QPointF(0, 0)

    _run(c, sprite, lib.duration("turn1") + 0.1)
    assert sprite.facing == "right"             # turn 播完才翻朝向
    assert c.state_of(sprite) == STATE_MOVE
    assert lib.clip("walk").start_count == 1
    assert sprite.velocity.x() > 0


def test_turn_roll_without_correction_degrades_to_idle():
    lib = _make_library()
    # 屏幕中线附近（滞回带内）掷中转向桶（0.3~0.4）→ 降级待机
    sprite = _make_sprite(lib, pos=(900, 400), facing="left")
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.35,)))
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, rolls=())
    assert c.state_of(sprite) == STATE_IDLE
    assert lib.clip("turn1").start_count == 0
    assert sprite.facing == "left"


def test_outward_facing_idle_roll_turns_inward():
    lib = _make_library()
    # 贴左缘且朝左（朝外）：掷中待机桶（<0.3）也改播转向纠朝向
    sprite = _make_sprite(lib, pos=(0, 400), facing="left")
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.1,)))
    _roll_into_move(c, sprite, lib, rolls=())
    assert c.state_of(sprite) == STATE_TURN
    assert lib.clip("turn1").start_count == 1
    _run(c, sprite, lib.duration("turn1") + 0.1)
    assert sprite.facing == "right"             # 纠完朝内，回待机
    assert c.state_of(sprite) == STATE_IDLE


# ---------------------------------------------------------------- 非 normal 状态
def test_captured_sprites_bind_owned_clip_not_rolls():
    """F1 语义：拖拽态只接受 drag 绑定，不接受掷骰驱动。

    旧断言（「非 normal 精灵从不被接管、不绑任何 clip」）在本刀补齐悬空
    动画后不再成立：接管期间画面归本控制器（绑 drag clip），但掷骰状态机
    绝不推进、velocity 绝不被改写（位置归鼠标/物理）。
    """
    lib = _make_library(drag="hang")
    sprite = _make_sprite(lib)
    sprite.interaction_state = INTERACTION_DRAG
    sprite.set_velocity(QPointF(50, 0))
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    _run(c, sprite, 3.0)
    assert c.state_of(sprite) == STATE_DRAG
    assert sprite._clip_name == "hang"          # 只绑 drag（悬空），不掷骰
    assert sprite.velocity == QPointF(50, 0)    # velocity 不被改写
    assert lib.clip("walk").start_count == 0    # 掷骰驱动的移动从未发生
    assert c.on_sprite_clicked(sprite) is False
    assert lib.clip("click1").start_count == 0


# ---------------------------------------------------------------- F1：拖拽悬空动画
def test_drag_started_binds_drag_and_release_returns_idle():
    lib = _make_library(drag="hang")
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    c.tick([sprite], 0.016)                         # 先接管进待机
    assert c.state_of(sprite) == STATE_IDLE

    sprite.on_press(QPointF(10, 10))                # overlay 接线点：按下
    c.on_drag_started(sprite)

    assert c.state_of(sprite) == STATE_DRAG
    assert sprite._clip_name == "hang"
    assert sprite.velocity == QPointF(0, 0)

    _run(c, sprite, 0.5)                            # 拖拽期间掷骰绝不改绑
    assert c.state_of(sprite) == STATE_DRAG
    assert sprite._clip_name == "hang"
    assert lib.clip("walk").start_count == 0

    sprite.on_release(QPointF(12, 12))              # 真拖拽松手（非甩出）
    c.on_drag_released(sprite)

    assert sprite.interaction_state == INTERACTION_NORMAL
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite._clip_name == "idle1"


def test_drag_without_drag_asset_falls_back_to_idle_pool():
    lib = _make_library()                           # 无 drag 素材
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    c.tick([sprite], 0.016)

    sprite.on_press(QPointF(10, 10))
    c.on_drag_started(sprite)

    assert c.state_of(sprite) == STATE_DRAG
    assert sprite._clip_name == "idle1"             # 回退 idle 池


def test_drag_state_returns_to_idle_when_drag_ends_without_release_callback():
    """防御：接线缺失（看门狗收尾/shell 分支没走到）时也绝不能卡在拖拽态。

    sprite 已回 normal 却仍是 STATE_DRAG：tick 必须自愈回待机（绑 idle、
    清 velocity），否则悬空动画无限循环。
    """
    lib = _make_library(drag="hang")
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    c.tick([sprite], 0.016)
    sprite.interaction_state = INTERACTION_DRAG
    c.tick([sprite], 0.016)
    assert c.state_of(sprite) == STATE_DRAG

    sprite.interaction_state = INTERACTION_NORMAL  # 看门狗已收尾，无人回调

    c.tick([sprite], 0.016)
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite._clip_name == "idle1"


# ---------------------------------------------------------------- F4：抛掷飞行期动画
def test_thrown_binds_drag_clip_and_landing_returns_idle():
    """F4：飞行期固定播 drag（悬空）clip 并循环；落地回 normal 后切回待机。

    位置/速度归 sprite_physics，本控制器只接管画面（绝不改写 velocity）。
    """
    lib = _make_library(drag="hang")
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    c.tick([sprite], 0.016)                          # 先接管进待机

    sprite.set_velocity(QPointF(800, -200))
    sprite.interaction_state = INTERACTION_THROWN
    c.tick([sprite], 0.016)

    assert c.state_of(sprite) == STATE_THROWN
    assert sprite._clip_name == "hang"
    assert sprite.velocity == QPointF(800, -200)     # 速度归物理，绝不改写

    lib.clip("hang").finished.emit()                 # 飞行过圈末
    assert lib.clip("hang").start_count == 2         # 悬空动画循环（不冻结）

    sprite.interaction_state = INTERACTION_NORMAL    # 落地（sprite_physics 收尾）
    c.tick([sprite], 0.016)
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite._clip_name == "idle1"
    assert sprite.velocity == QPointF(0, 0)


def test_thrown_without_drag_asset_falls_back_to_idle_pool():
    lib = _make_library()                            # 无 drag 素材
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    c.tick([sprite], 0.016)

    sprite.interaction_state = INTERACTION_THROWN
    c.tick([sprite], 0.016)

    assert c.state_of(sprite) == STATE_THROWN
    assert sprite._clip_name == "idle1"


def test_drag_release_not_taken_over_when_thrown():
    """松手被判为甩出：on_drag_released 不得把飞行动画抢成待机。"""
    lib = _make_library(drag="hang")
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    c.tick([sprite], 0.016)
    sprite.on_press(QPointF(10, 10))
    c.on_drag_started(sprite)

    sprite.interaction_state = INTERACTION_THROWN    # 模拟 on_release 的甩出判定
    c.on_drag_released(sprite)

    assert c.state_of(sprite) == STATE_DRAG          # 待机链不得抢走飞行段
    c.tick([sprite], 0.016)
    assert c.state_of(sprite) == STATE_THROWN
    assert sprite._clip_name == "hang"


# ---------------------------------------------------------------- F6：开播失败不建计划
def _roll_once_after_idle(c, sprite, lib):
    """让 sprite 走完当前待机进下一次掷骰（不预设结果状态）。"""
    c.tick([sprite], 0.016)
    _run(c, sprite, lib.duration("idle1") + 0.1)


def test_bind_failure_builds_no_move_plan():
    """F6：移动素材开播被拒 → 不建立移动计划（绝不按没播的动画位移）。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,), ints=(100, 0), choices=(1,)))
    c.predict_enabled = False
    lib.clip("walk").accept_start = False
    _roll_once_after_idle(c, sprite, lib)

    st = c._states[sprite]
    assert lib.clip("walk").start_count == 1        # 确实尝试过开播
    assert c.state_of(sprite) == STATE_IDLE         # acts 空 → 回退待机
    assert st.move_target is None
    assert st.curve is None
    assert sprite.velocity == QPointF(0, 0)
    assert sprite.pos == QPointF(800, 400)          # 位置一丝不动


def test_bind_failure_falls_back_to_acts_pool():
    """F6：开播失败走旧机同款回退链（window.py:2740 _switch 失败 → acts 池）。"""
    lib = _library_with_acts()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,), ints=(100, 0), choices=(1,)))
    c.predict_enabled = False
    lib.clip("walk").accept_start = False
    _roll_once_after_idle(c, sprite, lib)

    assert c.state_of(sprite) == STATE_ACTS
    assert sprite._clip_name in ("act1", "act2")
    assert c._states[sprite].move_target is None
    assert sprite.velocity == QPointF(0, 0)


def test_bind_failure_after_turn_collects_to_idle_without_double_flip():
    """F6：转向完成时移动开播被拒 → 收口待机（不留在 turn 态二次翻朝向）。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="left")       # 朝左却要向右走 → 先转向
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,), ints=(100, 0), choices=(1,)))
    c.predict_enabled = False
    _roll_once_after_idle(c, sprite, lib)
    assert c.state_of(sprite) == STATE_TURN
    lib.clip("walk").accept_start = False

    _run(c, sprite, lib.duration("turn1") + 0.05)   # 转向播完 → 移动开播被拒

    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.facing == "right"                 # 只翻这一次
    _run(c, sprite, lib.duration("turn1") * 2)
    assert sprite.facing == "right"                 # 绝不二次翻转
    assert sprite.pos == QPointF(800, 400)


def test_play_move_once_bind_failure_returns_false():
    """F6：菜单「移动」入口同样不建计划（返回 False，回退链兜底）。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="left")       # 与默认方向（左）一致 → 直接起步
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.tick([sprite], 0.016)
    lib.clip("walk").accept_start = False

    assert c.play_move_once(sprite, "walk") is False

    assert c.state_of(sprite) == STATE_IDLE
    assert c._states[sprite].move_target is None
    assert sprite.velocity == QPointF(0, 0)


# ---------------------------------------------------------------- F5：接管撤销移动计划
def test_capture_marks_suspended_and_drops_move_plan_on_return():
    """F5：接管期间打 suspended；回到 normal 后移动态收口回待机，绝不 snap。

    旧实现 window.py:4174-4177 _enter_physics_mode→_cancel_move：物理接管
    即撤销自主移动计划，否则松手后 elapsed 继续累加、一到 duration 就
    set_pos(旧目标) 瞬移。
    """
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, ints=(100, 0))
    st = c._states[sprite]
    assert c.state_of(sprite) == STATE_MOVE
    _run(c, sprite, 0.5)
    assert 800 < sprite.pos.x() < 920

    sprite.interaction_state = INTERACTION_DRAG
    c.tick([sprite], 0.05)
    assert st.suspended is True                     # 接管标记（收口依据）

    sprite.interaction_state = INTERACTION_NORMAL
    c.tick([sprite], 0.05)

    assert st.suspended is False
    assert c.state_of(sprite) == STATE_IDLE
    assert st.move_target is None                   # 计划已撤销
    assert st.pending_move is None
    assert sprite.velocity == QPointF(0, 0)


def test_drag_interrupt_move_never_snaps_to_old_target():
    """验收 ④：拖拽打断移动后不瞬移不 snap（拖到别处松手，位置就地保留）。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, ints=(100, 0))
    _run(c, sprite, 0.5)

    sprite.interaction_state = INTERACTION_DRAG      # 鼠标接管
    c.tick([sprite], 0.05)
    sprite.set_pos(QPointF(1500, 400))               # 拖到别处
    sprite.interaction_state = INTERACTION_NORMAL    # 松手（无回调，看门狗收尾）
    c.tick([sprite], 0.05)

    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos == QPointF(1500, 400)          # 绝不回到 920/旧路线
    assert sprite.velocity == QPointF(0, 0)

    _run(c, sprite, 5.0)                             # 长跑：也不会突然瞬移
    assert sprite.pos.x() >= 1500 - 320              # 只可能被正常游荡带走


def test_capture_during_turn_drops_pending_move():
    """F5：转向途中被接管 → pending_move 作废，回到 normal 不执行该移动。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="left")        # 朝左却要向右走 → 先转向
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, choices=(1,))
    st = c._states[sprite]
    assert c.state_of(sprite) == STATE_TURN
    assert st.pending_move is not None

    sprite.interaction_state = INTERACTION_DRAG
    c.tick([sprite], 0.05)
    sprite.interaction_state = INTERACTION_NORMAL
    c.tick([sprite], 0.05)

    assert c.state_of(sprite) == STATE_IDLE
    assert st.pending_move is None
    assert lib.clip("walk").start_count == 0         # 被撤销的移动绝不启动


# ---------------------------------------------------------------- F2：多圈续播 re-arm
def test_drag_finished_rearms_clip():
    """F2：拖拽悬空动画过圈末必须原地续播（否则长拖拽动画冻结）。"""
    lib = _make_library(drag="hang")
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    c.tick([sprite], 0.016)

    sprite.on_press(QPointF(10, 10))
    c.on_drag_started(sprite)
    assert lib.clip("hang").start_count == 1

    lib.clip("hang").finished.emit()                # WebMClip 圈末结束标记

    assert lib.clip("hang").start_count == 2        # re-arm：不冻结在末帧


def test_multi_loop_move_finished_rearms_clip():
    """F2：多圈移动第 2 圈起动画冻结的根修——中间圈 finished 触发重播。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, ints=(100, 0))  # 2 圈 × 60px
    st = c._states[sprite]
    assert c.state_of(sprite) == STATE_MOVE
    assert st.duration > lib.duration("walk")       # 确实是多圈计划
    assert lib.clip("walk").start_count == 1

    _run(c, sprite, lib.duration("walk") + 0.05)    # 推进到第 1 圈末
    assert c.state_of(sprite) == STATE_MOVE         # 移动尚未到点
    lib.clip("walk").finished.emit()                # WebMClip 圈末结束标记

    assert lib.clip("walk").start_count == 2        # 第 2 圈续播，不冻结
    assert c.state_of(sprite) == STATE_MOVE

    _run(c, sprite, st.duration)                    # 整段跑完仍正常收口
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos == QPointF(920, 400)


def test_clip_finished_ignored_when_no_remaining_time():
    """末圈 finished 不续播（剩余时长为 0）：收口交给到点 snap。

    否则已完成的移动会被重新起播一整圈，且 snap 后立刻绑待机 = 白起一
    次解码。
    """
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, ints=(100, 0))
    st = c._states[sprite]
    st.elapsed = st.duration                        # 末圈已到点（tick 即将收口）

    lib.clip("walk").finished.emit()

    assert lib.clip("walk").start_count == 1        # 不续播


def test_clip_finished_ignored_for_states_without_rearm():
    """点击/待机等一次性的状态链不接 finished 续播（收口由墙钟到点负责）。"""
    lib = _make_library()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    c.tick([sprite], 0.016)
    assert c.state_of(sprite) == STATE_IDLE
    assert lib.clip("idle1").start_count == 1

    lib.clip("idle1").finished.emit()

    assert lib.clip("idle1").start_count == 1


# ---------------------------------------------------------------- 点击反应
def test_click_plays_click_clip_then_idle():
    lib = _make_library()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.tick([sprite], 0.016)

    assert c.on_sprite_clicked(sprite) is True
    assert c.state_of(sprite) == STATE_CLICK
    assert lib.clip("click1").start_count == 1

    _run(c, sprite, lib.duration("click1") + 0.1)
    assert c.state_of(sprite) == STATE_IDLE
    assert lib.clip("idle1").start_count == 2


def test_click_interrupts_move():
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    _roll_into_move(c, sprite, lib)
    assert c.state_of(sprite) == STATE_MOVE

    assert c.on_sprite_clicked(sprite) is True
    assert c.state_of(sprite) == STATE_CLICK
    assert sprite.velocity == QPointF(0, 0)         # 移动被打断
    assert lib.clip("click1").start_count == 1

    pos_at_click = QPointF(sprite.pos)
    _run(c, sprite, lib.duration("click1") + 0.1)
    assert c.state_of(sprite) == STATE_IDLE         # 播完回待机，不恢复移动
    assert sprite.pos == pos_at_click


def test_click_without_click_pool_returns_false():
    lib = _make_library(clicks=[])
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    assert c.on_sprite_clicked(sprite) is False


# ---------------------------------------------------------------- 边界
def test_move_targets_stay_inside_bounds():
    # 随机长跑：真实 random 播种，任何时刻 sprite 矩形不得出活动边界
    lib = _make_library()
    sprite = _make_sprite(lib, pos=(100, 300), facing="left")
    bounds = QRect(0, 0, 1200, 800)
    c = BehaviorController(bounds, rng=random.Random(42))
    for _ in range(6000):  # 6000 × 50ms = 5 分钟模拟时长
        c.tick([sprite], 0.05)
        sprite.advance(0.05)
        assert bounds.contains(sprite.rect())


def test_move_fallback_to_idle_when_no_room():
    lib = _make_library()
    # 窄边界：两侧空间都 < MOVE_MIN_PX → choose_move_direction None → 回退待机
    sprite = _make_sprite(lib, pos=(20, 400))
    c = BehaviorController(QRect(0, 0, 400, 1000), rng=ScriptedRng(rolls=(0.99,)))
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, rolls=())
    assert c.state_of(sprite) == STATE_IDLE
    assert lib.clip("walk").start_count == 0
    assert sprite.velocity == QPointF(0, 0)


def test_clamp_pulls_out_of_bounds_sprite_back():
    lib = _make_library()
    sprite = _make_sprite(lib, pos=(-50, 5000))     # 界外落点（如抛掷切回 normal）
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.tick([sprite], 0.016)
    assert BOUNDS.contains(sprite.rect())


# ---------------------------------------------------------------- F3：圈内逐帧位移曲线
def _flat_head_curve(frames=48, still_ratio=0.25):
    """前 still_ratio 圈长走平（静帧段）、其余线性推进到 1.0 的曲线。

    素材实测：左转奔跑前 23% 圈长是静止蓄力段，位移必须为 0。
    """
    still = max(1, int(frames * still_ratio))
    tail = frames - still
    return [0.0] * still + [i / (tail - 1) for i in range(tail)]


def test_move_curve_holds_position_on_still_frames():
    """F3：有曲线的移动——静帧段位置一丝不动，动帧段正常推进，到点仍 snap。"""
    lib = _make_library(curves={"walk": _flat_head_curve(48, 0.25)})
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, ints=(100, 0))
    st = c._states[sprite]
    assert c.state_of(sprite) == STATE_MOVE
    assert st.curve is not None                 # 计划确实带回曲线（旧实现零引用）
    start = QPointF(sprite.pos)

    _run(c, sprite, lib.duration("walk") * 0.2)
    assert sprite.pos == start                  # 静帧段：位移恒为 0
    assert sprite.velocity == QPointF(0, 0)

    _run(c, sprite, lib.duration("walk") * 0.6)
    assert sprite.pos.x() > start.x()           # 动帧段：正常推进

    _run(c, sprite, st.duration)
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos == QPointF(920, 400)      # 到点仍 snap 到目标


def test_move_without_curve_stays_linear():
    """无曲线素材保持线性（与旧语义一致）：半程 ≈ 半位移。"""
    lib = _make_library()
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, ints=(100, 0))
    st = c._states[sprite]
    assert st.curve is None

    _run(c, sprite, st.duration / 2)

    assert abs(sprite.pos.x() - 860.0) <= 5.0   # 目标 920：半程 860


def test_move_curve_multi_loop_reaches_target():
    """多圈 + 曲线：每圈曲线各自跑满，总进度按圈数折算，终点不漂。"""
    curve = _flat_head_curve(24, 0.25)
    lib = _make_library(curves={"walk": curve}, frames={"walk": 24, "idle1": 24,
                                                        "turn1": 12, "click1": 12})
    sprite = _make_sprite(lib, facing="right")
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    c.predict_enabled = False
    _roll_into_move(c, sprite, lib, ints=(100, 0))
    st = c._states[sprite]
    assert st.loops == 2

    _run(c, sprite, st.duration)
    assert c.state_of(sprite) == STATE_IDLE
    assert sprite.pos == QPointF(920, 400)


# ---------------------------------------------------------------- 分类路径
def test_categories_via_build_categories_when_names_available():
    class NamedLibrary(FakeLibrary):
        manifest = None
        folder_map = None
        folder_files = None

        def names(self):
            return ["待机呼吸休闲", "东张西望", "螃蟹走路", "点击回应 - 开心跃动", "悠闲哼歌"]

    lib = NamedLibrary(idles=[], turns=[], moves=[], clicks=[], frames={})
    for name in lib.names():
        lib._clips[name] = FakeClip(name, 24)
    c = BehaviorController(BOUNDS, rng=ScriptedRng())
    cats = c._categories(lib)
    assert cats["idles"] == ["待机呼吸休闲"]
    assert cats["turns"] == ["东张西望"]
    assert cats["moves"] == ["螃蟹走路"]
    assert cats["clicks"] == ["点击回应 - 开心跃动"]


# ---------------------------------------------------------------- V-2：body_box 口径
def test_clamp_into_bounds_respects_body_box(monkeypatch):
    """V-2：兜底钳制口径 = 身体框贴边（画布透明边允许越界）。

    旧口径按整 canvas 矩形钳制，会把按身体框贴边的 sprite 每 tick 拉回
    一个透明边距（可见瞬移）。body_box=(100,60,400,330) × scale 0.5
    → 局部身体框 QRect(50,30,150,135)，画布 320×180。
    """
    from pet import catalog

    monkeypatch.setattr(catalog, "character_body_box", lambda _cid: (100, 60, 400, 330))
    lib = _make_library()
    sprite = _make_sprite(lib, pos=(600, 435))   # 身体右缘 600+50+150=800 贴界、下缘 435+30+135=600 贴界
    bounds = QRect(0, 0, 800, 600)
    c = BehaviorController(bounds, rng=ScriptedRng())

    c._clamp_into_bounds(sprite)

    assert sprite.pos == QPointF(600, 435)       # 合法贴边位不被拉回
    assert sprite.rect().right() > bounds.right()  # 画布透明边允许越界


def test_clamp_into_bounds_body_box_pulls_back_only_overflow(monkeypatch):
    """V-2：身体框真出界时按身体框口径钳回（不多拉）。"""
    from pet import catalog

    monkeypatch.setattr(catalog, "character_body_box", lambda _cid: (100, 60, 400, 330))
    lib = _make_library()
    sprite = _make_sprite(lib, pos=(700, 500))   # 身体右缘 850>800、下缘 665>600：均出界
    bounds = QRect(0, 0, 800, 600)
    c = BehaviorController(bounds, rng=ScriptedRng())

    c._clamp_into_bounds(sprite)

    assert sprite.pos == QPointF(600, 435)       # 钳到身体框贴边即停


# ---------------------------------------------------------------- D9：acts 随机动作池
def _library_with_acts():
    lib = _make_library()
    lib.acts = ["act1", "act2"]
    for name in lib.acts:
        lib._clips[name] = FakeClip(name, 18)
    return lib


def test_acts_bucket_binds_acts_clip():
    lib = _library_with_acts()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.5,)))
    c.predict_enabled = False
    _run(c, sprite, lib.duration("idle1") + 0.1)   # 待机播完掷骰 → 0.5 ∈ acts 桶
    assert c.state_of(sprite) == "acts"
    assert sprite._clip_name in ("act1", "act2")
    # acts 播完回掷骰（默认 roll 0.0 → 回待机）
    _run(c, sprite, lib.duration(sprite._clip_name) + 0.1)
    assert c.state_of(sprite) == "idle"


def test_acts_bucket_falls_back_to_idle_when_pool_empty():
    lib = _make_library()  # 无 acts
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.5,)))
    c.predict_enabled = False
    _run(c, sprite, lib.duration("idle1") + 0.1)
    assert c.state_of(sprite) == "idle"     # acts 空：40% 桶回退待机
    assert lib.clip("idle1").start_count == 2


def test_move_failure_falls_back_to_acts():
    # 窄边界 + 高 roll（>=0.8 移动桶）→ 移动计划失败 → 回退动作池（非待机）
    lib = _library_with_acts()
    sprite = _make_sprite(lib, pos=(20, 400))
    c = BehaviorController(QRect(0, 0, 400, 1000), rng=ScriptedRng(rolls=(0.99,)))
    _roll_into_move(c, sprite, lib, rolls=())
    assert c.state_of(sprite) == "acts"
    assert sprite._clip_name in ("act1", "act2")
    assert sprite.velocity == QPointF(0, 0)


# ---------------------------------------------------------------- 批10-A1：预测式预热
def test_prediction_made_in_lead_and_consumed():
    """提前量内创建预测 → 播完消费（context/gen 校验通过）。"""
    lib = _library_with_acts()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.5,)))
    # 待机起播后推进到提前量内（duration 1.008 - elapsed ≥ 0.658）
    _run(c, sprite, 0.75)
    st = c._states[sprite]
    assert st.predictor.counts["made"] == 1        # 已创建预测
    _run(c, sprite, 0.5)                            # 播完 → 消费
    assert st.predictor.counts["hit"] == 1
    assert c.state_of(sprite) == "acts"            # 0.5 ∈ acts 桶（预测产物）


def test_prediction_invalidated_by_intervening_bind():
    """点击打断（换代+换 context）→ 预测作废，现场掷骰。"""
    lib = _library_with_acts()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.5, 0.0)))
    _run(c, sprite, 0.75)
    st = c._states[sprite]
    assert st.predictor.counts["made"] == 1
    assert c.on_sprite_clicked(sprite) is True     # 点击绑 click1（换代）
    _run(c, sprite, lib.duration("click1") + 0.1)  # click 播完回 idle
    _run(c, sprite, lib.duration("idle1") + 0.1)   # idle 播完掷骰：预测已失效
    assert st.predictor.counts["hit"] == 0
    assert st.predictor.counts["miss_invalid"] >= 1


def test_predict_disabled_falls_back_to_live_roll():
    lib = _library_with_acts()
    sprite = _make_sprite(lib)
    c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.5,)))
    c.predict_enabled = False
    _run(c, sprite, lib.duration("idle1") + 0.1)
    st = c._states[sprite]
    assert st.predictor.counts["made"] == 0
    assert c.state_of(sprite) == "acts"
