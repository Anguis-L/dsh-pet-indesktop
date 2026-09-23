# -*- coding: utf-8 -*-
"""4.2b 纯逻辑单测：活跃宠清单（D5）+ 无锁 slot 身份分配（D6）+ 每身份几何。

零 Qt：本文件只 import ``pet.overlay_spawn_state``（纯逻辑模块），不起
QApplication。覆盖：清单原子读写/顺序保持、缺失=首次运行、损坏或缺首字段
回退「只有主宠」、身份分配单调不撞且跳过既有配置与活跃身份、分配上限报错、
几何合并写回（保留身份其它键）、非法几何丢弃。
"""
from __future__ import annotations

import json

import pytest

from pet import overlay_spawn_state as ovs


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 活跃宠清单（D5）
def test_active_list_roundtrip_keeps_spawn_order(tmp_path):
    assert ovs.save_active_slots(tmp_path, [3, 1, 2]) is True
    assert ovs.load_active_slots(tmp_path) == [3, 1, 2]
    data = json.loads(ovs.active_pets_path(tmp_path).read_text(encoding="utf-8"))
    assert data == {"version": ovs.ACTIVE_PETS_VERSION, "slots": [3, 1, 2]}


def test_active_list_write_is_atomic(tmp_path):
    ovs.save_active_slots(tmp_path, [1])
    ovs.write_slot_geometry(tmp_path, 1, {"rx": 0.5, "ry": 0.5, "scale": 1.0})
    assert list(tmp_path.glob("*.tmp")) == []


def test_active_list_missing_file_is_first_run(tmp_path):
    assert ovs.load_active_slots(tmp_path) == []


@pytest.mark.parametrize("text", [
    "{ not json",
    "[]",
    '"slots"',
    '{"slots": [1, 2]}',                        # 缺首字段 version
    '{"version": 2, "slots": [1]}',             # 版本不匹配
    '{"version": 1, "slots": {"a": 1}}',        # slots 非列表
])
def test_active_list_corrupt_falls_back_to_main_only(tmp_path, text):
    _write(ovs.active_pets_path(tmp_path), text)
    assert ovs.load_active_slots(tmp_path) == []


def test_active_list_drops_illegal_entries(tmp_path):
    _write(ovs.active_pets_path(tmp_path),
           json.dumps({"version": 1, "slots": [1, 0, -2, "2", None, 1, "x"]}))
    assert ovs.load_active_slots(tmp_path) == [1, 2]


def test_normalize_slots_dedupes_preserving_order():
    assert ovs.normalize_slots([2, 1, 2, "3", 0, -1, "x"]) == [2, 1, 3]
    assert ovs.normalize_slots(None) == []


# ---------------------------------------------------------------- 无锁 slot 分配（D6）
def test_allocate_slot_is_monotonic_and_never_collides(tmp_path):
    assert ovs.allocate_slot(tmp_path) == 1
    ovs.write_slot_geometry(tmp_path, 1, {"rx": 0.1, "ry": 0.1})
    assert ovs.allocate_slot(tmp_path) == 2
    ovs.write_slot_geometry(tmp_path, 2, {"rx": 0.2, "ry": 0.2})
    assert ovs.allocate_slot(tmp_path) == 3


def test_allocate_slot_skips_existing_configs_and_active_identities(tmp_path):
    _write(tmp_path / "config-slot-1.json", "{}")
    _write(tmp_path / "config-slot-2.json", "{}")
    # 活跃身份 3 尚未落配置（进程内已占）——同样不得撞
    assert ovs.allocate_slot(tmp_path, active_slots=[3]) == 4
    assert ovs.allocate_slot(tmp_path, active_slots=[4]) == 3


def test_allocate_slot_without_config_dir_uses_active_only():
    assert ovs.allocate_slot(None, active_slots=[1, 2]) == 3


def test_scan_slot_configs_matches_frozen_naming(tmp_path):
    _write(tmp_path / "config-slot-7.json", "{}")
    _write(tmp_path / "config-slot-x.json", "{}")       # 非数字后缀不认
    _write(tmp_path / "config.json", "{}")
    _write(tmp_path / "overlay-active-pets.json", "{}")
    assert ovs.scan_slot_configs(tmp_path) == {7}
    assert ovs.scan_slot_configs(None) == set()


def test_allocate_slot_raises_when_exhausted(tmp_path):
    for slot in (1, 2, 3):
        _write(tmp_path / f"config-slot-{slot}.json", "{}")
    with pytest.raises(ovs.SpawnStateError):
        ovs.allocate_slot(tmp_path, max_slots=3)


# ---------------------------------------------------------------- 每身份几何
def test_slot_geometry_roundtrip_preserves_other_keys(tmp_path):
    _write(tmp_path / "config-slot-1.json",
           json.dumps({"version": 4, "character": "nova", "user_customized": True}))
    assert ovs.write_slot_geometry(
        tmp_path, 1, {"rx": 0.25, "ry": 0.5, "facing": "right", "scale": 0.9}) is True
    assert ovs.read_slot_geometry(tmp_path, 1) == {
        "rx": 0.25, "ry": 0.5, "facing": "right", "scale": 0.9}
    raw = json.loads((tmp_path / "config-slot-1.json").read_text(encoding="utf-8"))
    assert raw["character"] == "nova"              # 身份其它键一个不丢
    assert raw["user_customized"] is True


def test_slot_geometry_missing_or_corrupt_is_empty(tmp_path):
    assert ovs.read_slot_geometry(tmp_path, 9) == {}
    _write(tmp_path / "config-slot-9.json", "{ broken")
    assert ovs.read_slot_geometry(tmp_path, 9) == {}
    assert ovs.slot_config_exists(tmp_path, 9) is True
    assert ovs.slot_config_exists(tmp_path, 10) is False


def test_slot_geometry_drops_illegal_values(tmp_path):
    _write(tmp_path / "config-slot-1.json",
           '{"version": 4, "rx": NaN, "ry": Infinity, "facing": "up", "scale": -1}')
    assert ovs.read_slot_geometry(tmp_path, 1) == {}


def test_slot_geometry_empty_write_is_rejected(tmp_path):
    assert ovs.write_slot_geometry(tmp_path, 1, {"rx": None, "facing": "up"}) is False
    assert ovs.slot_config_exists(tmp_path, 1) is False
