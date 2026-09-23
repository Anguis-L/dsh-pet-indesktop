# -*- coding: utf-8 -*-
"""Phase 4.1a pet_sprite 硬化 offscreen 单测（PHASE4_DESIGN.md D2/D3）。

D2 DPR 归一化：pixmap 按 CANVAS*scale*dpr 物理像素生成并携带
setDevicePixelRatio；rect/pos/alpha_at 输入恒为逻辑坐标。
D3 位置出口收口 + body_box 钳制：set_pos 是唯一写入口，身体框必须完整
落在 bounds 内（sprite 外接矩形允许溢出贴屏）；velocity 积分与拖拽
on_move 同样被钳。

造假方式对齐 tests/test_overlay_window.py：纯 QImage 假 clip/假 library，
body_box 取数经 monkeypatch 替换 catalog.character_body_box（不依赖真实
角色素材包）。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from pet import catalog
from pet.pet_sprite import PetSprite

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- 假 clip / 假 library
class FakeClip(QObject):
    """接口对齐 WebMClip 的假 clip：frameChanged 信号 + 固定 QImage 帧。"""

    frameChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self.frame = 0
        self.image = QImage(640, 360, QImage.Format.Format_ARGB32)
        self.image.fill(Qt.GlobalColor.transparent)

    def currentFrameNumber(self):
        return self.frame

    def currentImage(self):
        return self.image

    def frameCount(self):
        return 1

    def start(self):
        return True

    def stop(self):
        pass


class FakeLibrary:
    def __init__(self, clip, character_id="fake_char"):
        self._clip = clip
        self.no_mirror: set[str] = set()
        self.character_id = character_id

    def movie(self, name):
        return self._clip


class CountingPetSprite(PetSprite):
    """统计帧重建次数的 PetSprite（验证 dpr 变化触发重建）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.build_count = 0

    def _rebuild_pixmap(self):
        rebuilt = super()._rebuild_pixmap()
        if rebuilt:
            self.build_count += 1
        return rebuilt


def _half_opaque_clip():
    """左半不透明、右半全透明的 640×360 假帧。"""
    clip = FakeClip()
    for x in range(320):
        for y in range(360):
            clip.image.setPixel(x, y, 0xFF112233)
    return clip


# ---------------------------------------------------------------- D2：DPR 归一化
def test_dpr_physical_pixmap_and_logical_rect():
    clip = _half_opaque_clip()
    sprite = PetSprite(FakeLibrary(clip), pos=QPointF(100, 100), scale=0.5)
    sprite.set_dpr(1.5)
    sprite.bind_clip("fake")
    sprite._rebuild_pixmap()

    pm = sprite._pixmap
    assert pm is not None
    # 物理像素 = CANVAS*scale*dpr；pixmap 携带 dpr
    assert pm.width() == round(catalog.CANVAS_W * 0.5 * 1.5)   # 480
    assert pm.height() == round(catalog.CANVAS_H * 0.5 * 1.5)  # 270
    assert pm.devicePixelRatio() == 1.5
    # rect() 恒为逻辑坐标（CANVAS*scale），不随 dpr 变
    assert sprite.rect() == QRect(100, 100, 320, 180)


def test_dpr_logical_alpha_hit():
    clip = _half_opaque_clip()
    sprite = PetSprite(FakeLibrary(clip), pos=QPointF(0, 0), scale=0.5)
    sprite.set_dpr(1.5)
    sprite.bind_clip("fake")
    sprite._rebuild_pixmap()

    w = sprite.rect().width()          # 逻辑宽 320（命中图物理 480）
    mid_y = sprite.rect().height() // 2
    # alpha_at 输入仍是逻辑坐标：左半命中、右半穿透（内部 ×dpr 命中物理图）
    assert sprite.alpha_at(QPoint(5, mid_y)) > 0
    assert sprite.alpha_at(QPoint(w - 5, mid_y)) == 0

    sprite.facing = "right"            # 不在 no_mirror：镜像
    sprite._rebuild_pixmap()
    assert sprite.alpha_at(QPoint(5, mid_y)) == 0
    assert sprite.alpha_at(QPoint(w - 5, mid_y)) > 0


def test_dpr_change_triggers_rebuild():
    clip = _half_opaque_clip()
    sprite = CountingPetSprite(FakeLibrary(clip), scale=0.5)
    sprite.bind_clip("fake")

    sprite._rebuild_pixmap()
    sprite._rebuild_pixmap()
    assert sprite.build_count == 1                     # 签名不变：不重建
    assert sprite._pixmap.devicePixelRatio() == 1.0

    sprite.set_dpr(1.5)                                # dpr 进签名：重建
    sprite._rebuild_pixmap()
    assert sprite.build_count == 2
    assert sprite._pixmap.devicePixelRatio() == 1.5
    assert sprite._pixmap.width() == round(catalog.CANVAS_W * 0.5 * 1.5)

    sprite.set_dpr(1.5)                                # 同值：不再重建
    sprite._rebuild_pixmap()
    assert sprite.build_count == 2

    sprite.set_dpr(1.0)                                # 降回：再重建
    sprite._rebuild_pixmap()
    assert sprite.build_count == 3
    assert sprite._pixmap.width() == round(catalog.CANVAS_W * 0.5)


def test_set_dpr_rejects_non_positive():
    sprite = PetSprite(FakeLibrary(FakeClip()), scale=0.5)
    with pytest.raises(ValueError):
        sprite.set_dpr(0)
    with pytest.raises(ValueError):
        sprite.set_dpr(-1.5)


