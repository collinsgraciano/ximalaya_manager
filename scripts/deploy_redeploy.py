#!/usr/bin/env python3
"""ximalaya_manager VPS 一键重新部署脚本。

用法:
    python scripts/deploy_redeploy.py

功能:
    1. SSH 进 VPS, git pull 最新代码
    2. 对运行中的 PostgreSQL 执行 init-db.sql (幂等, 安全重跑)
    3. 重建并重启 web 容器
    4. 等待健康检查通过
    5. 验证: 容器状态 / 日志 / 外网访问

前置条件:
    - pip install paramiko
    - VPS 上项目路径: /opt/ximalaya_manager
    - VPS 上 Docker Compose 已配置好
"""

import paramiko
import sys
import time

# ═══════════════════════════════════════════
# VPS 连接配置
# ═══════════════════════════════════════════
VPS_HOST = "117.55.234.219"
VPS_PORT = 22
VPS_USER = "root"
VPS_PASS = "vrlcLlU5if"

PROJECT_DIR = "/opt/ximalaya_manager"
WEB_PORT = 59388

# ═══════════════════════════════════════════


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"连接 {VPS_HOST}...")
    ssh.connect(VPS_HOST, port=VPS_PORT, username=VPS_USER, password=VPS_PASS, timeout=15)
    print("SSH 连接成功\n")

    # Step 1: Git pull
    print("=== Git pull ===")
    stdin, stdout, stderr = ssh.exec_command(f"cd {PROJECT_DIR} && git pull origin main 2>&1")
    print(stdout.read().decode("utf-8", errors="replace").strip())

    # Step 2: Stop web container to free memory for build
    print("\n=== Stopping web container (free memory for build) ===")
    stdin, stdout, stderr = ssh.exec_command(
        f"cd {PROJECT_DIR} && docker compose stop web 2>&1"
    )
    print(stdout.read().decode("utf-8", errors="replace").strip())

    # Step 3: Re-run init-db.sql (idempotent)
    print("\n=== Re-running init-db.sql ===")
    stdin, stdout, stderr = ssh.exec_command(
        f"docker exec -i xm_postgres psql -U xm_app -d ximalaya < {PROJECT_DIR}/docker/init-db.sql 2>&1"
    )
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    for line in out.splitlines()[-5:]:
        print(line)
    print(f"Exit: {exit_status}")

    # Step 4: Clean up leftover build containers from previous interrupted builds
    print("\n=== Cleaning up leftover build containers ===")
    stdin, stdout, stderr = ssh.exec_command(
        "docker ps -a --filter 'ancestor=1c06f14f1f45' --filter 'status=running' -q | xargs -r docker stop 2>/dev/null; "
        "docker ps -a --filter 'ancestor=1c06f14f1f45' -q | xargs -r docker rm 2>/dev/null; "
        "echo done"
    )
    print(stdout.read().decode("utf-8", errors="replace").strip())

    # Step 5: Rebuild web container (DOCKER_BUILDKIT=0 forces serial stage execution)
    # BuildKit 并行执行多阶段 apt-get 会导致 1GB VPS OOM; 串行构建安全且保留缓存
    print("\n=== Rebuilding web container (BuildKit disabled — serial, cached) ===")
    stdin, stdout, stderr = ssh.exec_command(
        f"cd {PROJECT_DIR} && DOCKER_BUILDKIT=0 docker compose build web 2>&1",
        timeout=600,
    )
    while not stdout.channel.exit_status_ready():
        if stdout.channel.recv_ready():
            chunk = stdout.channel.recv(4096).decode("utf-8", errors="replace")
            print(chunk, end="")
        time.sleep(0.5)
    remaining = stdout.read().decode("utf-8", errors="replace")
    if remaining:
        print(remaining, end="")
    exit_status = stdout.channel.recv_exit_status()
    print(f"\nExit: {exit_status}")

    # Step 6: Start web container
    print("\n=== Starting web container ===")
    stdin, stdout, stderr = ssh.exec_command(
        f"cd {PROJECT_DIR} && docker compose up -d web 2>&1",
        timeout=120,
    )
    print(stdout.read().decode("utf-8", errors="replace").strip())

    # Step 7: Wait for health
    print("\nWaiting 15s for containers...")
    time.sleep(15)

    # Step 8: Verify
    print("\n=== Health check ===")
    stdin, stdout, stderr = ssh.exec_command(f"curl -s http://localhost:{WEB_PORT}/api/system/health")
    print(stdout.read().decode("utf-8", errors="replace").strip())

    print("\n=== External access ===")
    stdin, stdout, stderr = ssh.exec_command(f"curl -s http://{VPS_HOST}:{WEB_PORT}/api/system/health")
    print(stdout.read().decode("utf-8", errors="replace").strip())

    print("\n=== Container status ===")
    stdin, stdout, stderr = ssh.exec_command(f"cd {PROJECT_DIR} && docker compose ps")
    print(stdout.read().decode("utf-8", errors="replace").strip())

    print("\n=== Web logs (last 10) ===")
    stdin, stdout, stderr = ssh.exec_command(f"cd {PROJECT_DIR} && docker compose logs --tail=10 web")
    print(stdout.read().decode("utf-8", errors="replace").strip())

    ssh.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
