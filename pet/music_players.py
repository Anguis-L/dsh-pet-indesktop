# -*- coding: utf-8 -*-
"""定位本机音乐播放器的可执行文件。

右键菜单的「打开网易云并播放」需要知道播放器装在哪。策略是**先自动搜常见
路径，再允许用户手动覆盖**：

1. 配置里的手动路径（``music_player_paths``）优先——用户在设置里指定过就用它；
2. 否则按候选目录名搜几个常见盘符（本机实测网易云在 ``D:\\CloudMusic``、
   QQ音乐在 ``D:\\QQ音乐\\QQMusic``，都不是默认的 Program Files）；
3. 都找不到就返回 ``None``，菜单项据此置灰并说明原因。

搜索结果进程内缓存：目录扫描有几十毫秒开销，而右键菜单每次打开都要问一遍。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# 已知播放器：key -> (显示名, 可执行文件名, 可能的父目录名)
PLAYERS = {
    "netease": ("网易云音乐", "cloudmusic.exe", ("CloudMusic", "网易云音乐", "Netease")),
    "qqmusic": ("QQ音乐", "QQMusic.exe", ("QQMusic", "QQ音乐")),
}

# 搜哪些根目录：两个常见盘符 + 用户的 AppData（部分安装器会装在这里）。
_SEARCH_ROOTS = ("D:/", "C:/", "C:/Program Files", "C:/Program Files (x86)",
                 "C:/Users/%s/AppData/Local" % os.environ.get("USERNAME", ""),
                 "C:/Users/%s/AppData/Roaming" % os.environ.get("USERNAME", ""))

# 每个候选目录最多往下找几层（避免全盘递归）。
_MAX_DEPTH = 3

# 进程内缓存：player_key -> 路径或 None（None 也要缓存，否则每次都白扫一遍）。
_cache: dict[str, str | None] = {}


def player_label(player_key: str) -> str:
    return PLAYERS.get(player_key, (player_key, "", ()))[0]


def find_player(player_key: str, manual_path: str = "") -> str | None:
    """返回播放器可执行文件的绝对路径；找不到返回 None。

    ``manual_path`` 是用户在设置里手填的路径：非空且指向真实文件时优先采用。
    """
    if player_key not in PLAYERS:
        return None
    manual = str(manual_path or "").strip()
    if manual:
        expanded = Path(manual).expanduser()
        if expanded.is_file():
            return str(expanded)
        # 手填了但文件不存在：不静默忽略，也别退回自动搜——
        # 否则用户会以为"我填的路径生效了"，实际用的是搜到的另一个。
        log.debug("音乐播放器路径无效：%s", manual)
        return None
    if player_key in _cache:
        return _cache[player_key]
    found = _search(player_key)
    _cache[player_key] = found
    return found


def _search(player_key: str) -> str | None:
    if sys.platform != "win32":
        return None
    _, exe_name, dir_names = PLAYERS[player_key]
    for root in _SEARCH_ROOTS:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for dir_name in dir_names:
            direct = root_path / dir_name / exe_name
            if direct.is_file():
                return str(direct)
        # 目录名对不上时，浅层扫一层子目录找同名 exe。
        hit = _shallow_scan(root_path, exe_name)
        if hit:
            return hit
    return None


def _shallow_scan(root: Path, exe_name: str) -> str | None:
    """在 root 下浅层找 exe_name（限制层数与目录数，避免拖慢菜单）。"""
    try:
        stack = [(root, 0)]
        visited = 0
        while stack and visited < 200:
            current, depth = stack.pop()
            if depth >= _MAX_DEPTH:
                continue
            visited += 1
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            target = current / exe_name
            if target.is_file():
                return str(target)
            for entry in entries:
                try:
                    if entry.is_dir():
                        stack.append((entry, depth + 1))
                except OSError:
                    continue
    except Exception:
        log.debug("搜索播放器路径失败：%s", root, exc_info=True)
    return None


def clear_cache() -> None:
    """清掉路径缓存。设置里改了手动路径后调用。"""
    _cache.clear()
