# -*- coding: utf-8 -*-
"""热集帧序列「首跑自动供给」（帧序列化 B 档）。

素材包不带 frameseq 热集时（安装包只带 webm），运行时后台低优先级把热集
webm（idle/move/turn/click/drag ≈95% 播放时长）转成无损 WebP 帧序列；
转换结束（含部分成功）由 MovieLibrary 在 GUI 线程 rescan，此后新请求的 clip
自动走 FrameSeqClip（已创建的 clip 不动）。依据与实测数字见
.scratch/frame-seq-feasibility/FEASIBILITY.md。

硬约束（实现即契约）：
- 逐 clip 先转到 ``<stem>.tmp/``，成功后再 ``os.rename`` 原子替换为
  ``<stem>/``：半成品永远不被 MovieLibrary 捡到（library 只认含 f_*.webp
  的 ``<stem>/`` 目录）；
- ffmpeg 参数固化不可改（见 ``_ffmpeg_argv``）：``-c:v libvpx-vp9`` 强制
  libvpx 解码（原生 vp9 解码器静默丢弃 VP9 alpha 位流）、``-pix_fmt bgra``
  直通（libwebp 走 yuva420p 会 chroma 下采样）、``-nostdin``（循环里跑
  ffmpeg 会吞父进程 stdin）；
- fps 探测用 ``imageio_ffmpeg.read_frames`` 的 meta（运行时不带 ffprobe）；
- 幂等：``<stem>/`` 已有 f_*.webp 且 meta.json 即跳过；旧版「只有帧没有
  meta」的产物只补 meta.json（不重编码），保持首跑不重转已完成热集；
- 多实例互斥：frameseq 根目录 QLockFile（``.converting.lock``），拿不到锁
  直接放弃本轮（另一实例在转），不等待；
- ISSUE-111 纪律：每个 clip 转换前查 ``pet.webm_clip.session_ending()``，
  置位即停止后续；library shutdown 置 cancel 谓词并终止在飞 ffmpeg 子进程，
  线程有界等待后退出；
- Windows 下 ffmpeg 子进程 ``creationflags=BELOW_NORMAL_PRIORITY_CLASS``
  （POSIX 忽略），一次性重编码的 CPU 让给交互；
- ``PET_FRAMESEQ=0`` 整体禁用供给（dev 逃生门，不进 Config/设置页/schema）。

线程模型：``FrameseqProvisionWorker`` 是 library 拥有的 QThread（QObject
亲和 GUI 线程）；``run()`` 内只碰纯 Python 数据与线程内自建的 QLockFile，
收尾用 queued ``finished_work`` 信号回 GUI 线程触发 rescan——Qt 对象不出
owning 线程。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PySide6.QtCore import QLockFile, QThread, Signal

from .webm_clip import session_ending

try:  # 与 webm_clip 同一依赖口：不可用时供给静默 no-op（播放同样会降级）
    import imageio_ffmpeg
except Exception:  # pragma: no cover - 缺依赖属环境问题
    imageio_ffmpeg = None

logger = logging.getLogger(__name__)

# 热集目录（≈95% 播放时长）：其余冷集（random/events）保留现 webm 路径
HOT_FOLDERS: tuple[str, ...] = ("idle", "move", "turn", "click", "drag")
# dev 逃生门（不进 Config/设置页/schema）
ENV_DISABLE = "PET_FRAMESEQ"
# frameseq 根目录锁（多实例互斥）
LOCK_NAME = ".converting.lock"
# 库创建后延迟触发：让启动峰值（高优先级预热）先过去
PROVISION_DELAY_MS = 5000
# 半成品临时目录后缀（与最终目录同名不同路径，library 只认后者）
TMP_SUFFIX = ".tmp"
DEFAULT_FPS = 24.0
ENCODER_DESC = "libwebp lossless (bgra straight)"
# 与 webm_clip._FFMPEG_INPUT_PARAMS 同口径（fps 探测用；转换参数见 _ffmpeg_argv）
_PROBE_INPUT_PARAMS = ["-c:v", "libvpx-vp9", "-threads", "1"]


def provision_disabled() -> bool:
    """PET_FRAMESEQ=0 时整体禁用首跑供给（dev 逃生门，仅读环境变量）。"""
    return os.environ.get(ENV_DISABLE, "").strip() == "0"


def is_complete(out_dir: Path | str) -> bool:
    """帧序列目录是否已完成：含 f_*.webp 且含 meta.json。"""
    path = Path(out_dir)
    if not (path / "meta.json").is_file():
        return False
    return any(path.glob("f_*.webp"))


def hot_webms(videos_dir: Path | str,
              folders: Iterable[str] = HOT_FOLDERS) -> list[Path]:
    """热集目录下的全部 webm（按相对路径排序，行为同构建期工具）。"""
    root = Path(videos_dir)
    wanted = {str(f).lower() for f in folders}
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("*.webm"):
        rel = path.relative_to(root)
        if len(rel.parts) > 1 and rel.parts[0].lower() in wanted:
            found.append(path)
    return sorted(found)


def clip_out_dir(webm: Path | str, videos_dir: Path | str,
                 frameseq_root: Path | str) -> Path:
    """webm 对应的帧序列输出目录：frameseq/<folder>/<stem>/。"""
    rel = Path(webm).relative_to(Path(videos_dir)).with_suffix("")
    return Path(frameseq_root) / rel


def plan_clips(videos_dir: Path | str, frameseq_root: Path | str,
               *, folders: Iterable[str] = HOT_FOLDERS) -> list[tuple[Path, Path]]:
    """待供给清单（webm → 输出目录）：只含未完成的 clip；空 = 热集已完整。"""
    root = Path(videos_dir)
    out_root = Path(frameseq_root)
    plan: list[tuple[Path, Path]] = []
    for webm in hot_webms(root, folders):
        out_dir = clip_out_dir(webm, root, out_root)
        if not is_complete(out_dir):
            plan.append((webm, out_dir))
    return plan


def probe_fps(webm: Path | str) -> float:
    """用 imageio_ffmpeg.read_frames 的 meta 探测 fps（运行时禁用 ffprobe）。

    只取首个 meta 即 close（不消费帧），失败/异常回退 DEFAULT_FPS。
    """
    if imageio_ffmpeg is None or session_ending():
        return DEFAULT_FPS
    gen = None
    try:
        gen = imageio_ffmpeg.read_frames(
            str(webm), pix_fmt="bgra", bits_per_pixel=32,
            input_params=list(_PROBE_INPUT_PARAMS),
        )
        meta = next(gen)
        fps = float(meta.get("fps") or 0.0)
        return fps if fps > 0 else DEFAULT_FPS
    except Exception:
        return DEFAULT_FPS
    finally:
        if gen is not None:
            try:
                gen.close()
            except Exception:
                pass


def ffmpeg_exe() -> str | None:
    """imageio_ffmpeg 自带 exe；不可用/会话结束（issue #111）时返回 None。"""
    if imageio_ffmpeg is None or session_ending():
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _creation_flags() -> int:
    """Windows：ffmpeg 低于正常优先级（POSIX 忽略，返回 0）。"""
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))


