# -*- coding: utf-8 -*-
"""首跑帧序列自动供给（帧序列化 B 档）offscreen 单测。

覆盖：
- 原子目录（转进 ``<stem>.tmp/``，成功后 rename；半成品对 library 不可见）；
- 固化 ffmpeg 参数（bgra/libvpx 两组防坑开关不许被改动）；
- 幂等跳过 + 旧版「只有帧没有 meta」产物只补 meta、不重编码；
- QLockFile 多实例互斥（拿不到锁放弃本轮）；无 ffmpeg exe 静默 no-op；
- session_ending 闸门（issue #111）：置位后不再转后续 clip；
- PET_FRAMESEQ=0 逃生门；库收尾 cancel + 有界等待；
- maybe_provision_frameseq → 完成回调 rescan → 新 clip 走 FrameSeqClip；
- tools/convert_frameseq.py 薄壳：复用核心、幂等、退出码口径不变。

纪律（AGENTS.md）：全部 offscreen，不依赖真实 ffmpeg 与真实 153MB 素材——
桩 ``_popen`` + 桩 fps 探测；线程时序用事件泵 + 宽预算，不用固定 sleep 赌时序。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication

from pet import frameseq_provision as fp
from pet import library as library_mod
from pet.frameseq_clip import FrameSeqClip
from pet.library import MovieLibrary
from pet.webm_clip import set_session_ending

app = QApplication.instance() or QApplication([])

FRAME_COUNT = 3


# ---------------------------------------------------------------- 测试夹具
class _FakeClip:
    """极简假 WebMClip：不碰 ffmpeg/Qt（拷自 test_library_priority_warm 口径）。"""

    def __init__(self, path, parent=None):
        self.path = Path(path)

    def warm_meta(self):
        return

    def warm_first_frame(self):
        return

    def stop(self):
        return

    def cleanup(self):
        return


def _make_pack(tmp_path: Path, folders: dict[str, list[str]]) -> tuple[Path, Path]:
    """临时角色包：videos/<folder>/<name>.webm（占位字节）。"""
    videos = tmp_path / "videos"
    for folder, names in folders.items():
        directory = videos / folder
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_bytes(b"fake-webm")
    return videos, tmp_path / "frameseq"


def _make_complete(out_dir: Path, frames: int = FRAME_COUNT) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, frames + 1):
        (out_dir / f"f_{i:04d}.webp").write_bytes(b"webp")
    (out_dir / "meta.json").write_text(
        json.dumps({"fps": 24.0, "source": "x.webm", "frames": frames}),
        encoding="utf-8")


def _install_fake_ffmpeg(monkeypatch, *, frames: int = FRAME_COUNT,
                         fail: bool = False, on_convert=None) -> list:
    """桩 subprocess：communicate() 时把 f_%04d.webp 落进参数里的输出目录。

    返回每次转换的桩进程（argv/returncode），供参数与调用次数断言。
    """
    calls: list = []

    class _Proc:
        def __init__(self, argv):
            self.argv = list(argv)
            self.returncode = 1 if fail else 0
            self._done = False

        def poll(self):
            return self.returncode if self._done else None

        def terminate(self):
            self.returncode = -1
            self._done = True

        def communicate(self):
            if not fail:
                out_dir = Path(self.argv[-1]).parent
                for i in range(1, frames + 1):
                    (out_dir / f"f_{i:04d}.webp").write_bytes(b"webp")
            if on_convert is not None:
                on_convert(self.argv)
            self._done = True
            return (b"", b"" if self.returncode == 0 else b"stub failure")

    def _fake_popen(argv, **_kwargs):
        proc = _Proc(argv)
        calls.append(proc)
        return proc

    monkeypatch.setattr(fp, "_popen", _fake_popen)
    monkeypatch.setattr(fp, "probe_fps", lambda _webm: 24.0)
    return calls


def _pump_until(cond, timeout_s: float = 10.0) -> None:
    """事件泵：等到条件为真（跨线程 queued 信号需要事件循环）。"""
    deadline = time.monotonic() + timeout_s
    while not cond():
        QApplication.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("事件泵超时")
        time.sleep(0.005)


def _pump_events(seconds: float = 0.2) -> None:
    """有界泵事件（负断言用：给 0ms singleShot 一个触发机会）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)


@pytest.fixture(autouse=True)
def _reset_session_ending():
    yield
    set_session_ending(False)


