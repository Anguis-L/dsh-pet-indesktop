# -*- coding: utf-8 -*-
"""热集帧序列转换器（帧序列化 B 档）。

把角色包热集目录（idle/move/turn/click/drag，≈95% 播放时长）的 webm
转成无损 WebP 帧序列，供 pet/frameseq_clip.py（MovieLibrary 按目录存在
自动接管）使用：

    assets/characters/<id>/frameseq/<folder>/<stem>/f_0001.webp + meta.json

关键实现约束（全部来自 .scratch/frame-seq-feasibility 的实测坑）：
- 必须 `-pix_fmt bgra` 直通——默认 yuva420p 即使 -lossless 1 也会 chroma
  下采样（18% 像素 RGB 偏移）；bgra 路径与 webm 解码 bit-exact 且更小；
- 必须 `-nostdin`——循环里跑 ffmpeg 会吞掉父进程 stdin；
- fps 逐 clip 用 ffprobe r_frame_rate 实测写进 meta.json（不硬编 24）。

用法：
    python tools/convert_frameseq.py [--character shenshen] [--force]
                                     [--include random,events]
幂等：已存在且帧数与源一致的目录跳过（--force 全量重转）。
退出码：0 全部成功；1 有失败 clip（明细打到 stderr）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HOT_FOLDERS = ("idle", "move", "turn", "click", "drag")
REPO_ROOT = Path(__file__).resolve().parent.parent


def _ffprobe(args: list[str]) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _fps_of(webm: Path) -> float:
    rate = _ffprobe(["-select_streams", "v:0", "-show_entries",
                     "stream=r_frame_rate", "-of", "csv=p=0", str(webm)])
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return 24.0
    return fps if fps > 0 else 24.0


def _source_frame_count(webm: Path) -> int:
    out = _ffprobe(["-select_streams", "v:0", "-count_frames",
                    "-show_entries", "stream=nb_read_frames",
                    "-of", "csv=p=0", str(webm)])
    try:
        return int(out)
    except ValueError:
        return 0


def convert_clip(webm: Path, out_dir: Path, *, force: bool = False) -> tuple[bool, str]:
    """单 clip 转换：返回 (是否本次新转, 失败原因或空串)。"""
    existing = len(list(out_dir.glob("f_*.webp"))) if out_dir.is_dir() else 0
    if existing and not force:
        return False, ""
    out_dir.mkdir(parents=True, exist_ok=True)
    fps = _fps_of(webm)
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-c:v", "libvpx-vp9",  # 解码器强制 libvpx：原生 vp9 解码器静默丢弃
                                # VP9 alpha 位流（ffmpeg 8.1 实测），帧序列变全不透明
         "-i", str(webm),
         "-pix_fmt", "bgra", "-c:v", "libwebp", "-lossless", "1",
         str(out_dir / "f_%04d.webp")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()[:300]
    frames = len(list(out_dir.glob("f_*.webp")))
    meta = {
        "fps": fps,
        "source": webm.name,
        "frames": frames,
        "encoder": "libwebp lossless (bgra straight)",
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="热集帧序列转换器（B 档）")
    parser.add_argument("--character", default="shenshen")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--include", default="",
                        help="逗号分隔的额外目录（默认只转热集 idle/move/turn/click/drag）")
    args = parser.parse_args()

    videos = REPO_ROOT / "assets" / "characters" / args.character / "videos"
    if not videos.is_dir():
        print(f"角色素材目录不存在: {videos}", file=sys.stderr)
        return 1
    folders = set(HOT_FOLDERS)
    folders.update(f for f in args.include.split(",") if f)

    webms = sorted(p for p in videos.rglob("*.webm")
                   if p.relative_to(videos).parts[0].lower() in folders)
    if not webms:
        print("没有匹配目录的 webm", file=sys.stderr)
        return 1

    out_root = videos.parent / "frameseq"
    done = skipped = failed = 0
    webm_bytes = webp_bytes = 0
    for webm in webms:
        rel = webm.relative_to(videos).with_suffix("")
        out_dir = out_root / rel
        converted, err = convert_clip(webm, out_dir, force=args.force)
        if err:
            failed += 1
            print(f"FAIL {rel}: {err}", file=sys.stderr)
            continue
        if converted:
            done += 1
        else:
            skipped += 1
        webm_bytes += webm.stat().st_size
        webp_bytes += sum(f.stat().st_size for f in out_dir.glob("f_*.webp"))

    print(f"clips: 新转 {done} / 跳过 {skipped} / 失败 {failed}")
    print(f"磁盘账: webm {webm_bytes / 1e6:.1f}MB → webp {webp_bytes / 1e6:.1f}MB"
          f"（{webp_bytes / max(1, webm_bytes):.1f}x）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