# ---------------------------------------------------------------- D3：body_box 钳制
# 源像素身体框（640×360 画布），scale=0.5 → 局部逻辑 QRect(50, 30, 150, 135)
BODY_BOX = (100, 60, 400, 330)


@pytest.fixture
def body_box(monkeypatch):
    monkeypatch.setattr(catalog, "character_body_box",
                        lambda _cid: BODY_BOX)
    return BODY_BOX


@pytest.fixture
def no_body_box(monkeypatch):
    monkeypatch.setattr(catalog, "character_body_box",
                        lambda _cid: None)
    return None


def _clamped_sprite(body_box, bounds=QRect(0, 0, 800, 600)):
    sprite = PetSprite(FakeLibrary(FakeClip()), scale=0.5)
    sprite.set_bounds(bounds)
    return sprite


def test_body_box_clamp_right_edge_overflow_allowed(body_box):
    sprite = _clamped_sprite(body_box)
    sprite.set_pos(QPointF(900, 435))
    # 身体右缘贴 bounds.right：pos.x = 800 - 50 - 150 = 600
    assert sprite.pos == QPointF(600, 435)
    body = sprite._body_local_rect()
    body_right = sprite.pos.x() + body.x() + body.width() - 1
    assert body_right == sprite.bounds.toRect().right()
    # sprite 外接矩形（画布透明边）允许溢出 bounds —— 贴边语义
    assert sprite.rect().right() > sprite.bounds.toRect().right()


def test_body_box_clamp_left_top(body_box):
    sprite = _clamped_sprite(body_box)
    sprite.set_pos(QPointF(-500, -500))
    # 身体左上贴 bounds 左上：pos = (0-50, 0-30)；画布左上角可越界
    assert sprite.pos == QPointF(-50, -30)
    body = sprite._body_local_rect()
    assert sprite.pos.x() + body.x() == 0
    assert sprite.pos.y() + body.y() == 0
    assert sprite.rect().left() < 0


def test_body_box_clamp_bottom(body_box):
    sprite = _clamped_sprite(body_box)
    sprite.set_pos(QPointF(100, 900))
    # 身体底缘贴 bounds.bottom：pos.y = 600 - 30 - 135 = 435
    assert sprite.pos == QPointF(100, 435)
    assert sprite.rect().bottom() > sprite.bounds.toRect().bottom()


def test_set_bounds_reclamps_current_pos(body_box):
    sprite = PetSprite(FakeLibrary(FakeClip()), scale=0.5, pos=QPointF(900, 900))
    assert sprite.pos == QPointF(900, 900)             # 无 bounds：不钳
    sprite.set_bounds(QRect(0, 0, 800, 600))           # 设域立即补钳
    assert sprite.pos == QPointF(600, 435)


def test_no_bounds_no_clamp(body_box):
    sprite = PetSprite(FakeLibrary(FakeClip()), scale=0.5)
    sprite.set_pos(QPointF(1234, 567))
    assert sprite.pos == QPointF(1234, 567)
    sprite.set_bounds(QRect(0, 0, 800, 600))
    assert sprite.pos != QPointF(1234, 567)            # 被钳
    sprite.set_bounds(None)                            # 解域后恢复自由
    sprite.set_pos(QPointF(1234, 567))
    assert sprite.pos == QPointF(1234, 567)


def test_no_body_box_falls_back_to_full_rect_clamp(no_body_box):
    sprite = PetSprite(FakeLibrary(FakeClip()), scale=0.5)
    sprite.set_bounds(QRect(0, 0, 800, 600))
    # 未声明 body_box → 回退全画布 = 整矩形钳制（sprite 不得溢出）
    sprite.set_pos(QPointF(900, 900))
    assert sprite.pos == QPointF(800 - 320, 600 - 180)
    assert sprite.rect().right() == sprite.bounds.toRect().right()
    sprite.set_pos(QPointF(-100, -100))
    assert sprite.pos == QPointF(0, 0)


def test_velocity_integration_is_clamped(body_box):
    sprite = _clamped_sprite(body_box)
    sprite.set_pos(QPointF(500, 435))
    sprite.set_velocity(QPointF(10000, 0))             # 高速推出右界
    sprite.advance(1.0)
    # 积分走 set_pos：身体停在界内，不冲出
    assert sprite.pos == QPointF(600, 435)
    body = sprite._body_local_rect()
    assert sprite.pos.x() + body.x() + body.width() <= 800


def test_drag_on_move_is_clamped(body_box):
    sprite = _clamped_sprite(body_box)
    sprite.set_pos(QPointF(100, 100))
    sprite._clock = lambda: 1000.0                     # 固定假钟（时序纪律）
    sprite.on_press(QPointF(110, 110))                 # grab 偏移 (10, 10)
    sprite.on_move(QPointF(5000, 5000))                # 拖出界
    # on_move 走 set_pos：光标 - 偏移后的请求位置被钳回
    assert sprite.pos == QPointF(600, 435)


def test_clamp_when_body_wider_than_bounds(body_box):
    # 可用区比身体还窄：钳到下界（同 clamp_span 小屏兜底），不得 min/max 打架
    sprite = PetSprite(FakeLibrary(FakeClip()), scale=0.5)
    sprite.set_bounds(QRect(0, 0, 100, 600))
    sprite.set_pos(QPointF(50, 100))
    assert sprite.pos == QPointF(-50, 100)             # 身体左缘贴 bounds.left


def test_home_screen_placeholder():
    sprite = PetSprite(FakeLibrary(FakeClip()), scale=0.5)
    assert sprite.home_screen is None                  # T2 多屏占位字段