# ---------------------------------------------------------------- 转换核心
def test_convert_clip_atomic_dir_and_meta(tmp_path, monkeypatch):
    """转换中只有 <stem>.tmp/，成功后才 rename 成 <stem>/ 并写 meta.json。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    webm = videos / "idle" / "a.webm"
    out_dir = frameseq / "idle" / "a"
    tmp_dir = out_dir.with_name("a.tmp")
    seen = {}

    def _during(_argv):
        seen["target_exists"] = out_dir.exists()
        seen["tmp_frames"] = len(list(tmp_dir.glob("f_*.webp")))

    calls = _install_fake_ffmpeg(monkeypatch, on_convert=_during)
    converted, err = fp.convert_clip(webm, out_dir)

    assert (converted, err) == (True, "")
    assert len(calls) == 1
    # 转换进行中：目标目录不存在（library 永远捡不到半成品），帧落在 tmp
    assert seen == {"target_exists": False, "tmp_frames": FRAME_COUNT}
    assert not tmp_dir.exists()
    assert len(list(out_dir.glob("f_*.webp"))) == FRAME_COUNT
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta == {
        "fps": 24.0,
        "source": "a.webm",
        "frames": FRAME_COUNT,
        "encoder": fp.ENCODER_DESC,
    }


def test_convert_clip_ffmpeg_argv_is_frozen(tmp_path, monkeypatch):
    """ffmpeg 参数固化：bgra 直通 + libvpx-vp9 解码 + -nostdin，一项都不许少。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    webm = videos / "idle" / "a.webm"
    out_dir = frameseq / "idle" / "a"
    calls = _install_fake_ffmpeg(monkeypatch)

    converted, err = fp.convert_clip(webm, out_dir, exe="fake-ffmpeg")
    assert (converted, err) == (True, "")
    assert calls[0].argv == [
        "fake-ffmpeg",
        "-nostdin", "-v", "error", "-y",
        "-c:v", "libvpx-vp9",
        "-i", str(webm),
        "-pix_fmt", "bgra", "-c:v", "libwebp", "-lossless", "1",
        str(out_dir.with_name("a.tmp") / "f_%04d.webp"),
    ]


def test_convert_clip_idempotent_skip(tmp_path, monkeypatch):
    """已完成（f_*.webp + meta.json）→ 跳过，且不再拉 ffmpeg。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    webm = videos / "idle" / "a.webm"
    out_dir = frameseq / "idle" / "a"
    _install_fake_ffmpeg(monkeypatch)
    assert fp.convert_clip(webm, out_dir) == (True, "")

    calls = _install_fake_ffmpeg(monkeypatch)
    assert fp.convert_clip(webm, out_dir) == (False, "")
    assert calls == []
    assert fp.is_complete(out_dir) is True


def test_legacy_frames_without_meta_backfilled_not_reencoded(tmp_path, monkeypatch):
    """旧版产物（只有 f_*.webp、没有 meta.json）：只补 meta，不重编码。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    webm = videos / "idle" / "a.webm"
    out_dir = frameseq / "idle" / "a"
    out_dir.mkdir(parents=True)
    (out_dir / "f_0001.webp").write_bytes(b"webp")
    assert fp.is_complete(out_dir) is False

    calls = _install_fake_ffmpeg(monkeypatch)
    assert fp.convert_clip(webm, out_dir) == (False, "")   # 记为跳过，非新转
    assert calls == []                                     # 没有重编码
    assert fp.is_complete(out_dir) is True
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["frames"] == 1


def test_convert_clip_failure_cleans_tmp(tmp_path, monkeypatch):
    """ffmpeg 失败：清掉 tmp，目标目录不出现（半成品绝不留存）。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    out_dir = frameseq / "idle" / "a"
    _install_fake_ffmpeg(monkeypatch, fail=True)

    converted, err = fp.convert_clip(videos / "idle" / "a.webm", out_dir)
    assert converted is False
    assert "stub failure" in err
    assert not out_dir.exists()
    assert not out_dir.with_name("a.tmp").exists()


def test_convert_clip_cancelled_before_spawn(tmp_path, monkeypatch):
    """取消谓词已置位：不拉 ffmpeg、不留目录。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    calls = _install_fake_ffmpeg(monkeypatch)
    converted, err = fp.convert_clip(
        videos / "idle" / "a.webm", frameseq / "idle" / "a",
        cancelled=lambda: True)
    assert (converted, err) == (False, "")
    assert calls == []