def _ffmpeg_argv(exe: str, webm: Path, tmp_dir: Path) -> list[str]:
    """固化转换参数（任何一项都来自 FEASIBILITY.md 的实测坑，禁止改动）。"""
    return [
        exe,
        "-nostdin", "-v", "error", "-y",
        # 解码器强制 libvpx：原生 vp9 解码器静默丢弃 VP9 alpha 位流
        "-c:v", "libvpx-vp9",
        "-i", str(webm),
        # bgra 直通：默认 yuva420p 即使 -lossless 1 也 chroma 下采样
        "-pix_fmt", "bgra", "-c:v", "libwebp", "-lossless", "1",
        str(tmp_dir / "f_%04d.webp"),
    ]


def _popen(argv: Sequence[str], **kwargs) -> subprocess.Popen:
    """Popen 适配层（测试注入点：假 exe / 桩 subprocess）。"""
    return subprocess.Popen(list(argv), **kwargs)


def _write_meta(out_dir: Path, webm: Path, *, fps: float, frames: int) -> None:
    meta = {
        "fps": float(fps),
        "source": webm.name,
        "frames": int(frames),
        "encoder": ENCODER_DESC,
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _err_text(raw) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return str(raw or "").strip()[:300]


def convert_clip(webm: Path | str, out_dir: Path | str, *, force: bool = False,
                 exe: str | None = None, fps_probe: Callable[[Path], float] | None = None,
                 on_proc: Callable[[object], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> tuple[bool, str]:
    """单 clip 转换：``(是否本次新转, 失败原因或空串)``。

    - 已完成（f_*.webp + meta.json）→ (False, "")；旧版只有帧没有 meta 的
      产物 → 只补 meta.json，同样记 (False, "")（不重编码、不重转）；
    - 先转 ``<stem>.tmp/``，成功后 ``os.rename`` 原子替换为 ``<stem>/``；
    - ``cancelled`` 置位：终止在飞 ffmpeg、清掉半成品，返回 (False, "")。
    """
    source = Path(webm)
    target = Path(out_dir)
    if not force and target.is_dir():
        frames = sorted(target.glob("f_*.webp"))
        if frames:
            if not (target / "meta.json").is_file():
                # 旧版产物（无 meta）：补元数据即可，禁止重编码 153MB 热集
                probe = fps_probe or probe_fps
                _write_meta(target, source, fps=probe(source), frames=len(frames))
            return False, ""
    if cancelled is not None and cancelled():
        return False, ""
    if session_ending():
        return False, ""  # issue #111：会话结束绝不再派生 ffmpeg（静默降级）
    exe_path = exe or ffmpeg_exe()
    if not exe_path:
        return False, "ffmpeg 不可用"

    tmp_dir = target.with_name(target.name + TMP_SUFFIX)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    probe = fps_probe or probe_fps
    fps = probe(source)

    proc = _popen(
        _ffmpeg_argv(exe_path, source, tmp_dir),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=_creation_flags(),
    )
    if on_proc is not None:
        on_proc(proc)
    err: object = b""
    try:
        _out, err = proc.communicate()
    except Exception as exc:  # 子进程被 terminate/句柄异常：按失败收口
        err = str(exc)
    finally:
        if on_proc is not None:
            on_proc(None)

    if cancelled is not None and cancelled():
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False, ""
    if proc.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False, _err_text(err)

    frames = len(list(tmp_dir.glob("f_*.webp")))
    if frames <= 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False, "ffmpeg 未产出任何帧"
    _write_meta(tmp_dir, source, fps=fps, frames=frames)
    if target.exists():
        # 仅 --force 重转会走到这里：旧产物先让位（tmp 全程就绪，不存在
        # "半个目标目录"窗口；library 此刻回退 webm 解码，非错误态）
        shutil.rmtree(target, ignore_errors=True)
    os.rename(tmp_dir, target)  # 原子替换：library 只认完成态目录
    return True, ""


@dataclass
class ProvisionReport:
    """单轮供给结果（供日志/测试断言）。"""

    converted: int = 0
    skipped: int = 0
    failed: int = 0
    locked: bool = False


def provision_once(videos_dir: Path | str, frameseq_root: Path | str, *,
                   exe: str | None = None,
                   fps_probe: Callable[[Path], float] | None = None,
                   on_proc: Callable[[object], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> ProvisionReport:
    """同步执行一轮供给（QLockFile 互斥 + 逐 clip 转换 + 会话闸门）。

    拿不到锁直接返回 ``locked=True``（另一实例在转，不等待）；PET_FRAMESEQ=0
    或会话结束时直接 no-op。逐个 clip 转换前复查会话闸门/取消谓词。
    """
    report = ProvisionReport()
    if provision_disabled() or session_ending():
        return report
    exe_path = exe or ffmpeg_exe()
    if not exe_path:
        return report  # 无 ffmpeg exe（含 imageio 不可用）：静默 no-op
    root = Path(frameseq_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return report
    lock = QLockFile(str(root / LOCK_NAME))
    lock.setStaleLockTime(30000)
    if not lock.tryLock(0):
        report.locked = True
        return report
    try:
        for webm, out_dir in plan_clips(videos_dir, root):
            if cancelled is not None and cancelled():
                break
            if session_ending():
                break  # issue #111：置位后不再为后续 clip 派生 ffmpeg
            converted, err = convert_clip(
                webm, out_dir, exe=exe_path, fps_probe=fps_probe,
                on_proc=on_proc, cancelled=cancelled,
            )
            if err:
                report.failed += 1
                logger.warning('帧序列供给失败 %s: %s', webm.name, err)
            elif converted:
                report.converted += 1
            else:
                report.skipped += 1
    finally:
        lock.unlock()
    return report


class FrameseqProvisionWorker(QThread):
    """library 拥有的低优先级供给线程（run 内不创建跨线程 Qt 对象）。

    ``cancel()`` 由 GUI 线程调用：置取消谓词并 terminate 在飞 ffmpeg 子进程，
    使 reader ``communicate()`` 立即返回；收尾经 queued ``finished_work``
    信号回 GUI 线程触发 rescan。
    """

    finished_work = Signal()

    def __init__(self, videos_dir: Path | str, frameseq_root: Path | str,
                 parent=None) -> None:
        super().__init__(parent)
        self._videos_dir = Path(videos_dir)
        self._frameseq_root = Path(frameseq_root)
        self._cancel = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc: object | None = None
        self.report = ProvisionReport()

    def cancel(self) -> None:
        """置取消谓词 + 终止在飞 ffmpeg（幂等，任意线程可调用）。"""
        self._cancel.set()
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return
        poll = getattr(proc, "poll", None)
        try:
            if callable(poll) and poll() is not None:
                return
            proc.terminate()
        except Exception:
            pass

    def _track_proc(self, proc: object) -> None:
        with self._proc_lock:
            self._proc = proc

    def run(self) -> None:  # noqa: D401 - QThread 入口
        try:
            self.report = provision_once(
                self._videos_dir, self._frameseq_root,
                on_proc=self._track_proc, cancelled=self._cancel.is_set,
            )
        except Exception:
            logger.debug('帧序列供给线程异常结束', exc_info=True)
        finally:
            self.finished_work.emit()
