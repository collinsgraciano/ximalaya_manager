#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  喜马拉雅有声书管理系统 — 服务器端部署脚本
#  在服务器上手动运行：git pull → 智能构建 → 重启 → 健康检查
#
#  用法（SSH 登录服务器后）：
#    cd /root/ximalaya_manager
#    bash scripts/deploy/git-server-deploy.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SERVER_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${SERVER_PATH}"

# ─── 检测 docker compose 命令（优先 v2，避免 snap 沙箱问题）───
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif docker-compose version >/dev/null 2>&1; then
    DC="docker-compose"
else
    echo "  [x] 未找到 docker compose 命令，请安装 Docker"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════"
echo "  喜马拉雅管理系统部署开始 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  路径: ${SERVER_PATH}"
echo "═══════════════════════════════════════════════════════════"

# ─── 1. 拉取最新代码 ───
echo "[1/4] git pull..."
OLD_SCRIPT_HASH=$(md5sum "${BASH_SOURCE[0]}" 2>/dev/null | awk '{print $1}' || echo "")
git pull
NEW_SCRIPT_HASH=$(md5sum "${BASH_SOURCE[0]}" 2>/dev/null | awk '{print $1}' || echo "")
echo "  当前版本: $(git rev-parse --short HEAD)"
# 如果脚本自身被 git pull 更新了，用新版本重新执行
if [ "$OLD_SCRIPT_HASH" != "$NEW_SCRIPT_HASH" ] && [ -z "${DEPLOY_SCRIPT_REEXECED:-}" ]; then
    echo "  > 部署脚本自身有更新，自动重新执行新版本..."
    export DEPLOY_SCRIPT_REEXECED=1
    exec bash "${BASH_SOURCE[0]}" "$@"
fi
echo ""

# ─── 2. 检查 .env ───
echo "[2/4] 检查 .env..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "  [!] 已从 .env.example 创建 .env，请编辑后重新运行"
        echo "      nano .env"
        exit 1
    else
        echo "  [x] .env 不存在，请手动创建"
        exit 1
    fi
else
    echo "  .env OK"
fi
echo ""

# ─── 2.5 预下载 DeepFilter 二进制（持久化到宿主机）───
echo "[2.5/4] 检查 DeepFilter 二进制..."
DEEPFILTER_DIR="${SERVER_PATH}/data/deepfilter"
DEEPFILTER_BIN="deep-filter-0.5.6-x86_64-unknown-linux-musl"
DEEPFILTER_URL="https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-unknown-linux-musl"

mkdir -p "$DEEPFILTER_DIR"
if [ -f "$DEEPFILTER_DIR/$DEEPFILTER_BIN" ]; then
    echo "  OK DeepFilter 已在宿主机缓存中"
else
    echo "  > 下载 DeepFilter 到 $DEEPFILTER_DIR ..."
    if wget --tries=5 --timeout=30 --retry-connrefused \
        "$DEEPFILTER_URL" -O "$DEEPFILTER_DIR/$DEEPFILTER_BIN"; then
        chmod +x "$DEEPFILTER_DIR/$DEEPFILTER_BIN"
        echo "  OK DeepFilter 下载完成（后续重建镜像不再重复下载）"
    else
        echo "  [!] DeepFilter 下载失败，容器启动时会自动重试"
    fi
fi
echo ""

# ─── 2.6 检查 5433 端口占用（关闭系统自带 PostgreSQL / 残留容器）───
echo "[2.6/4] 检查 5433 端口占用..."
PORT_5433_OCCUPIED=$(ss -tlnp 2>/dev/null | grep ':5433 ' || echo "")
if [ -n "$PORT_5433_OCCUPIED" ]; then
    PORT_5433_PID=$(echo "$PORT_5433_OCCUPIED" | head -1 | sed -n 's/.*pid=\([0-9]*\).*/\1/p')
    PORT_5433_PROC=$(ps -p "${PORT_5433_PID}" -o comm= 2>/dev/null || echo "unknown")
    echo "  [!] 端口 5433 被占用: PID=${PORT_5433_PID} (${PORT_5433_PROC})"

    if [ "$PORT_5433_PROC" = "docker-proxy" ] || [ "$PORT_5433_PROC" = "docker" ]; then
        # ─── 情况 A: Docker 容器占用了端口 ───
        echo "  > 检测到 Docker 容器占用端口"
        OLD_PG_CONTAINER=$(docker ps --filter "publish=5433" --format '{{.Names}}' 2>/dev/null | head -1 || echo "")
        if [ -n "$OLD_PG_CONTAINER" ]; then
            echo "  > 停止残留容器: ${OLD_PG_CONTAINER}"
            docker stop "$OLD_PG_CONTAINER" 2>/dev/null && echo "  OK 已停止 ${OLD_PG_CONTAINER}"
            docker rm "$OLD_PG_CONTAINER" 2>/dev/null
        fi
    else
        # ─── 情况 B: 系统自带 PostgreSQL 占用了端口 ───
        PG_SERVICE=$(systemctl list-units --type=service --state=running 2>/dev/null \
            | grep -oE 'postgresql[^ ]*' | head -1 || echo "")
        if [ -n "$PG_SERVICE" ]; then
            echo "  > 停止系统 PostgreSQL 服务: ${PG_SERVICE}"
            systemctl stop "$PG_SERVICE" 2>/dev/null && echo "  OK 已停止 ${PG_SERVICE}"
            systemctl disable "$PG_SERVICE" 2>/dev/null && echo "  OK 已禁用开机自启 ${PG_SERVICE}"
        else
            echo "  [!] 未找到 PostgreSQL systemd 服务，尝试直接终止进程..."
            kill "$PORT_5433_PID" 2>/dev/null && echo "  OK 已终止进程 ${PORT_5433_PID}"
        fi
    fi

    # 二次确认端口已释放
    sleep 2
    if ss -tlnp 2>/dev/null | grep -q ':5433 '; then
        echo "  [x] 端口 5433 仍被占用，请手动处理: ss -tlnp | grep 5433"
        exit 1
    fi
    echo "  OK 端口 5433 已释放"
