# -*- coding: utf-8 -*-
"""SpriteMenuFacade（4.1c）：旧 context_menus 建造器的 pet 形适配面。

context_menus/shared.py 的 build_*/add_* 以 PetWindow 方法面为输入
（scale/change_scale/cfg/switch_clip/trigger_move/playback_speed/
set_playback_speed/set_on_top/set_no_move/set_mouse_through/
set_drag_physics/go_default_corner/hide/request_switch_character/
idles/turns/moves/clicks/acts）。本类把这套面映射到
OverlayShell + 主 sprite + 行为控制器，使旧菜单建造器原样复用——
菜单项的方法实现只写一次（在 facade 上），新旧路径语义一致。

组合（窗口能力子集，对齐 legacy 平面布局的相关段；chat/music/agent/
spawn/黄金回旋/edge_probe 等未接外围不展示——与纯桌宠版隐藏同纪律）：
    动画分类 / 播放速率 / 拖动物理 / 切换角色
    ───
    回到右下角 / 窗口置顶 / 不移动 / 鼠标穿透 / 开机自启 / 隐藏桌宠
    ───
    桌宠设置 / 退出这只（多宠时） / 退出

4.2c D13：菜单作用对象 = 右键命中的那一只（``sprite`` 参数）。设置入口按它
自己的 config 身份传 ``--instance``（子肥鱼 = slot-N），「退出这只」直接接
``OverlayShell.exit_pet(sprite)``——主宠退出时由壳负责提升列表首只子宠。
"""
from __future__ import annotations

from PySide6.QtWidgets import QMenu

from .context_menus.shared import (
    add_autostart,
    add_mouse_through,
    add_no_move,
    add_on_top,
    add_quit,
    add_clear_spawned_pets,
    add_return_corner,
    add_spawn_pet,
    build_animation_categories,
    build_character_menu,
    build_speed_menu,
    add_drag_physics,
    add_hide_pet,
)
from .context_menus.shared import add_action as _add_action


