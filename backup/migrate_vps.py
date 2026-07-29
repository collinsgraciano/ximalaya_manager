#!/usr/bin/env python3
"""ximalaya_manager VPS 一键迁移脚本。

用法:
    python backup/migrate_vps.py --dst-host NEW_IP --dst-pass NEW_ROOT_PASSWORD
    python backup/migrate_vps.py --dst-host NEW_IP --dst-pass NEW_PASSWORD --git-url https://github.com/your-repo/ximalaya_manager.git

功能:
    1. 从旧 VPS 备份数据库 + .env (pg_dump | gzip)
    2. 上传备份到新 VPS
    3. 在新 VPS 上 git clone 项目
    4. 恢复 .env (BASE_URL 自动改写为新 IP)
    5. docker compose up -d --build
    6. 恢复数据库 (gunzip | psql)
    7. 健康检查 + 数据校验

前置条件:
    - pip install paramiko
    - 新 VPS 已安装 Docker + Docker Compose
    - 新 VPS 的 SSH root 密码

注意:
    迁移完成后旧 VPS 不会自动关停, 确认新 VPS 正常后手动关停。
"""

import paramiko
import sys
import time
import argparse
from datetime import datetime

# ═══════════════════════════════════════════
# 旧 VPS (源) 配置
# ═══════════════════════════════════════════
SRC_HOST = "117.55.234.219"
SRC_PORT = 22
SRC_USER = "root"
SRC_PASS = "vrlcLlU5if"

SRC_PROJECT_DIR = "/opt/ximalaya_manager"
SRC_CONTAINER = "xm_postgres"
SRC_DB_USER = "xm_app"
SRC_DB_NAME = "ximalaya"

# ═══════════════════════════════════════════
# 新 VPS (目标) 默认配置 (可通过命令行参数覆盖)
# ═══════════════════════════════════════════
DST_PORT = 22
DST_USER = "root"
DST_PROJECT_DIR = "/opt/ximalaya_manager"
GIT_REPO_URL = "https://github.com/claude-demo/ximalaya_manager.git"

# VPS 上临时路径
REMOTE_DUMP_GZ = "/tmp/xm_dump.sql.gz"
REMOTE_BACKUP_DIR = "/tmp/xm_migration"
# ═══════════════════════════════════════════


def run_remote(ssh, cmd, timeout=120):
    """执行远程命令, 返回 (exit_code, stdout, stderr)。"""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return exit_code, out, err


def stream_remote(ssh, cmd, timeout=300):
    """执行远程命令并实时流式输出, 返回 exit_code。"""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    while not stdout.channel.exit_status_ready():
        if stdout.channel.recv_ready():
            chunk = stdout.channel.recv(4096).decode("utf-8", errors="replace")
            print(chunk, end="")
        if stderr.channel.recv_ready():
            chunk = stderr.channel.recv(4096).decode("utf-8", errors="replace")
            print(chunk, end="")
        time.sleep(0.3)
    remaining_out = stdout.read().decode("utf-8", errors="replace")
    if remaining_out:
        print(remaining_out, end="")
    remaining_err = stderr.read().decode("utf-8", errors="replace")
    if remaining_err:
        print(remaining_err, end="")
    return stdout.channel.recv_exit_status()


def sftp_upload(ssh, local_path, remote_path):
    """通过 SFTP 上传文件。"""
    sftp = ssh.open_sftp()
    try:
        sftp.put(str(local_path), remote_path)
    finally:
        sftp.close()


def sftp_download(ssh, remote_path, local_path):
    """通过 SFTP 下载文件。"""
    sftp = ssh.open_sftp()
    try:
        sftp.get(remote_path, str(local_path))
    finally:
        sftp.close()


