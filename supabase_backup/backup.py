#!/usr/bin/env python3
"""ximalaya_manager B2 定时备份脚本。

功能:
  1. SSH 到 VPS, pg_dump 导出完整数据库 (gzip 压缩)
  2. 下载 .env + docker-compose.yml 配置文件
  3. 上传所有文件到 Backblaze B2 (时间戳文件夹)
  4. 自动清理旧备份

用法:
  python backup.py backup           # 执行一次完整备份
  python backup.py daemon           # 定时守护进程 (默认 24 小时一次)
  python backup.py daemon -i 12     # 每 12 小时备份一次
  python backup.py list             # 列出 B2 中的备份
  python backup.py clean [--keep 7] # 清理旧备份, 保留最近 N 份

定时任务:
  Windows Task Scheduler:
    schtasks /create /tn "XmB2Backup" /tr "python H:\\2026_main_project\\ximalaya_manager\\supabase_backup\\backup.py backup" /sc daily /st 03:00
  Linux crontab:
    0 3 * * * cd /opt/ximalaya_manager && python3 supabase_backup/backup.py backup >> /var/log/xm_b2_backup.log 2>&1
"""

import sys
import os
import time
import argparse
from datetime import datetime

# 确保能 import common
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    get_vps_host, get_vps_container, get_vps_db_user, get_vps_db_name,
    get_vps_project_dir, get_b2_bucket, get_backup_keep, get_backup_interval_hours,
    check_b2_config, check_vps_config,
    ssh_connect, run_remote, sftp_read,
    ensure_bucket, upload_to_b2, list_backup_folders, delete_backup_folder,
    list_backup_files, list_b2_objects,
    timestamp_str, format_size,
)


# ═══════════════════════════════════════════
# 备份核心流程
# ═══════════════════════════════════════════

def do_backup() -> bool:
    """执行一次完整备份。"""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not check_b2_config():
        return False
    if not check_vps_config():
        return False

    vps_host = get_vps_host()
    vps_container = get_vps_container()
    vps_db_user = get_vps_db_user()
    vps_db_name = get_vps_db_name()
    vps_project_dir = get_vps_project_dir()
    b2_bucket = get_b2_bucket()
    backup_keep = get_backup_keep()

    remote_dump_gz = "/tmp/xm_b2_dump.sql.gz"
    remote_env = f"{vps_project_dir}/.env"
    remote_compose = f"{vps_project_dir}/docker-compose.yml"

    ts = timestamp_str()
    print(f"[{ts}] === 开始 B2 备份 ===", flush=True)
    print(f"  VPS: {vps_host}", flush=True)
    print(f"  B2 Bucket: {b2_bucket}", flush=True)
    print(flush=True)

    # ── 确保 bucket 可访问 ──
    if not ensure_bucket():
        return False

    # ── 连接 VPS ──
    print("连接 SSH ...", flush=True)
    try:
        ssh = ssh_connect()
    except Exception as e:
        print(f"FAIL: SSH 连接失败: {e}", flush=True)
        return False
    print("SSH 连接成功", flush=True)

    # ── Step 1: pg_dump 导出数据库 ──
    print("\n=== 1/3 导出数据库 ===", flush=True)
    dump_cmd = (
        f"docker exec {vps_container} "
        f"pg_dump -U {vps_db_user} -d {vps_db_name} "
        f"--no-owner --no-privileges --clean --if-exists "
        f"| gzip > {remote_dump_gz}"
    )
    exit_code, out, err = run_remote(ssh, dump_cmd, timeout=300)
    if exit_code != 0:
        print(f"  FAIL: pg_dump 退出码 {exit_code}", flush=True)
        if err:
            print(f"  stderr: {err[:300]}", flush=True)
        ssh.close()
        return False

    _, size_out, _ = run_remote(ssh, f"stat -c%s {remote_dump_gz} 2>/dev/null || echo 0")
    dump_bytes_remote = int(size_out) if size_out.strip().isdigit() else 0
    print(f"  OK: 远程 dump {format_size(dump_bytes_remote)}", flush=True)

    # 下载 dump
    print("  下载中 ...", flush=True)
    dump_gz = sftp_read(ssh, remote_dump_gz)
    print(f"  OK: 已下载 ({format_size(len(dump_gz))})", flush=True)

    # 清理远程临时文件
    run_remote(ssh, f"rm -f {remote_dump_gz}")

    # ── Step 2: 下载配置文件 ──
    print("\n=== 2/3 下载配置文件 ===", flush=True)
    config_files = {}
    for name, remote_path in [(".env", remote_env), ("docker-compose.yml", remote_compose)]:
        try:
            data = sftp_read(ssh, remote_path)
            config_files[name] = data
            print(f"  OK: {name} ({format_size(len(data))})", flush=True)
        except Exception as e:
            print(f"  WARN: 下载 {name} 失败: {e}", flush=True)

    ssh.close()

    # ── Step 3: 上传到 B2 ──
    print("\n=== 3/3 上传到 Backblaze B2 ===", flush=True)
    storage_prefix = ts
    all_uploaded = True

    if upload_to_b2(f"{storage_prefix}/db_dump.sql.gz", dump_gz, "application/gzip"):
        print(f"  OK: db_dump.sql.gz ({format_size(len(dump_gz))})", flush=True)
    else:
        all_uploaded = False

    for name, data in config_files.items():
        content_type = "text/plain" if name == ".env" else "text/yaml"
        if upload_to_b2(f"{storage_prefix}/{name}", data, content_type):
            print(f"  OK: {name} ({format_size(len(data))})", flush=True)
        else:
            all_uploaded = False

    if not all_uploaded:
        print("  WARN: 部分文件上传失败", flush=True)

    # ── 清理旧备份 ──
    print(f"\n=== 清理旧备份 (保留最近 {backup_keep} 份) ===", flush=True)
    deleted = clean_old_backups(backup_keep)
    if deleted:
        for d in deleted:
            print(f"  删除: {d}", flush=True)
    else:
        print(f"  无需清理", flush=True)

    # ── 汇总 ──
    print(f"\n=== 备份完成 ===", flush=True)
    print(f"  时间戳: {ts}", flush=True)
    print(f"  B2 路径: {b2_bucket}/{storage_prefix}/", flush=True)
    print(f"  数据库 dump: {format_size(len(dump_gz))}", flush=True)
    if config_files:
        print(f"  配置文件: {', '.join(config_files.keys())}", flush=True)
    print(f"\nDone!", flush=True)
    return True


