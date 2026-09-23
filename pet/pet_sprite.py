# -*- coding: utf-8 -*-
"""宠物 sprite（单合成窗架构 Phase 1a）：overlay 窗口内的可绘制/可命中单元。

架构背景见 .scratch/single-overlay-window/spec.md：宠物不再是顶层窗口，
而是 overlay 内按 z-order 合成的 sprite；移动 = 改 pos + 上报脏矩形，
不再经过 WM/DWM 的窗口移动路径。

镜像与帧缓存语义与 pet/window.py 一致（facing=='right' 且 clip 不在
library.no_mirror 时镜像；帧签名不变则复用已缩放的 QPixmap），但代码
独立——本模块不 import window.py，避免与旧窗口路径耦合。

Phase 4.1a 硬化（PHASE4_DESIGN.md D2/D3）：
- D2 DPR 归一化：pos/rect/命中全部留在 overlay 局部逻辑坐标系；
  只有渲染像素随 DPR——pixmap 按 CANVAS*scale*dpr 物理像素生成并
  setDevicePixelRatio(dpr)（参照 window.py:2129-2150 旧路径）。
- D3 位置出口收口：set_pos 是唯一位置写入口，按 manifest body_box
  钳制（身体框必须完整落在 bounds 内，画布透明边允许越界贴屏），
  对齐 window_placement.py:35-53 "让角色形象真正碰到边缘"的语义。
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap

from . import catalog
from . import physics as physics_mod
from .library import MovieLibrary, clip_current_image

# 交互状态（跨模块协调协议，见 .scratch/single-overlay-window/spec.md）：
# "normal" = 行为状态机驱动（游荡/待机/转向）；"drag" = 被用户拖拽；
# "thrown" = 抛掷物理接管。行为控制器只在 "normal" 下写 velocity；
# 碰撞世界只在双方非 "drag" 时结算；物理控制器只在 "thrown" 下积分。
INTERACTION_NORMAL = "normal"
INTERACTION_DRAG = "drag"
INTERACTION_THROWN = "thrown"

# Q 弹挤压时长（口径源 window.py:665 _squash_duration_ms）
SQUASH_DURATION_MS = 220


class PetSprite(QObject):
    """单个宠物 sprite：位置/朝向/缩放 + 当前 clip 帧的合成、命中与拖拽。"""

    def __init__(
        self,
        library: MovieLibrary,
        *,
        pos: QPointF | None = None,
        facing: str = "left",
        scale: float = catalog.DEFAULT_SCALE,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.library = library
        self.velocity = QPointF(0, 0)
        self.facing = facing
        self._scale = float(scale)  # scale 为 property（V-10）：直写内部字段
        # D2：渲染 DPR（物理像素 = CANVAS*scale*dpr；逻辑几何不随它变）。
        # 由 overlay 按所在屏 QScreen.devicePixelRatio() 写入。
        self._dpr = 1.0
        # D3：钳制域（overlay 局部逻辑坐标的工作区，None = 不钳制）。
        self._bounds: QRectF | None = None
        # T2 多屏占位：sprite 的归属屏（多屏钳制用，本刀只留字段）。
        self.home_screen = None
        # pos 为 overlay 局部逻辑坐标（Phase 1a 单屏：overlay 铺满主屏，
        # 局部坐标 = 屏幕逻辑坐标）；多屏几何见 spec 开放问题 Q1。
        # 位置变化由外部行为（wander 速度/抛掷物理）写 velocity 或 set_pos；
        # advance 只负责按 velocity 积分，不知道速度从哪来。一切位置写入
        # 收口 set_pos（body_box 钳制的唯一执行点）。
        # 脏上报（V-3/V-4）：overlay 在 add_sprite 时挂 _dirty_cb(old, new)，
        # set_pos 与帧到达直驱重绘，tick 不再是重绘闸门；_last_reported_rect
        # 记"上次上报给 overlay 的 rect"，advance 据此捕获 tick 之外
        # （物理控制器/拖拽事件）发生的位移。
        self._dirty_cb = None
        self._kinetic_cb = None  # M-1：运动信号回调（overlay 挂接）
        self._last_reported_rect: QRect | None = None
        self.pos = QPointF(0, 0)
        self.set_pos(pos if pos is not None else QPointF(0, 0))
        self._clip = None
        self._clip_name: str | None = None
        self._frame_sig: tuple | None = None
        self._frame_dirty = False
        self._pixmap: QPixmap | None = None
        self._hit_image: QImage | None = None
        self._dragging = False
        self._drag_offset: QPointF | None = None
        # 拖拽轨迹（(monotonic_ts, x, y) 光标样本）：松手初速估算的输入，
        # 格式对齐 physics.estimate_release_velocity；松手后清空
        self._drag_trail: list[tuple[float, float, float]] = []
        # 时间源：默认 time.monotonic；测试替换为假钟即可确定性构造轨迹，
        # 不用固定 sleep 赌时序
        self._clock = time.monotonic
        # 甩出速度软上限（px/s，对应旧架构 throw_strength 档位）：由集成层
        # 按设置写入，松手初速过 physics.soft_clamp_speed 时以此为渐近值
        self.throw_speed_cap = physics_mod.MAX_THROW_SPEED
        # 播放速率（菜单「播放速率」写入口）：bind_clip 起播时应用到新 clip
        self.playback_speed = 1.0
        # Q 弹挤压（点击/碰撞反馈，window.py _squash_geometry 语义）：
        # None = 未激活；否则 0..1 进度，advance 按 220ms 推进
        self._squash_progress: float | None = None
        # 拖动物理开关（菜单「拖动物理」）：False 时松手原地放下（不抛掷）
        self.drag_physics = True
        # 见模块顶部 INTERACTION_* 常量：跨模块协调协议的唯一权威字段
        self.interaction_state = INTERACTION_NORMAL

    # ---------------------------------------------------------------- 几何
    @property
    def scale(self) -> float:
        """渲染/逻辑缩放（菜单"大小"档位写入口）。"""
        return self._scale

    @scale.setter
    def scale(self, value: float) -> None:
        """V-10：缩放变化必须置脏 + 按新身体框补钳 + 上报新旧矩形。

        裸属性时期菜单改大小：不置 _frame_dirty、不重钳、advance 返回
        None——静态素材下永远不重绘，且尺寸变大后可能滞留界外。
        """
        value = float(value)
        if value <= 0:
            raise ValueError(f"scale 必须为正: {value!r}")
        if value == self._scale:
            return
        old_rect = self.rect()
        self._scale = value
        self._invalidate_frames()  # 签名含 scale：立即按新尺寸重建（D2）
        self.set_pos(self.pos)    # 尺寸变化后按新身体框补钳（内部判变）
        self._notify_dirty(old_rect, self.rect())

    def _logical_size(self) -> tuple[int, int]:
        """逻辑大小（CANVAS*scale）：rect/命中/碰撞坐标系的尺寸，与 DPR 无关。"""
        return (
            max(1, int(round(catalog.CANVAS_W * self.scale))),
            max(1, int(round(catalog.CANVAS_H * self.scale))),
        )

    def _scaled_size(self) -> tuple[int, int]:
        """物理像素大小（CANVAS*scale*dpr）：渲染/命中图的像素尺寸。"""
        dpr = self._dpr
        return (
            max(1, int(round(catalog.CANVAS_W * self.scale * dpr))),
            max(1, int(round(catalog.CANVAS_H * self.scale * dpr))),
        )

    def _invalidate_frames(self) -> None:
        """作废帧缓存并按当前 (scale, dpr) 立即重建（D2 唯一作废入口）。

        scale / dpr 任一变化都必须走这里：签名残留会让快路径跳过重建、
        旧成品继续显示（125%/150% 下变糊）；命中图残留更糟——它是物理
        像素图，新 dpr 索引旧图会越界/错位误判穿透。语义对齐旧路径
        window.py:2129-2150（_refresh_frame_for_screen_dpr 强制 _rebuild_frame
        + update）：这里同步重建，首帧未就绪时 _rebuild_pixmap 保持旧成品
        不破坏显示（失败不记账，见 _rebuild_pixmap 的签名写入时机）。
        """
        self._frame_sig = None
        self._hit_image = None
        self._frame_dirty = True
        self._rebuild_pixmap()

    def rect(self) -> QRect:
        """sprite 在 overlay 局部逻辑坐标系下的外接矩形（整数化后的绘制矩形）。

        恒为逻辑坐标（CANVAS*scale 逻辑大小）——位置/命中/碰撞全部留在
        逻辑坐标系，只有渲染像素随 DPR（D2）。
        """
        w, h = self._logical_size()
        return QRect(int(self.pos.x()), int(self.pos.y()), w, h)

    def center(self) -> QPointF:
        r = self.rect()
        return QPointF(r.x() + r.width() / 2, r.y() + r.height() / 2)

    def body_rect(self) -> QRect:
        """稳定身体框（overlay 局部逻辑坐标）= rect 原点 + body_box×scale。

        贴边钳制/碰撞体/气泡锚点的统一口径（V-2）：未声明 body_box 的
        角色包回退全画布（= rect()，语义等同"画布即身体"）。
        """
        body = self._body_local_rect()
        r = self.rect()
        return QRect(r.x() + body.x(), r.y() + body.y(),
                     body.width(), body.height())

    def radius(self) -> float:
        """碰撞圆半径（与 Phase 0 探针同一经验系数：0.45 * min(w, h)）。"""
        r = self.rect()
        return 0.45 * min(r.width(), r.height())

    # ---------------------------------------------------------------- DPR（D2）
    @property
    def dpr(self) -> float:
        """当前渲染 DPR（pixmap 物理像素 = 逻辑大小 × dpr）。"""
        return self._dpr

    def set_dpr(self, dpr: float) -> None:
        """设置渲染 DPR（overlay 按所在屏 devicePixelRatio 写入）。

        只影响渲染像素：pixmap 物理尺寸与命中图随 dpr 立即重建（旧成品与
        旧命中图绝不留在缓存里），并报脏要求重绘——屏 DPR 变化时宠物静止
        也要立刻换清晰成品，不等下一个 tick/重绘（旧路径
        window.py:2137-2142 同语义：_rebuild_frame + update）。rect/pos/
        velocity 等逻辑几何不变。
        """
        dpr = float(dpr)
        if dpr <= 0:
            raise ValueError(f"dpr 必须为正: {dpr!r}")
        if dpr == self._dpr:
            return
        self._dpr = dpr
        self._invalidate_frames()
        rect = self.rect()
        self._notify_dirty(rect, rect)  # 立即重绘（不等 tick）

    # ---------------------------------------------------------------- 钳制（D3）
    @property
    def bounds(self) -> QRectF | None:
        """当前钳制域（overlay 局部逻辑坐标；None = 不钳制）。"""
        return self._bounds

    def set_bounds(self, bounds: QRect | QRectF | None) -> None:
        """设置钳制域（overlay 局部逻辑坐标的工作区）；None 解除钳制。

        设置时对当前位置立即补一次钳制，保证不变量即刻成立。
        """
        self._bounds = None if bounds is None else QRectF(bounds)
        if self._bounds is not None:
            self.set_pos(self.pos)

    def _body_local_rect(self) -> QRect:
        """稳定身体框（sprite 局部逻辑坐标，body_box×scale）。

        与 window_placement.stable_body_local_rect 同一取数口径：角色
        manifest 的 body_box（源像素、已镜像对称化）× scale；未声明的角色包
        回退全画布（语义等同"画布即身体"，钳制退化为整矩形钳制）。sprite 无
        捕获头区/落地绘制偏移（帧即画布对齐左上角），故不加旧路径的
        headroom/PAD 修正。
        """
        character_id = str(getattr(self.library, "character_id", "") or "")
        box = catalog.character_body_box(character_id)
        if box is None:
            w, h = self._logical_size()
            return QRect(0, 0, w, h)
        x1, y1, x2, y2 = box
        s = self.scale
        return QRect(
            int(round(x1 * s)),
            int(round(y1 * s)),
            max(1, int(round((x2 - x1) * s))),
            max(1, int(round((y2 - y1) * s))),
        )

    @staticmethod
    def _clamp_axis(value: float, lo: float, hi: float, off: float, span: float) -> float:
        """把 value 钳到使 [value+off, value+off+span] 完整落在 [lo, hi] 内。

        hi 为连续右/下界（不含端点语义，对齐 QRectF 的 left+width）。可用区
        比身体还窄/矮时上界 < 下界，钳到下界（同 window_placement.clamp_span
        的小屏兜底模式）。
        """
        lower = lo - off
        upper = hi - off - span
        if upper < lower:
            return lower
        return min(max(value, lower), upper)

    def set_pos(self, pos: QPointF) -> None:
        """唯一位置写入口：写入前按 body_box 钳制（D3）。

        身体框（body_box×scale，相对 sprite 左上角偏移）必须完整落在
        bounds 内；sprite 外接矩形允许溢出（角色形象贴到屏幕边缘，画布
        透明边可以越界）。无 bounds 或无 body_box 声明（回退全画布）时
        等价于整矩形钳制。模块内一切位置写入（velocity 积分/拖拽/外部
        控制器）都必须经此入口。
        """
        x, y = float(pos.x()), float(pos.y())
        b = self._bounds
        if b is not None:
            body = self._body_local_rect()
            x = self._clamp_axis(x, b.left(), b.left() + b.width(),
                                 body.x(), body.width())
            y = self._clamp_axis(y, b.top(), b.top() + b.height(),
                                 body.y(), body.height())
        new_pos = QPointF(x, y)
        if new_pos == self.pos:
            return
        old_rect = self.rect()
        self.pos = new_pos
        # V-3/V-4：位移直驱重绘——物理控制器（before_sprites_advance）与
        # 拖拽事件都在 advance 之外移动 sprite，旧位置必须即时上报清除，
        # 否则静态素材冻结、动画素材拖尾。overlay 的 update 天然合并
        # 同一事件轮次内的多次调用，tick 内多次 set_pos 不会放大 paint。
        self._notify_dirty(old_rect, self.rect())

    def _notify_dirty(self, old: QRect, new: QRect) -> None:
        """向 overlay 上报脏矩形（add_sprite 挂接；未挂接时 no-op）。"""
        cb = self._dirty_cb
        if cb is not None:
            cb(old, new)

    def set_velocity(self, velocity: QPointF) -> None:
        self.velocity = QPointF(velocity)
        # M-1：非零速度 = 运动信号，同步唤醒 tick 档位——行为掷骰起步、
        # 碰撞写回、松手甩出都不等下一个（可能已降档的）tick
        cb = self._kinetic_cb
        if cb is not None and not self.velocity.isNull():
            cb()

    def close(self) -> None:
        """释放 clip 所有权（V-8）：断开信号 + 停解码 + 清帧缓存。

        overlay.remove_sprite（release_clip=True，默认）调用——sprite 移除
        即停解码，不再靠库 shutdown() 兜底。屏迁移等需要保留 clip 的
        场景走 remove_sprite(release_clip=False)。
        """
        clip = self._clip
        if clip is not None:
            try:
                clip.frameChanged.disconnect(self._on_frame_changed)
            except (TypeError, RuntimeError):
                pass  # 未连接过/对象已毁：忽略
            stop = getattr(clip, "stop", None)
            if callable(stop):
                stop()
        self._clip = None
        self._clip_name = None
        self._frame_sig = None
        self._pixmap = None
        self._hit_image = None
        self._dirty_cb = None
        self._kinetic_cb = None
        self._last_reported_rect = None

    # ---------------------------------------------------------------- clip 绑定
    def bind_clip(self, name: str) -> None:
        """绑定并起播一个动画 clip；旧 clip 断开信号并停止（不再后台解码）。

        clip 的 frameChanged 本来就 emit 在 GUI 线程，且 sprite 由 overlay
        持有（同线程），直接连接即可，无需 queued。
        """
        if self._clip is not None:
            try:
                self._clip.frameChanged.disconnect(self._on_frame_changed)
            except (TypeError, RuntimeError):
                pass  # 未连接过/对象已毁：忽略
            stop = getattr(self._clip, "stop", None)
            if callable(stop):
                stop()
        self._clip_name = name
        self._clip = self.library.movie(name)
        self._clip.frameChanged.connect(self._on_frame_changed)
        # 换 clip 后签名/缓存作废；首帧到达前先按脏处理，保证首 tick 上屏
        self._frame_sig = None
        self._pixmap = None
        self._hit_image = None
        self._frame_dirty = True
        setter = getattr(self._clip, "set_playback_speed", None)
        if callable(setter):
            setter(self.playback_speed)
        start = getattr(self._clip, "start", None)
        if callable(start):
            start()

    def _on_frame_changed(self, _frame: int) -> None:
        self._frame_dirty = True
        # V-4：帧到达直驱重绘，不经过 tick——tick 降档后动画帧率不随之掉
        rect = self.rect()
        self._notify_dirty(rect, rect)

    # ---------------------------------------------------------------- tick
    def advance(self, dt: float) -> tuple[QRect, QRect] | None:
        """推进一 tick：仅在 "normal" 状态下按 velocity 积分位置。

        拖拽（"drag"）位置由 on_move 驱动；抛掷（"thrown"）位置由
        sprite_physics.ThrowPhysicsController 在 before_sprites_advance
        阶段做 ≤8ms 子步积分——两者都不能在此再积一次（否则双重积分）。

        返回 (旧矩形, 新矩形) 供 overlay 合并脏区域与位置监听 fanout；
        位置未动且帧未变（视觉无变化）返回 None——overlay 据此跳过
        update（按需刷新铁律，见 Phase 0 实测：整窗重绘 CPU +36%）。

        旧矩形取 _last_reported_rect（上次上报给 overlay 的 rect）而非
        本函数入口的 rect()：物理控制器（before_sprites_advance）与拖拽
        事件都在 advance 之外移动 sprite，入口取 rect 会 old==new 漏报
        旧位置（V-3）。
        """
        old = (self._last_reported_rect
               if self._last_reported_rect is not None else self.rect())
        if self.interaction_state == INTERACTION_NORMAL and not self.velocity.isNull():
            self.set_pos(self.pos + self.velocity * dt)  # 积分也过 body_box 钳制
        squashing = False
        if self._squash_progress is not None:
            self._squash_progress += dt / (SQUASH_DURATION_MS / 1000.0)
            if self._squash_progress >= 1.0:
                self._squash_progress = None
            squashing = True  # 收势帧也要再画一次（回正）
        new = self.rect()
        if new != old or self._frame_dirty or self._squash_progress is not None or squashing:
            self._frame_dirty = False
            self._last_reported_rect = new
            return (old, new)
        return None

    # ---------------------------------------------------------------- 帧缓存与绘制
    def _mirror_frame(self) -> bool:
        """朝右且 clip 未登记 no_mirror（含文字素材）时镜像——同 window.py:2096。"""
        if self.facing != "right":
            return False
        no_mirror = getattr(self.library, "no_mirror", frozenset())
        return self._clip_name not in no_mirror

    def _rebuild_pixmap(self) -> bool:
        """按签名缓存重建当前帧：返回是否真正重建（False = 快路径复用）。

        签名 = (clip 身份, 源帧号, 镜像, 缩放, DPR)；任一变化才走
        取帧→镜像→预乘→Smooth 缩放 整条链。转换顺序与 window.py 一致：
        先转 ARGB32_Premultiplied 再缩放，避免直通 alpha 缩放产生暗边；
        缩放后的预乘图同时充任命中测试的 alpha 源（预乘不动 alpha 字节）。
        D2：缩放目标是物理像素（CANVAS*scale*dpr），pixmap 携带
        setDevicePixelRatio(dpr)，Qt 按逻辑大小绘制，HiDPI 下不糊。
        """
        clip = self._clip
        if clip is None:
            return False
        try:
            frame_n = clip.currentFrameNumber()
        except AttributeError:
            frame_n = None
        sig = (id(clip), frame_n, self._mirror_frame(), self.scale, self._dpr)
        if sig == self._frame_sig:
            return False
        img = clip_current_image(clip)
        if img is None or img.isNull():
            # 首帧未就绪/素材损坏：保留上一帧（若有），跳过本次重建
            return False
        if self._mirror_frame():
            img = img.mirrored(True, False)
        w, h = self._scaled_size()
        img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        img = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(self._dpr)
        self._pixmap = pm
        # V-15：显式深拷贝——convertToFormat（同格式）与 scaled（同尺寸）
        # 都可能返回隐式共享副本，不拷贝则 _hit_image 会别名 clip 的活帧
        # 缓冲（scale=1.0&dpr=1 时实测同指针），后台解码线程写缓冲时
        # 命中图被跨线程改。一帧一次 memcpy，成本可忽略
        self._hit_image = img.copy()
        self._frame_sig = sig
        return True

    def squash(self) -> None:
        """启动 Q 弹挤压（点击/真碰撞反馈，window.py _start_squash 语义）。"""
        self._squash_progress = 0.0
        rect = self.rect()
        self._notify_dirty(rect, rect)

    def _squashed_rect(self) -> QRect:
        """Q 弹帧的目标矩形（window.py:198 _squash_geometry 同式：
        pulse=sin(π·progress)，sy=1-0.15·pulse，sx=1+0.10·pulse，底中锚定）。"""
        r = self.rect()
        progress = max(0.0, min(1.0, float(self._squash_progress or 0.0)))
        pulse = math.sin(math.pi * progress)
        w = max(1, int(round(r.width() * (1.0 + 0.10 * pulse))))
        h = max(1, int(round(r.height() * (1.0 - 0.15 * pulse))))
        x = r.x() + int(round((r.width() - w) / 2))
        y = r.y() + (r.height() - h)
        return QRect(x, y, w, h)

    def paint(self, painter: QPainter) -> None:
        """把当前帧画到 overlay 的 painter 上（pos 即 overlay 局部坐标）。"""
        self._rebuild_pixmap()
        if self._pixmap is not None:
            if self._squash_progress is not None:
                painter.drawPixmap(self._squashed_rect(), self._pixmap)
            else:
                painter.drawPixmap(self.pos, self._pixmap)

    def alpha_at(self, local: QPoint | QPointF) -> int:
        """sprite 局部**逻辑**坐标处的 alpha（0-255）。镜像已烘焙进缓存帧，无需再翻转。

        命中图是物理像素（CANVAS*scale*dpr），输入逻辑坐标按「命中图物理
        尺寸 ÷ 逻辑尺寸」的实际比例换算（D2）——不用裸 dpr：物理尺寸是
        四舍五入取整，逻辑末列 ×dpr 可能越界误判穿透；按图实际比例换算
        则整幅逻辑矩形恰好覆盖整幅命中图。负数坐标 floor 到图外返回 0。
        """
        img = self._hit_image
        if img is None:
            return 0
        lw, lh = self._logical_size()
        x = math.floor(local.x() * img.width() / lw)
        y = math.floor(local.y() * img.height() / lh)
        if 0 <= x < img.width() and 0 <= y < img.height():
            return (img.pixel(x, y) >> 24) & 0xFF
        return 0

    # ---------------------------------------------------------------- 拖拽协议
    @property
    def dragging(self) -> bool:
        return self._dragging

    @property
    def drag_trail(self) -> list[tuple[float, float, float]]:
        """当前拖拽轨迹样本（(monotonic_ts, x, y)，只读副本；松手后清空）。"""
        return list(self._drag_trail)

    def on_press(self, pos: QPointF) -> None:
        """按下：进入拖拽态——记录 grab 偏移、velocity 归零、开始轨迹采样。

        轨迹样本（时间戳 + 光标位置）是松手时
        physics.estimate_release_velocity 估算甩出初速的输入。
        """
        self._dragging = True
        self.interaction_state = INTERACTION_DRAG
        self._drag_offset = QPointF(pos) - self.pos
        self.velocity = QPointF(0, 0)
        self._drag_trail = [(self._clock(), pos.x(), pos.y())]

    def on_move(self, pos: QPointF) -> None:
        if not self._dragging or self._drag_offset is None:
            return
        self.set_pos(QPointF(pos) - self._drag_offset)
        now = self._clock()
        self._drag_trail.append((now, pos.x(), pos.y()))
        cutoff = now - physics_mod.TRAIL_KEEP_SEC
        self._drag_trail = [s for s in self._drag_trail if s[0] >= cutoff]

    def on_release(self, pos: QPointF) -> None:
        """松手：落在光标处，按拖拽末段轨迹决定去向（语义对齐 window.py:3352-3366）。

        估算初速 >= DEAD_ZONE_SPEED → 置 "thrown" 并写初速（
        estimate_release_velocity 内部已过 soft_clamp_speed 软膝），
        抛掷物理控制器接管；低于死区 = 原地放下，velocity 归零回
        "normal"（行为控制器随后接管）。松手位置不入轨迹——否则
        estimate 的 RELEASE_STALE_SEC「停顿即静止放下」判定失效。
        """
        if not self._dragging:
            return
        if self._drag_offset is not None:
            self.set_pos(QPointF(pos) - self._drag_offset)
        self._dragging = False
        self._drag_offset = None
        rvx, rvy = physics_mod.estimate_release_velocity(
            self._drag_trail, self._clock(), cap=self.throw_speed_cap)
        self._drag_trail = []
        if not self.drag_physics:
            rvx, rvy = 0.0, 0.0  # 拖动物理关：原地放下（window.py 同语义）
        if math.hypot(rvx, rvy) < physics_mod.DEAD_ZONE_SPEED:
            self.velocity = QPointF(0, 0)
            self.interaction_state = INTERACTION_NORMAL
        else:
            self.velocity = QPointF(rvx, rvy)
            self.interaction_state = INTERACTION_THROWN
