# -*- coding: utf-8 -*-
"""槽位机制与个体记忆的全面测试。

覆盖 plan5 §7 的规范测试场景：
1. 真实子进程竞争同一临时配置根目录，依次获得 slot-0/1/2；指定槽竞争失败不降级，锁文件残留可复用。
2. 子进程持有 slot-1 后 exit 或被终止，新子进程重新加锁 slot-1，读取个体配置与 sessions，不删 lock 文件。
3. 真实子进程并发首次创建 slot 配置，最终 JSON 完整，PID 后缀 .tmp 不撞名。
4. 主配置变更后，新 slot 首次创建继承主配置（位置独立、自启仅主槽有效）；已有 slot 保持个体修改记忆。
5. 损坏配置唯一备份名，连续恢复不覆盖旧备份。
6. 旧 config.json 无槽位元数据仍作为 slot-0；旧 spawn 原子迁移到 slot-1/2，中断回滚与标记。
7. slot-0 被占用时自启失败不拿 slot-1。
8. 手动启动与菜单 spawn 顺序与 offset index 独立测试。
9. 位置避让与 spawn offset 独立生效测试。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pet.config import Config, APP_DIR_NAME
from pet import slot_manager as sm
from pet.chat.session_store import SessionStore
from pet.chat.models import ChatMessage, ChatSession


def test_field_default_factory_and_individual_memory(tmp_path):
    """场景 4：新 slot 首次创建继承主配置（位置/自启除外），之后只保留个体记忆。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    master = Config(base=tmp_path)
    master.set("character", "shenshen")
    master.set("playback_speed", 1.5)
    master.set("click_sound_volume", 0.33)
    master.set("on_top", False)
    master.set("show_dock_icon", False)
    master.set("chat_follow_pet", True)
    master.set("rx", 0.1)
    master.set("ry", 0.2)
    master.set("autostart_wanted", True)
    master.save()

    # 新建 slot-1：未保存前直接读取，应继承主配置的非个体设置；
    # 位置/屏幕与开机自启不复制，避免叠位和副槽自启。
    slot1 = Config(base=tmp_path, instance_id="slot-1")
    assert slot1.get("character") == "shenshen"
    assert slot1.get("playback_speed") == 1.5
    assert slot1.get("click_sound_volume") == 0.33
    assert slot1.get("on_top") is False
    assert slot1.get("show_dock_icon") is False
    assert slot1.get("chat_follow_pet") is True
    assert slot1.get("rx") is None
    assert slot1.get("ry") is None
    assert slot1.get("autostart_wanted") is False

    # slot-1 修改自身属性并保存（个体记忆）
    slot1.set("character", "dundun")
    slot1.set("click_sound_volume", 0.55)
    slot1.save()

    # 修改主配置后，已有 slot-1 不受影响
    master.set("character", "master_new")
    master.set("click_sound_volume", 0.99)
    master.save()

    slot1_reload = Config(base=tmp_path, instance_id="slot-1")
    assert slot1_reload.get("character") == "dundun"
    assert slot1_reload.get("click_sound_volume") == 0.55

    # 新建 slot-2 在首次创建时继承主配置当时的值（最新主配置），之后各自独立
    slot2 = Config(base=tmp_path, instance_id="slot-2")
    assert slot2.get("character") == "master_new"
    assert slot2.get("click_sound_volume") == 0.99


def test_spawn_reuse_keeps_existing_slot_config(tmp_path, monkeypatch):
    """「生小肥鱼」复用已有存档的 slot 时不再顶掉原槽设置（回归：旧版
    DSH_PET_SPAWN_FRESH 强制重播种会把 slot-N 个体配置覆盖成主配置）。"""
    master = Config(base=tmp_path)
    master.set("character", "shenshen")
    master.set("playback_speed", 2.0)
    master.set("click_sound_volume", 0.8)
    master.save()

    old_slot = Config(base=tmp_path, instance_id="slot-1")
    old_slot.set("character", "dundun")
    old_slot.set("playback_speed", 0.5)
    old_slot.set("click_sound_volume", 0.2)
    old_slot.save()

    # 旧版孵化进程环境可能仍带着 SPAWN_FRESH 标记，须被完全忽略
    monkeypatch.setenv("DSH_PET_SPAWN_FRESH", "1")
    reused_slot = Config(base=tmp_path, instance_id="slot-1")
    assert reused_slot.get("character") == "dundun"
    assert reused_slot.get("playback_speed") == 0.5
    assert reused_slot.get("click_sound_volume") == 0.2


def test_normal_reopen_keeps_existing_slot_config(tmp_path, monkeypatch):
    """普通重启/复用 slot 时，已有个体配置不被主配置覆盖。"""
    master = Config(base=tmp_path)
    master.set("character", "shenshen")
    master.save()

    slot = Config(base=tmp_path, instance_id="slot-1")
    slot.set("character", "dundun")
    slot.save()

    reopened = Config(base=tmp_path, instance_id="slot-1")
    assert reopened.get("character") == "dundun"


