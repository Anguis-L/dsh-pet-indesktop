# -*- coding: utf-8 -*-
"""config → sprite/behavior 同步点回归（DS 全量审查 M6/M7/M9/M10）。

M6：drag_physics/throw_strength/no_move/playback_speed 此前启动不读 config、
refresh 不覆盖、sprite 默认值与 config 默认相反（drag_physics=True vs False、
throw_speed_cap=6000 vs standard=4800）——开箱行为不一致 + 重启即丢。
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

import tests.test_overlay_window_capabilities as cap
import tests.test_sprite_menu_facade as fac
from pet.overlay_shell import OverlayShell
from pet.pet_sprite import PetSprite
from pet.physics import THROW_STRENGTH_CAPS

app = QApplication.instance() or QApplication([])


def _make_shell(tmp_path, values=None):
    config = cap.CapConfig(tmp_path, values or {})
    instance = cap.CapInstance(config)
    screen = cap.FakeScreen((0, 0, 1920, 1080), (0, 0, 1920, 1040))
    shell = OverlayShell(
        app, instance, screen=screen,
        sprite_factory=lambda lib, pos, scale: PetSprite(lib, pos=pos, scale=scale))
    lib = fac.RichLibrary()
    shell.lib = lib
    shell.sprite.library = lib
    return shell, config


def test_config_syncs_to_sprite_at_build(tmp_path):
    """启动即按 config 应用：默认值不再相反，四键全部生效。"""
    shell, _config = _make_shell(tmp_path, {
        "drag_physics": False,
        "throw_strength": "gentle",
        "no_move": True,
        "playback_speed": 1.5,
    })
    try:
        sprite = shell.sprite
        assert sprite.drag_physics is False            # 旧默认 True，与 config 相反（M6）
        assert sprite.throw_speed_cap == THROW_STRENGTH_CAPS["gentle"]
        assert sprite.playback_speed == 1.5
        assert shell.behavior.no_move is True
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_refresh_settings_resyncs_sprite(tmp_path):
    """设置页改完经 refresh_settings 即生效（不重启）。"""
    shell, config = _make_shell(tmp_path, {"drag_physics": False})
    try:
        config.set("drag_physics", True)
        config.set("throw_strength", "crazy")
        config.set("playback_speed", 0.5)
        shell.refresh_settings()
        assert shell.sprite.drag_physics is True
        assert shell.sprite.throw_speed_cap == THROW_STRENGTH_CAPS["crazy"]
        assert shell.sprite.playback_speed == 0.5
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_spawned_sprite_gets_config_sync(tmp_path):
    """spawn 的子肥鱼同样按 config 应用（不是只吃构造默认）。"""
    shell, _config = _make_shell(tmp_path, {"drag_physics": False,
                                            "throw_strength": "strong"})
    try:
        shell._spawn_slot(7)
        spawned = shell._spawned[-1]
        assert spawned.drag_physics is False
        assert spawned.throw_speed_cap == THROW_STRENGTH_CAPS["strong"]
        lib = shell._spawned_libs.pop(spawned)
        shutdown = getattr(lib, "shutdown", None)
        if callable(shutdown):
            shutdown()
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()


def test_stop_halts_self_talk_timer(tmp_path):
    """M9：shell.stop() 后 self_talk 定时器不再自我重排。"""
    shell, _config = _make_shell(tmp_path, {
        "self_talk_enabled": True,
        "self_talk_texts": ["台词"],
        "self_talk_min_interval": 5.0,
        "self_talk_max_interval": 5.0,
    })
    try:
        shell.overlay.show()
        assert shell._self_talk_timer.isActive() is True
        shell.start()
        shell.stop()
        assert shell._self_talk_timer.isActive() is False
        shell._delete_runtime_marker()
    finally:
        timer = getattr(shell, "_self_talk_timer", None)
        if timer is not None:
            timer.stop()
        shell._delete_runtime_marker()


def test_settle_supported_resets_flight_playback_speed():
    """M7：落岛支撑落定与抛掷落地共用速率复位语义（duration 失真源）。

    真世界最小落定场景：thrown sprite 低速贴岛，连 tick 到支撑落定阈值
    → 收尾必须调 reset_playback_speed（否则飞行倍率除进 duration()，
    后续 _plan_move 量化失配——sprite_physics 落地分支不是唯一出口）。
    """
    from pet.sprite_collision import SpriteCollisionWorld
    from tests.test_island_bridge import FakeSprite

    world = SpriteCollisionWorld()
    world.add_static_member("island", 300.0, 140.0, 200.0, 44.0)
    sprite = FakeSprite(370.0, 100.0)          # 60×60 底部 160 贴岛顶 140（内切圆相交）
    sprite.interaction_state = "thrown"
    sprite.set_velocity(type(sprite.velocity)(0.0, 0.0))
    resets: list = []
    sprite.reset_playback_speed = lambda: resets.append(1)

    for _ in range(world.support_settle_ticks):
        world.tick([sprite], 1 / 60)

    assert sprite.interaction_state == "normal"
    assert resets, "支撑落定收尾未复位飞行播放速率（M7）"


def test_island_hit_below_300_still_squashes(tmp_path):
    """GPT 审查阻断：岛撞 60-300 是合法真撞击（静态阈值 60），壳层不得再按
    hit_min_dv=300 二次过滤——低速撞岛的挤压/反馈链不再被截断。"""
    import types

    shell, _config = _make_shell(tmp_path, {})
    try:
        sprite = shell.sprite
        squashed: list = []
        sprite.squash = lambda: squashed.append(1)
        cid = sprite.collision_id
        event = types.SimpleNamespace(a="island", b=cid, j=100.0)  # 60<100<300
        shell._on_collision_squash(event)
        assert squashed == [1]
        shell._delete_runtime_marker()
    finally:
        shell._delete_runtime_marker()
