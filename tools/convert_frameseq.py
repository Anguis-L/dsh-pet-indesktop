# -*- coding: utf-8 -*-
"""热集帧序列转换器（帧序列化 B 档）——命令行薄壳。

转换核心全部在 ``pet/frameseq_provision.py``：运行时首跑自动供给与构建期
工具共用同一份实现（``<stem>.tmp/`` 原子目录、bgra + libvpx-vp9 两组防坑
参数、read_frames meta 探测 fps、幂等跳过）。本模块只保留命令行行为：

    assets/characters/<id>/frameseq/<folder>/<stem>/f_0001.webp + meta.json

用法：
    python tools/convert_frameseq.py [--character shenshen] [--force]
                                     [--include random,events]
幂等：已完成（f_*.webp + meta.json）的目录跳过；--force 全量重转。
退出码：0 全部成功；1 角色目录缺失/无匹配 webm/有失败 clip（明细打到 stderr）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pet.frameseq_provision import (  # noqa: E402  (须先补 sys.path 再导入)
    HOT_FOLDERS,
    clip_out_dir,
    convert_clip,
    hot_webms,
)

__all__ = ["HOT_FOLDERS", "REPO_ROOT", "clip_out_dir", "convert_clip",
           "hot_webms", "main"]


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

    webms = hot_webms(videos, folders)
    if not webms:
        print("没有匹配目录的 webm", file=sys.stderr)
        return 1

    out_root = videos.parent / "frameseq"
    done = skipped = failed = 0
    webm_bytes = webp_bytes = 0
    for webm in webms:
        rel = webm.relative_to(videos).with_suffix("")
        out_dir = clip_out_dir(webm, videos, out_root)
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