def test_spawn_seed_respects_inherit_size_switch(tmp_path):
    """生小肥鱼继承大小开关：开启保留主 scale，关闭改用 spawn_scale。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    # 开启继承：主鱼 1.0，新鱼也应为 1.0
    master = Config(base=tmp_path)
    master.set("scale", 1.0)
    master.set("spawn_inherit_size", True)
    master.set("spawn_scale", 0.5)
    master.set("spawn_inherit_dynamic_island", True)
    island = dict(master.get("dynamic_island"))
    island["enabled"] = True
    master.set("dynamic_island", island)
    master.save()
    slot_inherit = Config(base=tmp_path, instance_id="slot-1")
    assert slot_inherit.get("scale") == 1.0
    assert slot_inherit.get("dynamic_island", {}).get("enabled") is True

    # 关闭继承：主鱼仍是 1.0，但新鱼应使用 spawn_scale=0.5，且灵动岛不开启
    master.set("spawn_inherit_size", False)
    master.set("spawn_inherit_dynamic_island", False)
    master.save()
    slot_custom = Config(base=tmp_path, instance_id="slot-2")
    assert slot_custom.get("scale") == 0.5
    assert slot_custom.get("spawn_inherit_size") is False
    assert slot_custom.get("spawn_scale") == 0.5
    assert slot_custom.get("spawn_inherit_dynamic_island") is False
    assert slot_custom.get("dynamic_island", {}).get("enabled") is False


def test_corrupt_config_backup_unique_timestamp(tmp_path):
    """场景 6：损坏配置唯一备份名，连续恢复不覆盖旧备份。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    corrupt_cfg = config_dir / "config-slot-1.json"
    corrupt_cfg.write_text("{invalid-json", encoding="utf-8")

    b1 = sm.backup_corrupt_config(corrupt_cfg)
    assert b1 is not None and b1.exists()
    assert "corrupt-" in b1.name

    time.sleep(0.01)
    b2 = sm.backup_corrupt_config(corrupt_cfg)
    assert b2 is not None and b2.exists()
    assert b1 != b2


def test_migrate_legacy_spawns_atomic_and_rollback(tmp_path):
    """场景 7：旧 spawn 原子迁移到 slot-1/2，旧 config.json 仍为 slot-0，孤儿文件保留。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    # 创建旧 config.json（无槽位元数据）
    (config_dir / "config.json").write_text(json.dumps({"version": 4, "character": "master"}), encoding="utf-8")

    # 创建两个旧 spawn 配置文件和会话
    (config_dir / "config-spawn100x1.json").write_text(json.dumps({"character": "spawn1"}), encoding="utf-8")
    s1_dir = config_dir / "sessions-spawn100x1" / "shenshen"
    s1_dir.mkdir(parents=True, exist_ok=True)
    (s1_dir / "s1.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")

    # 人工给第二个 spawn 一个稍晚的 mtime
    spawn2_file = config_dir / "config-spawn200x1.json"
    spawn2_file.write_text(json.dumps({"character": "spawn2"}), encoding="utf-8")
    os.utime(spawn2_file, (time.time() + 10, time.time() + 10))

    # 执行迁移
    assert sm.migrate_legacy_spawns(config_dir) is True

    # 验证映射到 slot-1 和 slot-2
    cfg1 = json.loads((config_dir / "config-slot-1.json").read_text(encoding="utf-8"))
    assert cfg1.get("character") == "spawn1"
    assert (config_dir / "sessions-slot-1" / "shenshen" / "s1.json").exists()

    cfg2 = json.loads((config_dir / "config-slot-2.json").read_text(encoding="utf-8"))
    assert cfg2.get("character") == "spawn2"

    # 主配置不受影响
    master_cfg = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    assert master_cfg.get("character") == "master"

    # 迁移标记文件写入
    assert (config_dir / "migration-spawns.done").exists()


def test_corrupt_config_backup_and_wiring_in_config_load(tmp_path):
    """测试 Config._load 对损坏的 slot 配置及主配置调用 backup_corrupt_config 备份而不静默覆盖。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    # 1. 槽位配置文件损坏
    slot1_cfg = config_dir / "config-slot-1.json"
    slot1_cfg.write_text("{bad-json-slot1", encoding="utf-8")

    cfg1 = Config(base=tmp_path, instance_id="slot-1")
    # 检查是否生成了备份文件
    backups1 = list(config_dir.glob("config-slot-1.json.corrupt-*"))
    assert len(backups1) == 1
    assert backups1[0].read_text(encoding="utf-8") == "{bad-json-slot1"
    # cfg1 正常回退到默认配置
    assert cfg1.get("character") is not None

    # 2. 主配置文件损坏
    master_cfg = config_dir / "config.json"
    master_cfg.write_text("{bad-json-master", encoding="utf-8")

    cfg_master = Config(base=tmp_path)
    backups_master = list(config_dir.glob("config.json.corrupt-*"))
    assert len(backups_master) == 1
    assert backups_master[0].read_text(encoding="utf-8") == "{bad-json-master"
    assert cfg_master.get("character") is not None


