#!/usr/bin/env python3
"""ximalaya_manager VPS 定时备份脚本。

用法:
    python backup/backup_vps.py [--keep N] [--no-cleanup]

功能:
    1. SSH 进 VPS, pg_dump | gzip 导出压缩数据库
    2. 下载 .env + docker-compose.yml
    3. 保存到时间戳命名的文件夹 backup/archives/xm_backup_{timestamp}/
    4. 自动清理旧备份 (默认保留最近 7 份)

依赖:
    pip install paramiko

定时任务 (Windows Task Scheduler):
    schtasks /create /tn "XimalayaBackup" /tr "python H:\\2026_main_project\\ximalaya_manager\\backup\\backup_vps.py" /sc daily /st 03:00

定时任务 (Linux crontab):
    0 3 * * * cd /opt/ximalaya_manager && python3 backup/backup_vps.py >> /var/log/xm_backup.log 2>&1
"""

import paramiko
import sys
import os
import shutil
import argparse
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════
# VPS 连接配置 (与 deploy_redeploy.py 一致)
# ═══════════════════════════════════════════
VPS_HOST = "117.55.234.219"
VPS_PORT = 22
VPS_USER = "root"
VPS_PASS = "vrlcLlU5if"

PROJECT_DIR = "/opt/ximalaya_manager"
CONTAINER_NAME = "xm_postgres"
DB_USER = "xm_app"
DB_NAME = "ximalaya"

# 备份保留份数
DEFAULT_KEEP = 7

# 本地备份根目录 (脚本所在目录下的 archives/)
BACKUP_ROOT = Path(__file__).resolve().parent / "archives"

# VPS 上的临时导出路径
REMOTE_DUMP_GZ = "/tmp/xm_dump.sql.gz"
REMOTE_ENV = f"{PROJECT_DIR}/.env"
REMOTE_COMPOSE = f"{PROJECT_DIR}/docker-compose.yml"
# ═══════════════════════════════════════════


def run_remote(ssh, cmd, timeout=120):
    """执行远程命令, 返回 (exit_code, stdout, stderr)。"""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return exit_code, out, err


def sftp_download(ssh, remote_path, local_path):
    """通过 SFTP 下载文件。"""
    sftp = ssh.open_sftp()
    try:
        sftp.get(remote_path, str(local_path))
    finally:
        sftp.close()


def cleanup_old_backups(keep):
    """清理旧备份, 只保留最近 keep 份文件夹。"""
    if not BACKUP_ROOT.exists():
        return []

    dirs = sorted(
        [d for d in BACKUP_ROOT.iterdir() if d.is_dir() and d.name.startswith("xm_backup_")],
        reverse=True,
    )

    deleted = []
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)
        deleted.append(d.name)
    return deleted


def main():
    parser = argparse.ArgumentParser(description="ximalaya_manager VPS 备份")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"保留备份数 (默认 {DEFAULT_KEEP})")
    parser.add_argument("--no-cleanup", action="store_true", help="不清理旧备份")
    args = parser.parse_args()

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

    # 时间戳备份文件夹 (直接保存, 不打包)
    backup_dir = BACKUP_ROOT / f"xm_backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{timestamp}] 开始备份 ximalaya_manager", flush=True)
    print(f"  VPS: {VPS_HOST}", flush=True)
    print(f"  备份目录: {backup_dir}", flush=True)
    print(flush=True)

    # ── 连接 VPS ──
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print("连接 SSH ...", flush=True)
    ssh.connect(VPS_HOST, port=VPS_PORT, username=VPS_USER, password=VPS_PASS, timeout=15)
    print("SSH 连接成功", flush=True)

    # ── Step 1: pg_dump | gzip 导出数据库 ──
    print("\n=== 1/3 导出数据库 (gzip 压缩) ===", flush=True)
    dump_cmd = (
        f"docker exec {CONTAINER_NAME} "
        f"pg_dump -U {DB_USER} -d {DB_NAME} --no-owner --no-privileges"
        f" | gzip > {REMOTE_DUMP_GZ}"
    )
    exit_code, out, err = run_remote(ssh, dump_cmd, timeout=300)
    if exit_code != 0:
        print(f"  FAIL: pg_dump 退出码 {exit_code}", flush=True)
        print(f"  stderr: {err}", flush=True)
        ssh.close()
        sys.exit(1)

    _, size_out, _ = run_remote(ssh, f"stat -c%s {REMOTE_DUMP_GZ} 2>/dev/null || echo 0")
    dump_bytes = int(size_out) if size_out.strip().isdigit() else 0
    print(f"  OK: 远程压缩 dump {dump_bytes / 1024:.0f} KB", flush=True)

    local_dump_gz = backup_dir / "db_dump.sql.gz"
    print(f"  下载中 ...", flush=True)
    sftp_download(ssh, REMOTE_DUMP_GZ, local_dump_gz)
    print(f"  OK: {local_dump_gz.name} ({local_dump_gz.stat().st_size / 1024:.0f} KB)", flush=True)

    run_remote(ssh, f"rm -f {REMOTE_DUMP_GZ}")

    # ── Step 2: 下载 .env ──
    print("\n=== 2/3 下载 .env ===", flush=True)
    local_env = backup_dir / ".env"
    try:
        sftp_download(ssh, REMOTE_ENV, local_env)
        print(f"  OK: .env ({local_env.stat().st_size} bytes)", flush=True)
    except Exception as e:
        print(f"  WARN: 下载 .env 失败: {e}", flush=True)

    # ── Step 3: 下载 docker-compose.yml ──
    print("\n=== 3/3 下载 docker-compose.yml ===", flush=True)
    local_compose = backup_dir / "docker-compose.yml"
    try:
        sftp_download(ssh, REMOTE_COMPOSE, local_compose)
        print(f"  OK: docker-compose.yml", flush=True)
    except Exception as e:
        print(f"  WARN: 下载 docker-compose.yml 失败: {e}", flush=True)

    ssh.close()

    # ── 汇总 ──
    total_kb = sum(f.stat().st_size for f in backup_dir.iterdir() if f.is_file()) / 1024
    print(f"\n=== 备份完成 ===", flush=True)
    print(f"  目录: {backup_dir}", flush=True)
    print(f"  大小: {total_kb:.0f} KB", flush=True)
    print(f"  文件:", flush=True)
    for f in sorted(backup_dir.iterdir()):
        print(f"    {f.name} ({f.stat().st_size / 1024:.0f} KB)", flush=True)

    # ── 清理旧备份 ──
    if not args.no_cleanup:
        deleted = cleanup_old_backups(args.keep)
        if deleted:
            print(f"\n=== 清理旧备份 (保留最近 {args.keep} 份) ===", flush=True)
            for d in deleted:
                print(f"  删除: {d}", flush=True)
        else:
            print(f"\n  旧备份数未超过 {args.keep}, 无需清理", flush=True)

    print("\nDone!", flush=True)


if __name__ == "__main__":
    main()
