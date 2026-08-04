#!/usr/bin/env python3
"""ximalaya_manager B2 备份恢复脚本。

从 Backblaze B2 下载备份并恢复到 VPS PostgreSQL。

用法:
  python restore.py list                    # 列出所有可用备份
  python restore.py restore                 # 恢复最新备份
  python restore.py restore 20260802_030000 # 恢复指定备份
  python restore.py restore --db-only       # 仅恢复数据库
  python restore.py restore --config-only   # 仅恢复配置文件
  python restore.py download 20260802_030000 # 仅下载备份到本地
"""

import sys
import os
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    get_vps_container, get_vps_db_user, get_vps_db_name,
    get_vps_project_dir, get_b2_bucket,
    check_b2_config, check_vps_config,
    ssh_connect, run_remote, sftp_read, sftp_write,
    download_from_b2, list_backup_folders, list_backup_files,
    list_b2_objects,
    timestamp_str, format_size,
)

LOCAL_DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"


# ═══════════════════════════════════════════
# 列出备份
# ═══════════════════════════════════════════

def cmd_list():
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

    for idx, folder in enumerate(folders):
        objs = list_b2_objects(prefix=f"{folder}/")
        tag = " (最新)" if idx == 0 else ""
        print(f"  [{idx}] {folder}{tag}", flush=True)
        for obj in objs:
            fname = obj["key"].split("/")[-1]
            print(f"      - {fname} ({format_size(obj['size'])})", flush=True)
        print(flush=True)

    print(f"共 {len(folders)} 份备份", flush=True)
    print(f"\n恢复命令:", flush=True)
    print(f"  python restore.py restore              # 恢复最新备份", flush=True)
    print(f"  python restore.py restore {folders[0]} # 恢复指定备份", flush=True)


# ═══════════════════════════════════════════
# 下载备份
# ═══════════════════════════════════════════

def download_backup(folder: str, local_dir: Path = None) -> dict[str, Path]:
    """从 B2 下载指定备份文件夹的所有文件。"""
    if local_dir is None:
        local_dir = LOCAL_DOWNLOAD_DIR / folder
    local_dir.mkdir(parents=True, exist_ok=True)

    files = list_backup_files(folder)
    if not files:
        print(f"  ERROR: 备份 {folder} 中没有文件", flush=True)
        return {}

    downloaded = {}
    for filename in files:
        print(f"  下载 {filename} ...", flush=True, end=" ")
        data = download_from_b2(f"{folder}/{filename}")
        if data is not None:
            local_path = local_dir / filename
            local_path.write_bytes(data)
            downloaded[filename] = local_path
            print(f"OK ({format_size(len(data))})", flush=True)
        else:
            print("FAIL", flush=True)

    return downloaded


def cmd_download(folder: str):
    """仅下载备份到本地目录。"""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not check_b2_config():
        return

    print(f"=== 下载备份 {folder} ===\n", flush=True)
    downloaded = download_backup(folder)
    if downloaded:
        print(f"\n下载完成:", flush=True)
        for name, path in downloaded.items():
            print(f"  {name}: {path}", flush=True)


# ═══════════════════════════════════════════
# 恢复数据库到 VPS
# ═══════════════════════════════════════════

