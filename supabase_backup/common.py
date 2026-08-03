#!/usr/bin/env python3
"""ximalaya_manager B2 云备份 — 共享工具模块。

包含: 配置加载、SSH 工具、Backblaze B2 S3 兼容 API。
被 backup.py / restore.py / backup_service.py 共同引用。

B2 免费额度: 10GB 存储 + 1GB/天 下载流量，无单文件大小限制。
使用 S3 兼容 API (boto3)。

配置优先级: init_config() 运行时注入 > os.environ > 默认值
"""

from __future__ import annotations

import os
import sys
import io
import time
import paramiko
from pathlib import Path
from datetime import datetime

# ═══════════════════════════════════════════
# 路径
# ═══════════════════════════════════════════

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

# ═══════════════════════════════════════════
# 运行时配置（可在调用前通过 init_config() 注入）
# ═══════════════════════════════════════════

_runtime_config: dict = {}


def init_config(config: dict):
    """注入运行时配置。

    用于 Web API 调用时绕过 .env 文件，直接从 DB 传入配置。

    Args:
        config: 配置字典，键名与模块变量名一致，如 {"B2_ENDPOINT": "...", ...}
    """
    _runtime_config.clear()
    _runtime_config.update(config)


def _cfg(key: str, default: str = "") -> str:
    """读取配置：运行时注入 > 环境变量 > 默认值。"""
    if key in _runtime_config and _runtime_config[key] not in (None, ""):
        return str(_runtime_config[key])
    return os.environ.get(key, default)


def _cfg_int(key: str, default: int = 0) -> int:
    val = _cfg(key, str(default))
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _cfg_float(key: str, default: float = 0.0) -> float:
    val = _cfg(key, str(default))
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ═══════════════════════════════════════════
# 配置访问（通过 _cfg 获取）
# ═══════════════════════════════════════════

def get_vps_host() -> str:
    return _cfg("VPS_HOST", "117.55.234.219")

def get_vps_port() -> int:
    return _cfg_int("VPS_PORT", 22)

def get_vps_user() -> str:
    return _cfg("VPS_USER", "root")

def get_vps_pass() -> str:
    return _cfg("VPS_PASS", "")

def get_vps_container() -> str:
    return _cfg("VPS_CONTAINER", "xm_postgres")

def get_vps_db_user() -> str:
    return _cfg("VPS_DB_USER", "xm_app")

def get_vps_db_name() -> str:
    return _cfg("VPS_DB_NAME", "ximalaya")

def get_vps_project_dir() -> str:
    return _cfg("VPS_PROJECT_DIR", "/opt/ximalaya_manager")

def get_b2_endpoint() -> str:
    return _cfg("B2_ENDPOINT", "")

def get_b2_key_id() -> str:
    return _cfg("B2_ACCESS_KEY_ID", "")

def get_b2_key_secret() -> str:
    return _cfg("B2_SECRET_ACCESS_KEY", "")

def get_b2_bucket() -> str:
    return _cfg("B2_BUCKET", "xm-backups")

def get_backup_keep() -> int:
    return _cfg_int("BACKUP_KEEP", 7)

def get_backup_interval_hours() -> float:
    return _cfg_float("BACKUP_INTERVAL_HOURS", 24)


def check_b2_config() -> bool:
    """检查 B2 配置是否完整。"""
    missing = []
    if not get_b2_endpoint():
        missing.append("B2_ENDPOINT")
    if not get_b2_key_id():
        missing.append("B2_ACCESS_KEY_ID")
    if not get_b2_key_secret():
        missing.append("B2_SECRET_ACCESS_KEY")
    if missing:
        print(f"ERROR: 未配置 {', '.join(missing)}", flush=True)
        return False
    return True


def check_vps_config() -> bool:
    """检查 VPS SSH 配置是否完整。"""
    if not get_vps_pass():
        print("ERROR: 未配置 VPS_PASS", flush=True)
        return False
    return True


# ═══════════════════════════════════════════
# SSH 工具
# ═══════════════════════════════════════════

