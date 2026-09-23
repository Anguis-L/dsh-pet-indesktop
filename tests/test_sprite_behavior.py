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

from pet.pet_sprite import INTERACTION_DRAG, INTERACTION_THROWN, PetSprite
from pet.sprite_behavior import (
    STATE_CLICK,
    STATE_IDLE,
    STATE_MOVE,
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

    def __init__(self, name, frames=24):
        super().__init__()
        self.name = name
        self._frames = frames
        self.image = QImage(640, 360, QImage.Format.Format_ARGB32)
        self.image.fill(0xFF336699)
        self.frame = 0
        self.started = False
        self.start_count = 0

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
        return True

    def stop(self):
        self.started = False


class FakeLibrary:
    """轻量库协议：直接暴露 idles/turns/moves/clicks 池属性（无 names()）。"""

    def __init__(self, *, idles, turns, moves, clicks, frames=None, strides=None):
        self.idles = list(idles)
        self.turns = list(turns)
        self.moves = list(moves)
        self.clicks = list(clicks)
        frames = frames or {}
        self._clips = {}
        for name in self.idles + self.turns + self.moves + self.clicks:
            self._clips[name] = FakeClip(name, frames.get(name, 24))
        self.move_strides = dict(strides or {})
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
def test_non_normal_sprites_are_not_driven():
    for state in (INTERACTION_DRAG, INTERACTION_THROWN):
        lib = _make_library()
        sprite = _make_sprite(lib)
        sprite.interaction_state = state
        sprite.set_velocity(QPointF(50, 0))
        c = BehaviorController(BOUNDS, rng=ScriptedRng(rolls=(0.99,)))
        _run(c, sprite, 3.0)
        assert c.state_of(sprite) is None           # 从未接管
        assert lib.clip("idle1").start_count == 0   # 未绑定任何 clip
        assert sprite.velocity == QPointF(50, 0)    # velocity 不被改写
        assert c.on_sprite_clicked(sprite) is False
        assert lib.clip("click1").start_count == 0


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