def restore_database_to_vps(dump_bytes: bytes, is_gz: bool = False) -> bool:
    """将 SQL dump 恢复到 VPS PostgreSQL。

    Args:
        dump_bytes: dump 文件内容
        is_gz: 是否为 gzip 压缩文件
    """
    print("\n--- 恢复数据库到 VPS ---", flush=True)

    vps_container = get_vps_container()
    vps_db_user = get_vps_db_user()
    vps_db_name = get_vps_db_name()

    try:
        ssh = ssh_connect()
    except Exception as e:
        print(f"  FAIL: SSH 连接失败: {e}", flush=True)
        return False

    try:
        remote_restore_sql = "/tmp/xm_restore_dump.sql"

        if is_gz:
            remote_gz = "/tmp/xm_restore_dump.sql.gz"
            print(f"  上传 dump ({format_size(len(dump_bytes))}) ...", flush=True)
            sftp_write(ssh, remote_gz, dump_bytes)
            print("  解压 ...", flush=True)
            exit_code, _, err = run_remote(ssh, f"gunzip -f {remote_gz}", timeout=120)
            if exit_code != 0:
                print(f"  FAIL: gunzip 失败: {err}", flush=True)
                return False
        else:
            print(f"  上传 dump ({format_size(len(dump_bytes))}) ...", flush=True)
            sftp_write(ssh, remote_restore_sql, dump_bytes)

        print("  执行 psql 恢复 ...", flush=True)
        restore_cmd = (
            f"docker exec -i {vps_container} "
            f"psql -U {vps_db_user} -d {vps_db_name} "
            f"< {remote_restore_sql}"
        )
        exit_code, out, err = run_remote(ssh, restore_cmd, timeout=600)
        if exit_code != 0:
            print(f"  WARN: psql 退出码 {exit_code}", flush=True)
            if err:
                lines = err.strip().split("\n")
                error_lines = [l for l in lines if "ERROR" in l.upper()]
                if error_lines:
                    print(f"  错误信息:", flush=True)
                    for l in error_lines[:5]:
                        print(f"    {l}", flush=True)
                else:
                    print(f"  (非致命警告，恢复可能已成功)", flush=True)
        else:
            print("  OK: 数据库恢复成功", flush=True)

        run_remote(ssh, f"rm -f {remote_restore_sql} {remote_restore_sql}.gz")

        print("\n  验证数据:", flush=True)
        tables = ["books", "audiobook_chapters", "global_settings", "xm_jobs", "xm_worker_stats", "xm_scrape_tasks"]
        for table in tables:
            _, count, _ = run_remote(
                ssh,
                f"docker exec {vps_container} psql -U {vps_db_user} -d {vps_db_name} -t -c \"SELECT count(*) FROM {table};\"",
                timeout=30,
            )
            count = count.strip() if count else "?"
            print(f"    {table}: {count} 行", flush=True)

        return True

    finally:
        ssh.close()


# ═══════════════════════════════════════════
# 恢复配置文件到 VPS
# ═══════════════════════════════════════════

def restore_config_to_vps(env_bytes: bytes | None, compose_bytes: bytes | None) -> bool:
    """恢复 .env 和 docker-compose.yml 到 VPS。"""
    print("\n--- 恢复配置文件到 VPS ---", flush=True)

    vps_project_dir = get_vps_project_dir()
    remote_env = f"{vps_project_dir}/.env"
    remote_compose = f"{vps_project_dir}/docker-compose.yml"

    try:
        ssh = ssh_connect()
    except Exception as e:
        print(f"  FAIL: SSH 连接失败: {e}", flush=True)
        return False

    try:
        backup_ts = timestamp_str()
        if env_bytes is not None:
            run_remote(ssh, f"cp {remote_env} {remote_env}.bak_{backup_ts} 2>/dev/null; true")
            sftp_write(ssh, remote_env, env_bytes)
            print(f"  OK: .env 已恢复 (旧文件备份为 .env.bak_{backup_ts})", flush=True)

        if compose_bytes is not None:
            run_remote(ssh, f"cp {remote_compose} {remote_compose}.bak_{backup_ts} 2>/dev/null; true")
            sftp_write(ssh, remote_compose, compose_bytes)
            print(f"  OK: docker-compose.yml 已恢复 (旧文件备份为 .bak_{backup_ts})", flush=True)

        if env_bytes is not None or compose_bytes is not None:
            print(f"\n  请重启服务使配置生效:", flush=True)
            print(f"    cd {vps_project_dir} && docker compose up -d --build web", flush=True)

        return True

    finally:
        ssh.close()


