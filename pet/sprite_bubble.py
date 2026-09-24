# -*- coding: utf-8 -*-
"""sprite 气泡跟随（4.1c）：真实 PetSpeechBubble 独立小窗跟随 sprite。

自 demo（.scratch/single-overlay-window/run_overlay_demo.py Phase 3b）
迁入 pet/ 的产品化版本：跟随源 = overlay.add_position_listener（sprite
不产生 moveEvent）；锚点 = sprite 身体框换算全局（V-2 口径——整画布
rect 会把气泡锚到透明边上沿）；30Hz 跟随节流 + 尾部补发（V-5：tick 级
fanout 游走期每 tick 一次 SetWindowPos ≈1.5ms 的 WM 移动税不能再交）。

构造/跟随/显隐全链路 try/except 静默降级——气泡是外围装饰，失败绝不
崩主链路。

4.3 后半（本刀）：气泡主体可点 → 快速对话入口。对齐旧路径
``window.py:3419-3447`` 的 ``_on_speech_bubble_clicked`` 语义——只有普通
无按钮气泡可点开对话栏，交互（buttons）/告警气泡的主体点击是 no-op。
点击能力复用既有 ``PetSpeechBubble`` 公开面（``clicked`` 信号 +
``set_interactive`` + ``show_text``），不新造控件、不复制气泡实现。

自言自语配图（本刀）：``show_image`` 补齐 ``PetSpeechBubble.show_image``
的锚点/缩放面——旧架构唯一宿主 PetWindow 在 overlay 拓扑下不构造，没有
这一步配图自言自语（``self_talk_image_chance``）在 sprite 世界无路可走。
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QPoint, QRect, QTimer

from .speech_bubble import PetSpeechBubble

logger = logging.getLogger(__name__)

_FOLLOW_INTERVAL = 1.0 / 30.0  # 30Hz 跟随上限


def sprite_anchor_rect_global(sprite, overlay_origin: QPoint) -> QRect:
    """气泡锚点：sprite 身体框（overlay 局部）换算到全局屏幕坐标。

    口径平移自 window_placement.bubble_anchor_rect（旧架构锚点是"host 的
    可见内容矩形"）；sprite 侧的同口径是 body_rect()（body_box×scale；
    未声明 body_box 回退全画布 rect()），直接加 overlay 原点即得全局
    锚点（overlay 铺满屏幕、自身不移动，原点即屏幕全局原点）。
    """
    rect = sprite.body_rect() if hasattr(sprite, "body_rect") else sprite.rect()
    return QRect(overlay_origin + rect.topLeft(), rect.size())


class SpriteBubbleFollower:
    """一只 sprite 的气泡跟随（真实 PetSpeechBubble 独立小窗）。

    ``on_clicked(sprite)``：气泡主体点击回调（快速对话入口）；不给 = 气泡
    保持全鼠标穿透（无聊天变体/未接线，静默降级）。
    """

    def __init__(self, overlay, sprite, *, style_id: str = "classic_top",
                 on_clicked=None) -> None:
        self._overlay = overlay
        self._sprite = sprite
        self._origin = overlay.geometry().topLeft()
        self.on_clicked = on_clicked
        self.bubble: PetSpeechBubble | None = None
        self._last_follow = 0.0
        self._follow_pending = False
        self._follow_timer = QTimer(overlay)
        self._follow_timer.setSingleShot(True)
        self._follow_timer.setInterval(int(_FOLLOW_INTERVAL * 1000))
        self._follow_timer.timeout.connect(self._flush_follow)
        try:
            self.bubble = PetSpeechBubble(style_id=style_id)
            self.bubble.clicked.connect(self._on_bubble_clicked)
            overlay.add_position_listener(sprite, self._on_sprite_moved)
        except Exception:
            self.bubble = None  # 构造失败 = 无气泡，静默降级

    def anchor(self) -> QRect:
        return sprite_anchor_rect_global(self._sprite, self._origin)

    def _on_sprite_moved(self, sprite) -> None:
        if self.bubble is None:
            return
        now = time.monotonic()
        if now - self._last_follow >= _FOLLOW_INTERVAL:
            self._last_follow = now
            self._follow_pending = False
            try:
                self.bubble.reposition(self.anchor())
            except Exception:
                pass
        else:
            self._follow_pending = True
            if not self._follow_timer.isActive():
                self._follow_timer.start()

    def _flush_follow(self) -> None:
        """节流窗口结束后补发最后一帧位置（防气泡停在半途）。"""
        if not self._follow_pending or self.bubble is None:
            return
        self._follow_pending = False
        self._last_follow = time.monotonic()
        try:
            self.bubble.reposition(self.anchor())
        except Exception:
            pass

    def say(self, text: str, duration_ms: int = 3200, *, subtitle: str = "") -> bool:
        """播一句气泡文本；气泡不可用/文案为空返回 False（静默降级）。"""
        return self.show(text, duration_ms, subtitle=subtitle)

    def show(self, text: str, duration_ms: int = 3200, *, subtitle: str = "",
             sticky: bool = False, buttons: list | None = None,
             title_first: bool = False, width_locked: bool = False) -> bool:
        """按 ``PetWindow.show_bubble`` 的形参面呈现一句气泡。

        ``sticky`` / ``buttons`` 原样透传给 ``PetSpeechBubble.show_text``——
        提醒/审批气泡的交互语义与旧路径同源（气泡控件自身负责按钮与穿透
        切换），本层只补锚点与降级。返回是否真的展示。
        """
        text = str(text or "").strip()
        if self.bubble is None or not text:
            return False
        try:
            self.bubble.show_text(
                text, self.anchor(), duration_ms,
                subtitle=str(subtitle or ""),
                sticky=bool(sticky),
                buttons=list(buttons) if buttons else None,
                title_first=bool(title_first),
                width_locked=bool(width_locked),
                pet_scale=getattr(self._sprite, "scale", None))
            return True
        except Exception:
            logger.debug("overlay: 气泡播放失败", exc_info=True)
            return False

    def show_image(self, image_path, duration_ms: int = 3200,
                   image_scale: float = 1.0, pixmap=None) -> bool:
        """播一张配图气泡（配图自言自语；``PetSpeechBubble.show_image`` 形参面）。

        形参对齐 ``speech_bubble.py:960`` 的 ``show_image(path, anchor,
        duration_ms, *, pet_scale, image_scale)``：锚点由跟随器自算（气泡
        锚定 sprite 而非 window 的可见内容矩形），``pet_scale`` 取 sprite
        缩放，故调用方只关心图与显示时长/缩放。图片路径无效、气泡不可用
        或底层抛异常都返回 False——与 ``show`` 同款静默降级，配图失败绝
        不冒泡出异常到自言自语链路。
        """
        if self.bubble is None:
            return False
        try:
            return bool(self.bubble.show_image(
                image_path, self.anchor(), duration_ms,
                pet_scale=getattr(self._sprite, "scale", None),
                image_scale=image_scale, pixmap=pixmap))
        except Exception:
            logger.debug("overlay: 气泡配图播放失败", exc_info=True)
            return False

    def set_interactive(self, on: bool) -> None:
        """气泡是否可点（可点 = 可打开快速对话）。

        不可用时保持 QT 的 ``WA_TransparentForMouseEvents`` 全穿透——气泡
        绝不吞掉桌面上的点击（旧路径 ``_set_speech_bubble_interactive`` 同款）。
        """
        setter = getattr(self.bubble, "set_interactive", None)
        if not callable(setter):
            return
        try:
            setter(bool(on))
        except Exception:
            logger.debug("overlay: 气泡交互态切换失败", exc_info=True)

    def hide(self) -> None:
        """收起当前气泡（气泡不可用时静默）。"""
        hider = getattr(self.bubble, "hide", None)
        if callable(hider):
            try:
                hider()
            except Exception:
                logger.debug("overlay: 气泡收起失败", exc_info=True)

    def _on_bubble_clicked(self) -> None:
        """气泡主体点击 → 快速对话（``window.py:3419`` 语义）。

        交互（按钮）/告警气泡的主体点击必须是 no-op——按钮自身由气泡控件
        处理；以实际展示状态判定，不依赖文案关键词。
        """
        if getattr(self.bubble, "_interactive_active", False):
            return
        callback = self.on_clicked
        if callable(callback):
            callback(self._sprite)

    def set_origin(self, origin: QPoint) -> None:
        """屏迁移后更新全局原点（overlay 重建时由壳层调用）。"""
        self._origin = QPoint(origin)

    def close(self) -> None:
        try:
            self._overlay.remove_position_listener(self._sprite, self._on_sprite_moved)
        except Exception:
            pass
        if self.bubble is not None:
            try:
                self.bubble.close()
            except Exception:
                pass
            self.bubble = None
