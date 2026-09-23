# -*- coding: utf-8 -*-
"""sprite 音效（4.1c）：点击音效 + 碰撞音效的产品化播放器。

音源解析与旧架构同一入口（click_sound.resolve_click_sound_candidates：
用户自定义 click.wav 优先，内置 default 包兜底；配置键
click_sound_enabled / click_sound_pack / click_sound_volume 与设置页一致）。
播放走 click_sound.play_sound（Windows 下 wav 直放 winmm，不拉起
QtMultimedia）。

- 点击音效：单击 sprite（点击/拖拽阈值判定路由）→ 按 click_sound_volume
  播放，80ms 节流防抖；
- 碰撞音效：collision event.j >= world.hit_min_dv（真撞击量级）→ 重档
  （j >= 2×hit_min_dv）0.9 / 轻档 0.45，80ms 节流（碰碰车一次接触连发
  多个事件）。

音效失败（无音频设备/后端错误/解析失败）一律静默降级，绝不影响主链路。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import click_sound

_THROTTLE_S = 0.08
_LIGHT_VOLUME = 0.45
_HEAVY_VOLUME = 0.9
_DEFAULT_PACK = {"kind": "builtin", "id": "default"}


class SpriteSoundPlayer:
    """点击/碰撞音效统一播放器（duck-typed config：get/dir 即可）。"""

    def __init__(self, config, world=None, *, clock=time.monotonic) -> None:
        self._config = config
        self._world = world
        self._clock = clock
        self._last_play = 0.0
        self._path: Path | None | bool = None  # None=未解析；False=解析过无音源

    # ---------------------------------------------------------------- 内部
    def _resolve_path(self) -> Path | None:
        if self._path is not None:
            return self._path or None
        try:
            if not bool(self._config.get("click_sound_enabled", True)):
                self._path = False
                return None
            pack = self._config.get("click_sound_pack", None) or _DEFAULT_PACK
            data_dir = getattr(self._config, "dir", None)
            candidates = click_sound.resolve_click_sound_candidates(pack, data_dir)
            self._path = candidates[0] if candidates else False
        except Exception:
            self._path = False  # 解析失败 = 无音效，静默降级
        return self._path or None

    def _play(self, volume: float) -> None:
        path = self._resolve_path()
        if path is None:
            return
        now = self._clock()
        if now - self._last_play < _THROTTLE_S:
            return
        self._last_play = now
        self._submit(path, volume)

    def _submit(self, path: Path, volume: float) -> None:
        """提交播放：GUI 场景推迟到事件循环下一轮——play_sound 是同步阻塞
        （winmm 首播 ~157ms / 其后 ~62ms，4.3 岛桥刀实测），在碰撞 listener
        里直调会把该 tick 拖到 ~60ms（碰碰车场景每撞必卡）；推迟后位置交付
        当帧完成、音效下一轮回放（+<16ms 无感）。无 QApplication（纯脚本/
        测试直调）退化为同步直放。"""
        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            if QApplication.instance() is not None:
                QTimer.singleShot(0, lambda: self._safe_play(path, volume))
                return
        except Exception:
            pass
        self._safe_play(path, volume)

    def _safe_play(self, path: Path, volume: float) -> None:
        try:
            click_sound.play_sound(path, volume)
        except Exception:
            pass  # 无音频设备/后端失败：静默降级

    # ---------------------------------------------------------------- 事件入口
    def on_click(self) -> None:
        """单击音效：音量 = click_sound_volume（默认 0.70，同设置页）。"""
        volume = float(self._config.get("click_sound_volume", 0.70) or 0.70)
        self._play(max(0.0, min(1.0, volume)))

    def on_collision(self, event) -> None:
        """碰撞音效：j 闸门 + 轻重两档（demo 碰碰车实测口径）。"""
        world = self._world
        min_j = float(getattr(world, "hit_min_dv", 0.0) or 0.0)
        if getattr(event, "j", 0.0) < min_j:
            return
        heavy = bool(world is not None and event.j >= min_j * 2.0)
        self._play(_HEAVY_VOLUME if heavy else _LIGHT_VOLUME)