def step(n, total, title):
    print(f"\n{'='*60}")
    print(f"  Step {n}/{total}: {title}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="ximalaya_manager VPS 一键迁移")
    parser.add_argument("--dst-host", required=True, help="新 VPS IP 地址")
    parser.add_argument("--dst-pass", required=True, help="新 VPS root SSH 密码")
    parser.add_argument("--dst-port", type=int, default=DST_PORT, help=f"新 VPS SSH 端口 (默认 {DST_PORT})")
    parser.add_argument("--dst-user", default=DST_USER, help=f"新 VPS SSH 用户 (默认 {DST_USER})")
    parser.add_argument("--git-url", default=GIT_REPO_URL, help="Git 仓库地址")
    parser.add_argument("--dst-dir", default=DST_PROJECT_DIR, help=f"新 VPS 项目路径 (默认 {DST_PROJECT_DIR})")
    args = parser.parse_args()

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    dst_host = args.dst_host
    dst_pass = args.dst_pass
    dst_port = args.dst_port
    dst_user = args.dst_user
    git_url = args.git_url
    dst_dir = args.dst_dir

    TOTAL_STEPS = 7
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"[{timestamp}] 开始迁移 ximalaya_manager")
    print(f"  旧 VPS: {SRC_HOST}")
    print(f"  新 VPS: {dst_host}")
    print(f"  Git:    {git_url}")

    # ════════════════════════════════════════
    # Step 1: 从旧 VPS 备份
    # ════════════════════════════════════════
    step(1, TOTAL_STEPS, "从旧 VPS 备份数据库 + .env")

    print(f"  连接旧 VPS {SRC_HOST} ...")
    ssh_src = paramiko.SSHClient()
    ssh_src.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_src.connect(SRC_HOST, port=SRC_PORT, username=SRC_USER, password=SRC_PASS, timeout=15)
    print("  SSH 连接成功")

    # pg_dump | gzip
    print("  导出数据库 (pg_dump | gzip) ...")
    dump_cmd = (
        f"docker exec {SRC_CONTAINER} "
        f"pg_dump -U {SRC_DB_USER} -d {SRC_DB_NAME} --no-owner --no-privileges"
        f" | gzip > {REMOTE_DUMP_GZ}"
    )
    exit_code, out, err = run_remote(ssh_src, dump_cmd, timeout=300)
    if exit_code != 0:
        print(f"  FAIL: pg_dump 退出码 {exit_code}")
        print(f"  stderr: {err}")
        ssh_src.close()
        sys.exit(1)

    _, size_out, _ = run_remote(ssh_src, f"stat -c%s {REMOTE_DUMP_GZ} 2>/dev/null || echo 0")
    dump_bytes = int(size_out) if size_out.strip().isdigit() else 0
    print(f"  OK: 压缩 dump {dump_bytes / 1024:.0f} KB")

    # 下载 .env 到临时文件
    import tempfile, os
    tmp_dir = tempfile.mkdtemp(prefix="xm_migrate_")
    local_dump_gz = os.path.join(tmp_dir, "db_dump.sql.gz")
    local_env = os.path.join(tmp_dir, ".env")

    print("  下载数据库备份 ...")
    sftp_download(ssh_src, REMOTE_DUMP_GZ, local_dump_gz)
    print(f"  OK: {os.path.getsize(local_dump_gz) / 1024:.0f} KB")

    print("  下载 .env ...")
    try:
        sftp_download(ssh_src, f"{SRC_PROJECT_DIR}/.env", local_env)
        print("  OK")
    except Exception as e:
        print(f"  FAIL: 无法下载 .env: {e}")
        ssh_src.close()
        sys.exit(1)

    # 清理远程临时文件
    run_remote(ssh_src, f"rm -f {REMOTE_DUMP_GZ}")
    ssh_src.close()
    print("  旧 VPS 连接已关闭")

    # ════════════════════════════════════════
    # Step 2: 连接新 VPS, 上传备份
    # ════════════════════════════════════════
    step(2, TOTAL_STEPS, "连接新 VPS 并上传备份")

    print(f"  连接新 VPS {dst_host} ...")
    ssh_dst = paramiko.SSHClient()
    ssh_dst.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_dst.connect(dst_host, port=dst_port, username=dst_user, password=dst_pass, timeout=15)
    print("  SSH 连接成功")

    # 创建临时目录
    run_remote(ssh_dst, f"mkdir -p {REMOTE_BACKUP_DIR}")

    # 上传备份文件
    remote_dump = f"{REMOTE_BACKUP_DIR}/db_dump.sql.gz"
    remote_env = f"{REMOTE_BACKUP_DIR}/.env"

    print("  上传 db_dump.sql.gz ...")
    sftp_upload(ssh_dst, local_dump_gz, remote_dump)
    print(f"  OK: {os.path.getsize(local_dump_gz) / 1024:.0f} KB")

    print("  上传 .env ...")
    sftp_upload(ssh_dst, local_env, remote_env)
    print("  OK")

    # ════════════════════════════════════════
    # Step 3: git clone 项目
    # ════════════════════════════════════════
    step(3, TOTAL_STEPS, "在新 VPS 上克隆项目")

    # 检查是否已存在
    exit_code, _, _ = run_remote(ssh_dst, f"test -d {dst_dir}/.git && echo yes")
    if exit_code == 0:
        print(f"  项目已存在于 {dst_dir}, 执行 git pull ...")
        stream_remote(ssh_dst, f"cd {dst_dir} && git pull origin main 2>&1")
    else:
        parent = os.path.dirname(dst_dir.rstrip("/"))
        print(f"  克隆到 {dst_dir} ...")
        stream_remote(ssh_dst, f"cd {parent} && git clone {git_url} {os.path.basename(dst_dir)} 2>&1")

    # ════════════════════════════════════════
    # Step 4: 恢复 .env (改写 BASE_URL)
    # ════════════════════════════════════════
    step(4, TOTAL_STEPS, "恢复 .env 并改写 BASE_URL")

    print("  复制 .env 到项目目录 ...")
    run_remote(ssh_dst, f"cp {remote_env} {dst_dir}/.env")

    # 改写 BASE_URL 为新 IP
    new_base_url = f"http://{dst_host}:59388"
    print(f"  改写 BASE_URL -> {new_base_url}")
    run_remote(ssh_dst, f"sed -i 's|^BASE_URL=.*|BASE_URL={new_base_url}|' {dst_dir}/.env")

    # 验证 .env 关键项
    _, env_content, _ = run_remote(ssh_dst, f"grep -E '^(POSTGRES_PASSWORD|APP_PASSWORD|WORKER_AUTH_TOKEN|BASE_URL)=' {dst_dir}/.env")
    print(f"  .env 关键项:\n    {env_content.replace(chr(10), chr(10) + '    ')}")

    # ════════════════════════════════════════
    # Step 5: docker compose up
    # ════════════════════════════════════════
    step(5, TOTAL_STEPS, "启动 Docker 容器")

    print("  docker compose up -d --build ...")
    stream_remote(ssh_dst, f"cd {dst_dir} && docker compose up -d --build 2>&1", timeout=300)

    print("\n  等待 PostgreSQL 健康检查 (15s) ...")
    time.sleep(15)

    _, ps_out, _ = run_remote(ssh_dst, f"cd {dst_dir} && docker compose ps --format 'table {{{{.Name}}}}\t{{{{.Status}}}}'")
    print(f"  容器状态:\n    {ps_out.replace(chr(10), chr(10) + '    ')}")

    # ════════════════════════════════════════
    # Step 6: 恢复数据库
    # ════════════════════════════════════════
    step(6, TOTAL_STEPS, "恢复数据库")

    print("  导入 db_dump.sql.gz (gunzip | psql) ...")
    restore_cmd = (
        f"docker exec -i xm_postgres bash -c "
        f"'gunzip -c /dev/stdin | psql -U xm_app -d ximalaya'"
        f" < {remote_dump} 2>&1"
    )
    exit_code = stream_remote(ssh_dst, restore_cmd, timeout=300)
    if exit_code != 0:
        print(f"  WARN: psql 退出码 {exit_code}, 可能部分已存在 (init-db.sql 已创建表)")

    # ════════════════════════════════════════
    # Step 7: 健康检查 + 数据校验
    # ════════════════════════════════════════
    step(7, TOTAL_STEPS, "健康检查 + 数据校验")

    print("\n  [1] API 健康检查 (localhost):")
    _, health, _ = run_remote(ssh_dst, "curl -s http://localhost:59388/api/system/health")
    print(f"    {health}")

    print("\n  [2] API 健康检查 (外网):")
    _, health_ext, _ = run_remote(ssh_dst, f"curl -s http://{dst_host}:59388/api/system/health")
    print(f"    {health_ext}")

    print("\n  [3] 数据校验:")
    queries = [
        ("books", "SELECT count(*) AS books FROM books"),
        ("audiobook_chapters", "SELECT count(*) AS chapters FROM audiobook_chapters"),
        ("xm_jobs", "SELECT count(*) AS jobs FROM xm_jobs"),
        ("global_settings", "SELECT count(*) AS settings FROM global_settings"),
    ]
    for label, sql in queries:
        _, count, _ = run_remote(ssh_dst, f"docker exec xm_postgres psql -U xm_app -d ximalaya -t -c \"{sql}\"")
        print(f"    {label}: {count.strip()}")

    print("\n  [4] Web 日志 (最后 5 行):")
    _, logs, _ = run_remote(ssh_dst, f"cd {dst_dir} && docker compose logs --tail=5 web 2>&1")
    print(f"    {logs.replace(chr(10), chr(10) + '    ')}")

    # 清理
    run_remote(ssh_dst, f"rm -rf {REMOTE_BACKUP_DIR}")
    ssh_dst.close()

    # 清理本地临时文件
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # ════════════════════════════════════════
    # 汇总
    # ════════════════════════════════════════
    print(f"\n{'='*60}")
    print(f"  迁移完成!")
    print(f"{'='*60}")
    print(f"  新 VPS: {dst_host}")
    print(f"  Web:    http://{dst_host}:59388")
    print(f"  端口:   59388 (Web), 5433 (PostgreSQL)")
    print()
    print("  迁移后检查清单:")
    print("    [ ] Web 界面能正常访问")
    print("    [ ] 用 APP_PASSWORD 能登录")
    print("    [ ] 专辑列表、章节列表数据完整")
    print("    [ ] Colab Worker --vps-url 已更新为新地址")
    print("    [ ] 防火墙已开放 59388 端口")
    print()
    print("  注意: 旧 VPS 未关停, 确认新 VPS 正常后手动关停。")


if __name__ == "__main__":
    main()