# ═══════════════════════════════════════════
# 定时守护进程
# ═══════════════════════════════════════════

def daemon_loop(interval_hours: float):
    """定时守护进程：每隔 interval_hours 执行一次备份。"""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    interval_seconds = interval_hours * 3600
    print(f"=== B2 备份守护进程启动 ===", flush=True)
    print(f"  备份间隔: {interval_hours} 小时", flush=True)
    print(f"  按 Ctrl+C 停止", flush=True)
    print(flush=True)

    while True:
        try:
            do_backup()
        except Exception as e:
            print(f"\nERROR: 备份异常: {e}", flush=True)

        next_time = datetime.now().strftime("%H:%M:%S")
        print(f"\n下次备份: {interval_hours} 小时后 (当前 {next_time})", flush=True)
        print("-" * 50, flush=True)

        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\n守护进程已停止", flush=True)
            break


# ═══════════════════════════════════════════
# 列出备份
# ═══════════════════════════════════════════

def list_backups():
    """列出 B2 中的所有备份。"""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not check_b2_config():
        return

    print(f"=== B2 备份列表 ({get_b2_bucket()}) ===\n", flush=True)

    folders = list_backup_folders()
    if not folders:
        print("  (空) 没有备份", flush=True)
        return

    for folder in folders:
        objs = list_b2_objects(prefix=f"{folder}/")
        print(f"  {folder}", flush=True)
        for obj in objs:
            fname = obj["key"].split("/")[-1]
            print(f"    - {fname} ({format_size(obj['size'])})", flush=True)
        print(flush=True)

    print(f"共 {len(folders)} 份备份", flush=True)


# ═══════════════════════════════════════════
# 清理旧备份
# ═══════════════════════════════════════════

def clean_old_backups(keep: int) -> list[str]:
    """清理旧备份，保留最近 keep 份，返回已删除的文件夹名列表。"""
    if not check_b2_config():
        return []

    folders = list_backup_folders()
    if len(folders) <= keep:
        return []

    to_delete = folders[keep:]
    for folder in to_delete:
        delete_backup_folder(folder)

    return to_delete


def do_clean(keep: int):
    """执行清理操作。"""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"=== 清理旧备份 (保留最近 {keep} 份) ===\n", flush=True)
    deleted = clean_old_backups(keep)
    if deleted:
        for d in deleted:
            print(f"  已删除: {d}", flush=True)
        print(f"\n共删除 {len(deleted)} 份旧备份", flush=True)
    else:
        print("  无需清理", flush=True)


# ═══════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="ximalaya_manager B2 定时备份",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python backup.py backup           # 执行一次完整备份
  python backup.py daemon           # 定时守护进程 (默认 24h)
  python backup.py daemon -i 12     # 每 12 小时备份一次
  python backup.py list             # 列出所有备份
  python backup.py clean --keep 7   # 清理旧备份, 保留 7 份
        """,
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    sub.add_parser("backup", help="执行一次完整备份")

    p_daemon = sub.add_parser("daemon", help="定时守护进程")
    p_daemon.add_argument("-i", "--interval", type=float, default=None, help=f"备份间隔小时 (默认 {get_backup_interval_hours()})")

    sub.add_parser("list", help="列出 B2 中的备份")

    p_clean = sub.add_parser("clean", help="清理旧备份")
    p_clean.add_argument("--keep", type=int, default=None, help=f"保留备份数 (默认 {get_backup_keep()})")

    args = parser.parse_args()

    if args.command == "backup":
        ok = do_backup()
        sys.exit(0 if ok else 1)

    elif args.command == "daemon":
        interval = args.interval if args.interval is not None else get_backup_interval_hours()
        daemon_loop(interval)

    elif args.command == "list":
        list_backups()

    elif args.command == "clean":
        keep = args.keep if args.keep is not None else get_backup_keep()
        do_clean(keep)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