def ssh_connect() -> paramiko.SSHClient:
    """连接 VPS SSH，返回 SSHClient。"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        get_vps_host(),
        port=get_vps_port(),
        username=get_vps_user(),
        password=get_vps_pass(),
        timeout=15,
    )
    return ssh


def run_remote(ssh: paramiko.SSHClient, cmd: str, timeout: int = 300) -> tuple[int, str, str]:
    """执行远程命令，返回 (exit_code, stdout, stderr)。"""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return exit_code, out, err


def sftp_read(ssh: paramiko.SSHClient, remote_path: str) -> bytes:
    """通过 SFTP 读取远程文件内容。"""
    sftp = ssh.open_sftp()
    try:
        with sftp.open(remote_path, "rb") as f:
            return f.read()
    finally:
        sftp.close()


def sftp_write(ssh: paramiko.SSHClient, remote_path: str, data: bytes):
    """通过 SFTP 写入远程文件。"""
    sftp = ssh.open_sftp()
    try:
        with sftp.open(remote_path, "wb") as f:
            f.write(data)
    finally:
        sftp.close()


# ═══════════════════════════════════════════
# Backblaze B2 S3 兼容 API
# ═══════════════════════════════════════════

def _get_s3_client():
    """创建 boto3 S3 客户端 (B2 S3 兼容 API)。"""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{get_b2_endpoint()}",
        aws_access_key_id=get_b2_key_id(),
        aws_secret_access_key=get_b2_key_secret(),
        config=Config(
            connect_timeout=30,
            read_timeout=300,
            retries={"max_attempts": 3},
        ),
    )


def ensure_bucket() -> bool:
    """确保 bucket 存在。B2 中 bucket 需在网页端创建，这里仅检查是否存在。"""
    try:
        s3 = _get_s3_client()
        s3.head_bucket(Bucket=get_b2_bucket())
        return True
    except Exception as e:
        err_str = str(e)
        bucket = get_b2_bucket()
        if "404" in err_str or "NoSuchBucket" in err_str:
            print(f"  ERROR: B2 bucket '{bucket}' 不存在，请在 B2 网页端创建", flush=True)
        elif "403" in err_str or "AccessDenied" in err_str:
            print(f"  ERROR: 无权访问 B2 bucket '{bucket}'，请检查密钥权限", flush=True)
        else:
            print(f"  ERROR: 检查 bucket 失败: {err_str[:200]}", flush=True)
        return False


def upload_to_b2(key: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
    """上传文件到 B2 Storage。"""
    try:
        s3 = _get_s3_client()
        s3.upload_fileobj(
            io.BytesIO(data),
            get_b2_bucket(),
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return True
    except Exception as e:
        print(f"  FAIL: 上传 {key} 失败: {e}", flush=True)
        return False


def download_from_b2(key: str) -> bytes | None:
    """从 B2 下载文件，返回 bytes。"""
    try:
        s3 = _get_s3_client()
        buf = io.BytesIO()
        s3.download_fileobj(get_b2_bucket(), key, buf)
        return buf.getvalue()
    except Exception as e:
        print(f"  FAIL: 下载 {key} 失败: {e}", flush=True)
        return None


def list_b2_objects(prefix: str = "") -> list[dict]:
    """列出 B2 bucket 中的对象。"""
    try:
        s3 = _get_s3_client()
        objects = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=get_b2_bucket(), Prefix=prefix):
            for obj in page.get("Contents", []):
                objects.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj.get("LastModified"),
                })
        return objects
    except Exception:
        return []


def list_backup_folders() -> list[str]:
    """列出所有备份文件夹名（时间戳），按降序排列（最新在前）。"""
    objects = list_b2_objects()
    folders = set()
    for obj in objects:
        key = obj["key"]
        if "/" in key:
            folders.add(key.split("/")[0])
    return sorted(folders, reverse=True)


def list_backup_files(folder: str) -> list[str]:
    """列出某个备份文件夹下的所有文件名。"""
    objects = list_b2_objects(prefix=f"{folder}/")
    files = []
    for obj in objects:
        key = obj["key"]
        filename = key.split("/")[-1] if "/" in key else key
        if filename:
            files.append(filename)
    return files


def delete_from_b2(key: str) -> bool:
    """删除 B2 中的文件。"""
    try:
        s3 = _get_s3_client()
        s3.delete_object(Bucket=get_b2_bucket(), Key=key)
        return True
    except Exception:
        return False


def delete_backup_folder(folder: str):
    """删除整个备份文件夹（逐个删除其中的文件）。"""
    objects = list_b2_objects(prefix=f"{folder}/")
    for obj in objects:
        delete_from_b2(obj["key"])


# ═══════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════

def timestamp_str() -> str:
    """返回当前时间戳字符串 (如 20260802_030000)。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def format_size(size_bytes: int) -> str:
    """格式化文件大小。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