def test_plan_clips_excludes_complete(tmp_path):
    """待供给清单只含未完成热集；冷集目录不参与。"""
    videos, frameseq = _make_pack(
        tmp_path, {"idle": ["a.webm", "b.webm"], "random": ["r.webm"]})
    _make_complete(frameseq / "idle" / "a")
    plan = fp.plan_clips(videos, frameseq)
    assert [(w.name, d.name) for w, d in plan] == [("b.webm", "b")]


# ---------------------------------------------------------------- 互斥 / 闸门 / 逃生门
def test_provision_once_lock_mutex(tmp_path, monkeypatch):
    """另一实例持有 QLockFile：直接放弃本轮（locked=True），不等待、不转换。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    frameseq.mkdir(parents=True)
    held = QLockFile(str(frameseq / fp.LOCK_NAME))
    assert held.tryLock(0) is True
    try:
        calls = _install_fake_ffmpeg(monkeypatch)
        report = fp.provision_once(videos, frameseq)
        assert report.locked is True
        assert (report.converted, report.skipped, report.failed) == (0, 0, 0)
        assert calls == []
        assert not (frameseq / "idle" / "a").exists()
    finally:
        held.unlock()

    # 锁释放后可正常转（证明上一步唯一差异就是锁）
    calls = _install_fake_ffmpeg(monkeypatch)
    report = fp.provision_once(videos, frameseq)
    assert report.locked is False
    assert report.converted == 1
    assert len(calls) == 1


def test_provision_once_stops_after_session_ending(tmp_path, monkeypatch):
    """issue #111 闸门：第一个 clip 转换期间会话结束 → 不再转后续 clip。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm", "b.webm"]})
    calls = _install_fake_ffmpeg(
        monkeypatch, on_convert=lambda _argv: set_session_ending(True))

    report = fp.provision_once(videos, frameseq)
    assert report.converted == 1
    assert len(calls) == 1
    assert (frameseq / "idle" / "a").is_dir()
    assert not (frameseq / "idle" / "b").exists()


