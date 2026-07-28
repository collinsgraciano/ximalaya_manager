"""Telegram 音频上传模块 — 多 Bot Token 轮换。"""

from __future__ import annotations

import os
import time
import random
import threading
import logging
import requests

logger = logging.getLogger(__name__)

# TG API 基础 URL（支持 VPS 中继代理）
_TG_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

# 串行上传锁
_UPLOAD_LOCK = threading.Lock()

# Round-robin 计数器（多 Bot Token 轮换）
_token_counter = 0
_token_counter_lock = threading.Lock()


def extract_bot_user_id(token: str) -> int | None:
    """从 Bot Token 中提取永久 Telegram User ID。

    Token 格式: {bot_user_id}:{secret}  例如: 7485554965:AAHxxx...
    """
    try:
        return int(token.split(":")[0])
    except (ValueError, IndexError):
        return None


def upload_audio_to_telegram(
    file_path: str,
    bot_token: str,
    chat_id: str,
    title: str = "",
    caption: str = "",
    max_retries: int = 3,
) -> dict:
    """上传音频文件到 Telegram，返回上传结果。

    返回:
        {
            "ok": bool,
            "file_id": str,
            "message_id": int,
            "bot_user_id": int,
            "error": str,
            "file_size": int,
        }
    """
    bot_user_id = extract_bot_user_id(bot_token)

    if not os.path.exists(file_path):
        return {"ok": False, "file_id": "", "message_id": 0, "bot_user_id": bot_user_id,
                "error": f"文件不存在: {file_path}", "file_size": 0}

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        return {"ok": False, "file_id": "", "message_id": 0, "bot_user_id": bot_user_id,
                "error": "文件大小为 0", "file_size": 0}

    # TG Bot API 限制：sendAudio 文件最大 50MB
    if file_size > 50 * 1024 * 1024:
        return upload_document_to_telegram(file_path, bot_token, chat_id, title, caption, max_retries)

    api_url = f"{_TG_API_BASE}/bot{bot_token}/sendAudio"

    for attempt in range(1, max_retries + 1):
        try:
            with open(file_path, "rb") as f:
                files = {"audio": (os.path.basename(file_path), f)}
                data = {"chat_id": chat_id}
                if title:
                    data["title"] = title[:64]  # TG title 限制 64 字符
                if caption:
                    data["caption"] = caption[:1024]  # TG caption 限制 1024 字符

                resp = requests.post(api_url, data=data, files=files, timeout=300)

            result = resp.json()

            if resp.status_code == 200 and result.get("ok"):
                msg = result.get("result", {})
                audio = msg.get("audio", {})
                file_id = audio.get("file_id", "")
                message_id = msg.get("message_id", 0)
                logger.info(f"[TG上传] 成功: {os.path.basename(file_path)} "
                           f"({file_size // 1024}KB) msg_id={message_id}")
                return {
                    "ok": True,
                    "file_id": file_id,
                    "message_id": message_id,
                    "bot_user_id": bot_user_id,
                    "error": "",
                    "file_size": file_size,
                }

            # 429 限流
            if resp.status_code == 429:
                retry_after = result.get("parameters", {}).get("retry_after", 5)
                logger.warning(f"[TG上传] 触发限流 (429)，等待 {retry_after} 秒")
                time.sleep(retry_after)
                continue

            error_desc = result.get("description", "未知错误")
            logger.warning(f"[TG上传] 失败 (尝试 {attempt}/{max_retries}): {error_desc}")

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[TG上传] 连接异常 (尝试 {attempt}/{max_retries}): {e}")
        except Exception as e:
            logger.warning(f"[TG上传] 异常 (尝试 {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            base_delay = 5 * attempt
            jitter = random.uniform(0, base_delay * 0.5)
            time.sleep(base_delay + jitter)

    return {"ok": False, "file_id": "", "message_id": 0, "bot_user_id": bot_user_id,
            "error": f"超出最大重试次数 ({max_retries})", "file_size": file_size}


def upload_document_to_telegram(
    file_path: str,
    bot_token: str,
    chat_id: str,
    title: str = "",
    caption: str = "",
    max_retries: int = 3,
) -> dict:
    """大文件用 sendDocument 上传（限制 2GB，但需要 Bot 在频道中有权限）。"""
    bot_user_id = extract_bot_user_id(bot_token)
    api_url = f"{_TG_API_BASE}/bot{bot_token}/sendDocument"

    for attempt in range(1, max_retries + 1):
        try:
            with open(file_path, "rb") as f:
                files = {"document": (os.path.basename(file_path), f)}
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:1024]

                resp = requests.post(api_url, data=data, files=files, timeout=600)

            result = resp.json()

            if resp.status_code == 200 and result.get("ok"):
                msg = result.get("result", {})
                doc = msg.get("document", {})
                file_id = doc.get("file_id", "")
                message_id = msg.get("message_id", 0)
                file_size = os.path.getsize(file_path)
                logger.info(f"[TG上传] sendDocument 成功: {os.path.basename(file_path)} "
                           f"({file_size // 1024}KB) msg_id={message_id}")
                return {
                    "ok": True,
                    "file_id": file_id,
                    "message_id": message_id,
                    "bot_user_id": bot_user_id,
                    "error": "",
                    "file_size": file_size,
                }

            if resp.status_code == 429:
                retry_after = result.get("parameters", {}).get("retry_after", 5)
                logger.warning(f"[TG上传] 触发限流 (429)，等待 {retry_after} 秒")
                time.sleep(retry_after)
                continue

            error_desc = result.get("description", "未知错误")
            logger.warning(f"[TG上传] sendDocument 失败 (尝试 {attempt}/{max_retries}): {error_desc}")

        except Exception as e:
            logger.warning(f"[TG上传] sendDocument 异常 (尝试 {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            time.sleep(5 * attempt)

    return {"ok": False, "file_id": "", "message_id": 0, "bot_user_id": bot_user_id,
            "error": f"sendDocument 超出最大重试次数 ({max_retries})",
            "file_size": os.path.getsize(file_path) if os.path.exists(file_path) else 0}


def upload_with_token_rotation(
    file_path: str,
    bot_tokens: list[str],
    chat_id: str,
    title: str = "",
    caption: str = "",
    serial: bool = True,
    interval: float = 3.0,
) -> dict:
    """多 Bot Token 轮换上传。

    使用 round-robin 选择起始 Token，失败时尝试下一个。
    serial=True 时使用锁确保串行上传（避免 TG 限流）。

    返回结果中额外包含 bot_token_idx: 实际成功上传的 Token 在原数组中的索引。
    """
    if not bot_tokens:
        return {"ok": False, "file_id": "", "message_id": 0, "bot_user_id": None,
                "bot_token_idx": None,
                "error": "无可用 Bot Token", "file_size": 0}

    # Round-robin 选择起始 Token 索引
    global _token_counter
    with _token_counter_lock:
        start_idx = _token_counter % len(bot_tokens)
        _token_counter = (_token_counter + 1) % len(bot_tokens)

    # 重排 Token 列表，从 start_idx 开始
    ordered_tokens = bot_tokens[start_idx:] + bot_tokens[:start_idx]
    used_idx = start_idx  # 实际使用的 Token 在原数组中的索引

    if serial:
        while not _UPLOAD_LOCK.acquire(timeout=2):
            pass

    try:
        result = upload_audio_to_telegram(
            file_path, ordered_tokens[0], chat_id, title, caption,
        )

        if not result.get("ok") and len(ordered_tokens) > 1:
            for i, token in enumerate(ordered_tokens[1:], 1):
                logger.info(f"[TG上传] Token {start_idx} 失败，尝试 Token {(start_idx + i) % len(bot_tokens)}...")
                result = upload_audio_to_telegram(
                    file_path, token, chat_id, title, caption,
                )
                if result.get("ok"):
                    used_idx = (start_idx + i) % len(bot_tokens)
                    break

        if result.get("ok") and interval > 0:
            time.sleep(interval)

        result["bot_token_idx"] = used_idx
        return result
    finally:
        if serial:
            _UPLOAD_LOCK.release()