class SpriteMenuFacade:
    """context_menus 建造器的 pet 形适配面（duck-typed，非 PetWindow 子类）。

    ``sprite`` = 本次菜单的作用对象（4.2c D13）：右键命中的是哪一只，菜单里的
    设置入口与「退出这只」就作用在哪一只。不给 = 主 sprite（兼容既有调用面）。
    """

    def __init__(self, shell, sprite=None) -> None:
        self._shell = shell
        self._sprite = sprite

    @property
    def sprite(self):
        """菜单作用对象（默认主 sprite）。"""
        return self._shell.sprite if self._sprite is None else self._sprite

    # ---------------------------------------------------------------- 基础属性
    @property
    def cfg(self):
        return self._shell._config

    @property
    def scale(self) -> float:
        return float(self._shell.sprite.scale)

    @property
    def mouse_through(self) -> bool:
        return bool(self._shell._user_mouse_through)

    @property
    def no_move(self) -> bool:
        return bool(getattr(self._shell.behavior, "no_move", False))

    @property
    def drag_physics(self) -> bool:
        return bool(getattr(self._shell.sprite, "drag_physics", True))

    @property
    def playback_speed(self) -> float:
        return float(getattr(self._shell.sprite, "playback_speed", 1.0))

    # ---------------------------------------------------------------- 素材清单（动画分类建造器）
    def _cats(self) -> dict:
        return self._shell.behavior._categories(self._shell.sprite.library)

    @property
    def idles(self) -> list:
        return self._cats()["idles"]

    @property
    def turns(self) -> list:
        return self._cats()["turns"]

    @property
    def moves(self) -> list:
        return self._cats()["moves"]

    @property
    def clicks(self) -> list:
        return self._cats()["clicks"]

    @property
    def acts(self) -> list:
        return self._cats()["acts"]

    # ---------------------------------------------------------------- 动画/速率
    def switch_clip(self, name: str) -> None:
        """播放指定动画（一次性，播完回掷骰链——window.py switch_clip 语义）。"""
        self._shell.behavior.play_once(self._shell.sprite, name)

    def trigger_move(self, name: str) -> bool:
        """以指定移动素材触发一次移动（window.py trigger_move 语义）。"""
        return self._shell.behavior.play_move_once(self._shell.sprite, name)

    def set_playback_speed(self, value: float) -> None:
        self._shell.sprite.playback_speed = float(value)
        clip = getattr(self._shell.sprite, "_clip", None)
        setter = getattr(clip, "set_playback_speed", None)
        if callable(setter):
            setter(float(value))

    # ---------------------------------------------------------------- 窗口能力
    def change_scale(self, scale: float) -> None:
        self._shell.sprite.scale = float(scale)
        self.cfg.set("scale", float(scale))
        save = getattr(self.cfg, "save", None)
        if callable(save):
            save()

    def set_drag_physics(self, on: bool) -> None:
        self._shell.sprite.drag_physics = bool(on)
        self.cfg.set("drag_physics", bool(on))
        save = getattr(self.cfg, "save", None)
        if callable(save):
            save()

    def set_no_move(self, on: bool) -> None:
        self._shell.behavior.no_move = bool(on)
        self.cfg.set("no_move", bool(on))
        save = getattr(self.cfg, "save", None)
        if callable(save):
            save()

    def set_mouse_through(self, on: bool) -> None:
        self._shell.overlay.set_mouse_through(bool(on))

    def set_on_top(self, on: bool) -> None:
        self._shell.set_on_top(bool(on))

    def go_default_corner(self) -> None:
        shell = self._shell
        shell.sprite.set_pos(shell._default_corner_pos(
            shell._bounds, shell.sprite.rect()))

    def hide(self) -> None:
        self._shell.set_pet_visible(False)

    # ---------------------------------------------------------------- 角色/设置/退出
    def request_switch_character(self, character_id: str) -> None:
        self._shell.switch_character(str(character_id))

    def on_spawn_pet(self) -> None:
        self._shell.spawn_pet()

    def on_clear_spawned_pets(self) -> None:
        self._shell.clear_spawned_pets()

    def on_open_settings(self) -> None:
        """打开设置页（D13：按被点 sprite 的 config 身份传给独立设置进程）。"""
        self._shell.open_settings_for(self.sprite)

    def on_exit_pet(self) -> None:
        """「退出这只」：退掉菜单作用对象（主宠退出则提升列表首只子宠为主）。"""
        self._shell.exit_pet(self.sprite)

    def close(self) -> None:
        self._shell.app.quit()

    def request_quit(self) -> None:  # add_quit 的调用面
        self.close()


def build_sprite_full_menu(shell, sprite=None) -> QMenu:
    """overlay 全量右键菜单（窗口能力子集，组合对齐 legacy 布局相关段）。

    ``sprite`` = 被点中的那一条（4.2c D13 由 ``ShellOverlayWindow.contextMenuEvent``
    透传）；动作面里作用于"某一只"的入口（桌宠设置 / 退出这只）按它路由。
    """
    facade = SpriteMenuFacade(shell, sprite)
    menu = QMenu()
    build_animation_categories(menu, facade, icons=False)
    build_speed_menu(menu, facade, icons=False)
    add_drag_physics(menu, facade, icons=False)
    build_character_menu(menu, facade, icons=False)
    menu.addSeparator()
    add_return_corner(menu, facade, icons=False)
    add_on_top(menu, facade, icons=False)
    add_no_move(menu, facade, icons=False)
    add_mouse_through(menu, facade, icons=False)
    add_autostart(menu, facade, icons=False)
    add_spawn_pet(menu, facade)
    add_clear_spawned_pets(menu, facade, icons=False)
    add_hide_pet(menu, facade, icons=False)
    menu.addSeparator()
    _add_action(menu, "桌宠设置", None, facade.on_open_settings,
                close_on_trigger=True)
    # 「退出这只」只在多宠时出现（legacy parity：单窗 flag 关时不注入该入口，
    # 只有 app.quit 语义的「退出」）——没有子肥鱼时它等价于「退出」，不重复列出
    if getattr(shell, "_spawned", None):
        _add_action(menu, "退出这只", None, facade.on_exit_pet,
                    close_on_trigger=True)
    add_quit(menu, facade, icons=False)
    # F5 教训：facade 与菜单同寿命（wrapper 回收后回调命中失效引用）
    menu._facade = facade
    return menu