def test_provision_once_disabled_by_env(tmp_path, monkeypatch):
    """PET_FRAMESEQ=0：整体禁用（dev 逃生门），零转换零进程。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    monkeypatch.setenv(fp.ENV_DISABLE, "0")
    calls = _install_fake_ffmpeg(monkeypatch)

    report = fp.provision_once(videos, frameseq)
    assert (report.converted, report.skipped, report.failed) == (0, 0, 0)
    assert calls == []
    assert not (frameseq / "idle" / "a").exists()


def test_provision_once_no_ffmpeg_is_silent(tmp_path, monkeypatch):
    """无 ffmpeg exe：静默 no-op（不计失败、不拉进程、不报错）。"""
    videos, frameseq = _make_pack(tmp_path, {"idle": ["a.webm"]})
    monkeypatch.setattr(fp, "ffmpeg_exe", lambda: None)

    report = fp.provision_once(videos, frameseq)
    assert (report.converted, report.skipped, report.failed) == (0, 0, 0)
    assert not (frameseq / "idle" / "a").exists()


# ---------------------------------------------------------------- 库接线
def test_maybe_provision_disabled_no_worker(tmp_path, monkeypatch):
    """PET_FRAMESEQ=0 时 maybe_provision_frameseq 连排期都不做。"""
    videos, _ = _make_pack(tmp_path, {"idle": ["x.webm"]})
    monkeypatch.setenv(fp.ENV_DISABLE, "0")
    monkeypatch.setattr(fp, "PROVISION_DELAY_MS", 0)
    monkeypatch.setattr(library_mod, "WebMClip", _FakeClip)
    lib = MovieLibrary(asset_dir=videos, prewarm_enabled=False)
    try:
        lib.maybe_provision_frameseq()
        _pump_events(0.2)
        assert lib._frameseq_provision_requested is False
        assert lib._frameseq_worker is None
    finally:
        lib.shutdown()


def test_complete_hot_set_spawns_no_worker(tmp_path, monkeypatch):
    """热集已完整：静默 no-op——不起线程、不碰 ffmpeg。"""
    videos, frameseq = _make_pack(
        tmp_path, {"idle": ["x.webm"], "random": ["r.webm"]})
    _make_complete(frameseq / "idle" / "x")
    monkeypatch.setattr(library_mod, "WebMClip", _FakeClip)
    monkeypatch.setattr(fp, "PROVISION_DELAY_MS", 0)
    spawned: list = []

    class _BoomWorker:
        def __init__(self, *args, **kwargs):
            spawned.append((args, kwargs))

    monkeypatch.setattr(fp, "FrameseqProvisionWorker", _BoomWorker)
    lib = MovieLibrary(asset_dir=videos, prewarm_enabled=False)
    try:
        lib.maybe_provision_frameseq()
        _pump_events(0.3)
        assert spawned == []
        assert lib._frameseq_worker is None
        assert "x" in lib._frameseq_dirs
    finally:
        lib.shutdown()


def test_shutdown_cancels_provision_worker_bounded(tmp_path, monkeypatch):
    """库收尾：置取消谓词 + 有界等待（≤2s），不留不受控重编码进程。"""
    videos, _ = _make_pack(tmp_path, {"idle": ["x.webm"]})
    monkeypatch.setattr(library_mod, "WebMClip", _FakeClip)
    lib = MovieLibrary(asset_dir=videos, prewarm_enabled=False)
    events = {}

    class _FakeWorker:
        def cancel(self):
            events["cancelled"] = True

        def wait(self, ms):
            events["wait_ms"] = ms

    lib._frameseq_worker = _FakeWorker()
    lib.shutdown()
    assert events == {"cancelled": True, "wait_ms": 2000}
    assert lib._frameseq_worker is None


def test_maybe_provision_then_rescan_switches_new_clip(tmp_path, monkeypatch):
    """端到端：延迟触发 → 后台转换 → 完成回调 rescan → 新 clip 为 FrameSeqClip。"""
    videos, frameseq = _make_pack(
        tmp_path, {"idle": ["x.webm"], "random": ["r.webm"]})
    monkeypatch.setattr(library_mod, "WebMClip", _FakeClip)
    monkeypatch.setattr(fp, "PROVISION_DELAY_MS", 0)
    calls = _install_fake_ffmpeg(monkeypatch)
    lib = MovieLibrary(asset_dir=videos, prewarm_enabled=False)
    try:
        assert lib._frameseq_dirs == {}
        lib.maybe_provision_frameseq()
        lib.maybe_provision_frameseq()  # 幂等：只排一次
        _pump_until(lambda: lib._frameseq_worker is None
                    and (frameseq / "idle" / "x").is_dir())
        assert len(calls) == 1
        assert "x" in lib._frameseq_dirs
        assert isinstance(lib.movie("x"), FrameSeqClip)   # 新请求走帧序列
        assert not isinstance(lib.movie("r"), FrameSeqClip)  # 冷集仍走 webm
    finally:
        lib.shutdown()


# ---------------------------------------------------------------- tools 薄壳
def test_tools_shell_delegates_to_core():
    """薄壳只做命令行：转换核心与参数表都直接复用 pet.frameseq_provision。"""
    import tools.convert_frameseq as shell

    assert shell.convert_clip is fp.convert_clip
    assert shell.HOT_FOLDERS == fp.HOT_FOLDERS


def test_tools_shell_idempotent_and_exit_codes(tmp_path, monkeypatch, capsys):
    """CLI 幂等与退出码不变：完成热集 → 新转 0 / 退出 0；角色缺失 → 退出 1。"""
    import tools.convert_frameseq as shell

    monkeypatch.setattr(shell, "REPO_ROOT", tmp_path)
    videos = tmp_path / "assets" / "characters" / "x" / "videos"
    (videos / "idle").mkdir(parents=True)
    (videos / "idle" / "a.webm").write_bytes(b"webm" * 10)
    _make_complete(videos.parent / "frameseq" / "idle" / "a")

    monkeypatch.setattr(sys, "argv", ["convert_frameseq.py", "--character", "x"])
    assert shell.main() == 0
    out = capsys.readouterr().out
    assert "clips: 新转 0 / 跳过 1 / 失败 0" in out

    monkeypatch.setattr(sys, "argv", ["convert_frameseq.py", "--character", "nope"])
    assert shell.main() == 1
    assert "角色素材目录不存在" in capsys.readouterr().err

    # 无匹配 webm（角色目录存在但热集为空）→ 同样退出 1
    (tmp_path / "assets" / "characters" / "empty" / "videos").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv",
                        ["convert_frameseq.py", "--character", "empty"])
    assert shell.main() == 1
    assert "没有匹配目录的 webm" in capsys.readouterr().err
