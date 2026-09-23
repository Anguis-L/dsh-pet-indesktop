# -*- coding: utf-8 -*-
"""Phase 4.2b：overlay 拓扑的活跃宠集合 + 无锁 slot 身份分配 + 每身份几何持久化。

设计稿：.scratch/single-overlay-window/PHASE4_DESIGN.md（D5/D6/T5）。

- **D5 活跃宠集合**：启动恢复依据 = 显式"活跃宠"清单（运行时状态文件
  ``overlay-active-pets.json``），**不是**"slot 配置存在"。旧多进程路径里
  "退出子肥鱼"只关进程、slot 配置保留；若按配置存在复活，退出的子肥鱼会在
  重启后复活，与旧语义冲突。清单是运行时状态，不进 Config schema/设置页。
- **D6 无锁 slot 分配器**：进程内扫 ``config-slot-N.json`` + 活跃身份分配
  最小空闲 N，替代 ``spawn_in_process_window`` 的文件锁分配（app.py:2789-2807
  旧语义）。overlay 拓扑由 D4 进程门保证单实例，不再需要 slot 锁做跨进程互斥。
- **每身份几何持久化**：每只子肥鱼按各自 slot 身份把 rx/ry/facing/scale 合并
  写回 ``config-slot-N.json``（命名冻结：``agent_link.other_instances_use_agent``
  依赖 ``config-*.json`` glob，T5）。

纯逻辑零 Qt（便于单测）：磁盘访问只走 Path + json，写盘原子（tmp + replace，
对齐 Config.save / slot_manager 既有惯例）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path

# 活跃宠清单：运行时状态文件。刻意不带 config- 前缀——既不进旧 slot 配置
# glob（agent_link/config 迁移），也不与 runtime 标记 glob 相撞。
ACTIVE_PETS_FILENAME = "overlay-active-pets.json"
ACTIVE_PETS_VERSION = 1
# slot 扫描上限（对齐 slot_manager.acquire_pet_slot 的 max_scan_slots=128）
MAX_SCAN_SLOTS = 128

_GEOMETRY_KEYS = ("rx", "ry", "facing", "scale")
_SLOT_CONFIG_RE = re.compile(r"^config-slot-(\d+)\.json$")


class SpawnStateError(Exception):
    """slot 身份分配失败（前 MAX_SCAN_SLOTS 个身份均被占用）。"""


# ------------------------------------------------------------------ 活跃宠清单
def active_pets_path(config_dir: Path | str) -> Path:
    """活跃宠清单路径：``<config_dir>/overlay-active-pets.json``。"""
    return Path(config_dir) / ACTIVE_PETS_FILENAME


def normalize_slots(slots) -> list[int]:
    """去重并保持顺序的正整数 slot 列表（非法条目丢弃）。"""
    result: list[int] = []
    seen: set[int] = set()
    for item in slots or ():
        try:
            slot = int(item)
        except (TypeError, ValueError):
            continue
        if slot < 1 or slot in seen:
            continue
        seen.add(slot)
        result.append(slot)
    return result


def load_active_slots(config_dir: Path | str) -> list[int]:
    """读活跃宠清单（spawn 顺序）。

    防御性回退：文件缺失 = 首次运行语义（旧版主配置兼容）；文件损坏、不是
    对象、缺首字段 ``version``、``slots`` 非列表 → 一律按"只有主宠"处理
    （返回空清单），绝不把损坏状态当成活跃集合。
    """
    path = active_pets_path(config_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        logging.warning("overlay: 活跃宠清单不可读，按只有主宠处理: %s", path)
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logging.warning("overlay: 活跃宠清单损坏，按只有主宠处理: %s", path)
        return []
    if not isinstance(data, dict) or data.get("version") != ACTIVE_PETS_VERSION:
        logging.warning("overlay: 活跃宠清单缺首字段 version，按只有主宠处理: %s", path)
        return []
    slots = data.get("slots")
    if not isinstance(slots, list):
        logging.warning("overlay: 活跃宠清单 slots 非法，按只有主宠处理: %s", path)
        return []
    return normalize_slots(slots)


def save_active_slots(config_dir: Path | str, slots) -> bool:
    """原子写活跃宠清单（tmp + os.replace）。返回是否落盘成功。"""
    path = active_pets_path(config_dir)
    payload = {
        "version": ACTIVE_PETS_VERSION,
        "slots": normalize_slots(slots),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        logging.warning("overlay: 写活跃宠清单失败: %s", path, exc_info=True)
        return False
    return True


# ------------------------------------------------------------------ 无锁 slot 分配
def slot_config_path(config_dir: Path | str, slot_id: int) -> Path:
    """slot 身份对应的配置文件路径（命名冻结，T5）。"""
    return Path(config_dir) / f"config-slot-{int(slot_id)}.json"


def scan_slot_configs(config_dir: Path | str | None) -> set[int]:
    """扫目录内已有 ``config-slot-N.json`` 的 slot 编号集合（无目录 → 空集）。"""
    if not config_dir:
        return set()
    try:
        files = list(Path(config_dir).glob("config-slot-*.json"))
    except OSError:
        return set()
    slots: set[int] = set()
    for path in files:
        match = _SLOT_CONFIG_RE.match(path.name)
        if match:
            slots.add(int(match.group(1)))
    return slots


def allocate_slot(config_dir: Path | str | None = None, *, active_slots=(),
                  max_slots: int = MAX_SCAN_SLOTS) -> int:
    """无锁身份分配（D6）：取最小空闲 N。

    已占用 = 目录内已有 ``config-slot-N.json`` 的身份 ∪ 进程内活跃身份。
    slot 配置身份一经分配即保留（"退出子肥鱼配置保留"），因此分配单调增长、
    永不与既有身份相撞——复用一个保留身份会静默覆盖它的位置/设置记忆。
    """
    taken = scan_slot_configs(config_dir)
    for slot in normalize_slots(active_slots):
        taken.add(slot)
    for candidate in range(1, int(max_slots) + 1):
        if candidate not in taken:
            return candidate
    raise SpawnStateError(f"overlay: 前 {max_slots} 个 slot 身份均已被占用")


def _load_slot_config(config_dir: Path | str, slot_id: int) -> dict:
    """读 slot 配置原始 dict；缺失/损坏/非对象 → 空 dict（不落盘、不报错）。"""
    try:
        raw = slot_config_path(config_dir, slot_id).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logging.warning("overlay: slot 配置损坏，几何按未记录处理: %s",
                        slot_config_path(config_dir, slot_id))
        return {}
    return data if isinstance(data, dict) else {}


def _finite(value):
    """有限浮点（NaN/Inf/非数值 → None）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_geometry(geometry) -> dict:
    """只保留合法几何键（rx/ry 有限数、facing 枚举、scale 正有限数）。"""
    result: dict = {}
    if not isinstance(geometry, dict):
        return result
    for key in ("rx", "ry"):
        number = _finite(geometry.get(key))
        if number is not None:
            result[key] = number
    facing = geometry.get("facing")
    if facing in ("left", "right"):
        result["facing"] = facing
    scale = _finite(geometry.get("scale"))
    if scale is not None and scale > 0:
        result["scale"] = scale
    return result


def slot_config_exists(config_dir: Path | str, slot_id: int) -> bool:
    """slot 身份配置文件是否存在（复活前置：身份记忆还在才复活）。"""
    try:
        return slot_config_path(config_dir, slot_id).is_file()
    except OSError:
        return False


def read_slot_geometry(config_dir: Path | str, slot_id: int) -> dict:
    """读 slot 身份持久化的几何（rx/ry/facing/scale）；无记录/损坏 → 空 dict。"""
    return _clean_geometry(_load_slot_config(config_dir, slot_id))


def write_slot_geometry(config_dir: Path | str, slot_id: int, geometry) -> bool:
    """把几何键合并写回 slot 配置（保留该身份其它键；原子替换）。

    只覆盖几何键，绝不整文件重写：``config-slot-N.json`` 是该子肥鱼的完整
    配置身份（可能由 slot 落种/用户设置写就），几何只是其中一部分。
    """
    clean = _clean_geometry(geometry)
    if not clean:
        return False
    path = slot_config_path(config_dir, slot_id)
    data = _load_slot_config(config_dir, slot_id)
    data.setdefault("version", 4)
    data.update(clean)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        logging.warning("overlay: 写 slot 几何失败: %s", path, exc_info=True)
        return False
    return True