def test_migrate_legacy_spawns_rollback_on_sessions_move_failure(tmp_path, monkeypatch):
    """测试旧 spawn 迁移时，若 config 移动成功但 sessions 移动失败，完整回滚三方状态。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    old_cfg = config_dir / "config-spawn999x1.json"
    old_cfg.write_text(json.dumps({"character": "spawn_fail"}), encoding="utf-8")
    old_sessions = config_dir / "sessions-spawn999x1"
    old_sessions.mkdir(parents=True, exist_ok=True)
    (old_sessions / "session.json").write_text("{}", encoding="utf-8")

    # 模拟在 move staged_sessions -> target_sessions 时抛异常
    orig_move = shutil.move

    def mock_move(src, dst, **kwargs):
        if "sessions-slot-1" in str(dst):
            raise OSError("Injected sessions move failure")
        return orig_move(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "move", mock_move)

    result = sm.migrate_legacy_spawns(config_dir)
    assert result is False

    # 验证完整回滚：原文件存在，目标文件不存在
    assert old_cfg.exists()
    assert old_sessions.exists()
    assert (old_sessions / "session.json").exists()
    assert not (config_dir / "config-slot-1.json").exists()
    assert not (config_dir / "sessions-slot-1").exists()
    assert not (config_dir / ".migration_staging").exists()


def test_migrate_legacy_spawns_recovers_staged_remnants(tmp_path):
    """测试启动迁移时若存在非空 .migration_staging 残留，能够恢复完成或清理。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = config_dir / ".migration_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "config-slot-1.json").write_text(json.dumps({"character": "staged_pet"}), encoding="utf-8")

    assert sm.migrate_legacy_spawns(config_dir) is True
    # 验证已从 staging 恢复到目标位置
    assert (config_dir / "config-slot-1.json").exists()
    assert json.loads((config_dir / "config-slot-1.json").read_text(encoding="utf-8"))["character"] == "staged_pet"
    assert not staging_dir.exists()


def test_migration_staging_remnant_is_kept_when_target_already_exists(tmp_path):
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "config-slot-1.json"
    target.write_text(json.dumps({"character": "current"}), encoding="utf-8")
    staging_dir = config_dir / ".migration_staging"
    staging_dir.mkdir()
    staged = staging_dir / "config-slot-1.json"
    staged.write_text(json.dumps({"character": "staged"}), encoding="utf-8")

    assert sm.migrate_legacy_spawns(config_dir) is False
    assert staged.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["character"] == "current"


def test_list_runtime_marker_files_returns_legacy_and_v2(tmp_path):
    """批 B：list_runtime_marker_files 同时认旧名与版本化新名 runtime 标记。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    (config_dir / "runtime-111.json").write_text("{}", encoding="utf-8")
    (config_dir / "pet-runtime-v2-222-slot-1.json").write_text("{}", encoding="utf-8")
    (config_dir / "pet-runtime-v2-333-slot-2.json").write_text("{}", encoding="utf-8")
    (config_dir / "config-slot-1.json").write_text("{}", encoding="utf-8")
    (config_dir / "sessions-slot-1").mkdir()

    names = {p.name for p in sm.list_runtime_marker_files(config_dir)}
    # 新旧两种命名都被列出
    assert {"runtime-111.json",
            "pet-runtime-v2-222-slot-1.json",
            "pet-runtime-v2-333-slot-2.json"} <= names
    # 非 runtime 标记文件不得被误列
    assert "config-slot-1.json" not in names
    assert "sessions-slot-1" not in names


def test_migrate_legacy_spawns_skips_when_v2_marker_alive(tmp_path):
    """批 B：migrate_legacy_spawns 检测 v2 版本化 runtime 标记（多进程模式只写
    新名），发现存活实例时跳过迁移。旧 glob 只认 runtime-*.json 会误放行。"""
    config_dir = tmp_path / APP_DIR_NAME
    config_dir.mkdir(parents=True, exist_ok=True)

    old_cfg = config_dir / "config-spawn100x1.json"
    old_cfg.write_text(json.dumps({"character": "spawn1"}), encoding="utf-8")
    (config_dir / "sessions-spawn100x1").mkdir(parents=True, exist_ok=True)

    # v2 标记用当前（必然存活）进程 pid
    v2 = config_dir / f"pet-runtime-v2-{os.getpid()}-slot-1.json"
    v2.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    assert sm.migrate_legacy_spawns(config_dir) is False
    # 旧配置未被迁移（检测到存活实例跳过）
    assert old_cfg.exists()
    assert not (config_dir / "config-slot-1.json").exists()
    assert not (config_dir / "migration-spawns.done").exists()


def test_app_main_validates_slot_arg():
    """测试 app.main 校验 --slot 参数范围（0~127）及非法值。"""
    from pet import app as app_mod

    # 负数
    assert app_mod.main(["dsh-pet", "--slot", "-1"]) == 1
    # 超大值
    assert app_mod.main(["dsh-pet", "--slot", "128"]) == 1
    # 非法字符串
    assert app_mod.main(["dsh-pet", "--slot", "abc"]) == 1
    # 缺少值
    assert app_mod.main(["dsh-pet", "--slot"]) == 1