else
    echo "  OK 端口 5433 未被占用"
fi
echo ""

# ─── 3. 智能构建与重启 ───
echo "[3/4] Docker 构建..."

# 判断是否需要重建镜像
NEED_BUILD=false

# 检查 1: requirements.txt
REQ_HASH_FILE=".cache_req_hash"
CUR_REQ_HASH=$(md5sum requirements.txt 2>/dev/null | awk '{print $1}' || echo "none")
LAST_REQ_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_REQ_HASH" != "$LAST_REQ_HASH" ]; then
    NEED_BUILD=true
    echo "  > requirements.txt 有变更"
fi

# 检查 2: Dockerfile
DOCKER_HASH_FILE=".cache_docker_hash"
CUR_DOCKER_HASH=$(cat docker/Dockerfile.web 2>/dev/null | md5sum | awk '{print $1}' || echo "none")
LAST_DOCKER_HASH=$(cat "$DOCKER_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_DOCKER_HASH" != "$LAST_DOCKER_HASH" ]; then
    NEED_BUILD=true
    echo "  > Dockerfile 有变更"
fi

# 检查 3: backend/ + pipeline/ + docker/ 源码变更
SRC_HASH_FILE=".cache_src_hash"
CUR_SRC_HASH=$(find backend/ pipeline/ docker/ -type f \( -name '*.py' -o -name '*.html' -o -name '*.sql' -o -name '*.j2' -o -name '*.txt' -o -name '*.sh' \) 2>/dev/null | sort | xargs cat 2>/dev/null | md5sum | awk '{print $1}' || echo "none")
LAST_SRC_HASH=$(cat "$SRC_HASH_FILE" 2>/dev/null || echo "")
if [ "$CUR_SRC_HASH" != "$LAST_SRC_HASH" ]; then
    NEED_BUILD=true
    echo "  > 源代码有变更"
fi

if [ "$NEED_BUILD" = true ]; then
    echo "  正在构建镜像..."
    $DC build
    echo "$CUR_REQ_HASH" > "$REQ_HASH_FILE"
    echo "$CUR_DOCKER_HASH" > "$DOCKER_HASH_FILE"
    echo "$CUR_SRC_HASH" > "$SRC_HASH_FILE"
else
    echo "  依赖与源码均未变更，跳过构建"
fi

echo "  重启服务..."
$DC up -d
echo ""

# ─── 4. 健康检查 ───
echo "[4/4] 等待服务就绪..."
sleep 3

# Web 服务健康检查
for i in $(seq 1 15); do
    if curl -sf http://localhost:59388/api/system/health >/dev/null 2>&1; then
        echo "  OK Web 服务就绪"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "  [!] Web 服务未就绪，查看日志:"
        echo "      $DC logs --tail 20 web"
        exit 1
    fi
    sleep 2
done

# PostgreSQL 健康检查
for i in $(seq 1 10); do
    if $DC exec -T postgres pg_isready -U xm_app -d ximalaya >/dev/null 2>&1; then
        echo "  OK PostgreSQL 就绪"
        break
    fi
    if [ $i -eq 10 ]; then
        echo "  [!] PostgreSQL 未就绪，查看日志:"
        echo "      $DC logs --tail 20 postgres"
    fi
    sleep 2
done

echo ""
echo "─── 服务状态 ───"
$DC ps

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  部署完成 — $(date '+%H:%M:%S')"
echo "  Web 管理系统:  http://$(hostname -I | awk '{print $1}'):59388"
echo "═══════════════════════════════════════════════════════════"