# ═══════════════════════════════════════════
# 完整恢复流程
# ═══════════════════════════════════════════

def cmd_restore(folder: str = None, db_only: bool = False, config_only: bool = False):
    """从 B2 恢复备份到 VPS。"""
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not check_b2_config():
        return
    if not check_vps_config():
        return

    if folder is None:
        folders = list_backup_folders()
        if not folders:
            print("ERROR: B2 中没有备份", flush=True)
            return
        folder = folders[0]
        print(f"=== 恢复最新备份: {folder} ===\n", flush=True)
    else:
        folders = list_backup_folders()
        if folder not in folders:
            print(f"ERROR: 备份 {folder} 不存在", flush=True)
            print(f"可用备份: {', '.join(folders)}", flush=True)
            return
        print(f"=== 恢复备份: {folder} ===\n", flush=True)

    print("从 B2 下载备份 ...", flush=True)
    downloaded = download_backup(folder)
    if not downloaded:
        print("ERROR: 下载失败", flush=True)
        return

    dump_bytes = None
    dump_is_gz = False
    env_bytes = None
    compose_bytes = None

    if "db_dump.sql" in downloaded:
        dump_bytes = downloaded["db_dump.sql"].read_bytes()
    elif "db_dump.sql.gz" in downloaded:
        dump_bytes = downloaded["db_dump.sql.gz"].read_bytes()
        dump_is_gz = True
    if ".env" in downloaded:
        env_bytes = downloaded[".env"].read_bytes()
    if "docker-compose.yml" in downloaded:
        compose_bytes = downloaded["docker-compose.yml"].read_bytes()

    if db_only:
        if dump_bytes:
            restore_database_to_vps(dump_bytes, is_gz=dump_is_gz)
        else:
            print("ERROR: 备份中没有 db_dump.sql 或 db_dump.sql.gz", flush=True)
    elif config_only:
        restore_config_to_vps(env_bytes, compose_bytes)
    else:
        if dump_bytes:
            restore_database_to_vps(dump_bytes, is_gz=dump_is_gz)
        else:
            print("WARN: 备份中没有 db_dump.sql 或 db_dump.sql.gz，跳过数据库恢复", flush=True)

        if env_bytes or compose_bytes:
            restore_config_to_vps(env_bytes, compose_bytes)
        else:
            print("\n(无配置文件可恢复)", flush=True)

    print(f"\n=== 恢复完成 ===", flush=True)
    print(f"  备份: {folder}", flush=True)
    print(f"\nDone!", flush=True)


# ═══════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="ximalaya_manager B2 备份恢复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python restore.py list                      # 列出所有备份
  python restore.py restore                   # 恢复最新备份 (数据库+配置)
  python restore.py restore 20260802_030000   # 恢复指定备份
  python restore.py restore --db-only         # 仅恢复数据库
  python restore.py restore --config-only     # 仅恢复配置文件
  python restore.py download 20260802_030000  # 仅下载到本地
        """,
    )

    sub = parser.add_subparsers(dest="command", help="子命令")

    sub.add_parser("list", help="列出所有可用备份")

    p_restore = sub.add_parser("restore", help="恢复备份到 VPS")
    p_restore.add_argument("folder", nargs="?", default=None, help="备份文件夹名 (时间戳), 默认最新")
    p_restore.add_argument("--db-only", action="store_true", help="仅恢复数据库")
    p_restore.add_argument("--config-only", action="store_true", help="仅恢复配置文件")

    p_download = sub.add_parser("download", help="仅下载备份到本地")
    p_download.add_argument("folder", help="备份文件夹名 (时间戳)")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list()

    elif args.command == "restore":
        if args.db_only and args.config_only:
            print("ERROR: --db-only 和 --config-only 不能同时使用", flush=True)
            sys.exit(1)
        cmd_restore(folder=args.folder, db_only=args.db_only, config_only=args.config_only)

    elif args.command == "download":
        cmd_download(args.folder)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
