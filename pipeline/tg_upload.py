"""Telegram 音频上传模块 — 多 Bot Token 轮换。"""

from __future__ import annotations

import os
import json
import time
import random
import threading
import logging
import subprocess
import tempfile
import requests

logger = logging.getLogger(__name__)

# TG API 基础 URL（支持 VPS 中继代理）
_TG_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

# 串行上传锁
_UPLOAD_LOCK = threading.Lock()

# Round-robin 计数器（多 Bot Token 轮换）
_token_counter = 0
_token_counter_lock = threading.Lock()


def compress_audio_if_needed(file_path: str, max_size_mb: int = 48) -> tuple[str, bool]:
    """如果音频文件超过 max_size_mb, 用 FFmpeg 降低码率压缩到限制以下。

    返回 (文件路径, 是否为临时文件)。
    - 不需要压缩: 返回 (原路径, False)
    - 压缩成功: 返回 (临时文件路径, True), 调用方负责删除
    """
    file_size = os.path.getsize(file_path)
    max_bytes = max_size_mb * 1024 * 1024

    if file_size <= max_bytes:
        return file_path, False

    logger.info(f"[TG上传] 文件过大 ({file_size // 1024 // 1024}MB), "
               f"开始压缩到 {max_size_mb}MB 以下...")

    # 获取音频时长
    try:
        # 获取音频编码信息（codec, sample_rate, channels）
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration:stream=codec_name,sample_rate,channels",
                 "-of", "json", file_path],
                capture_output=True, text=True, check=True, timeout=60,
            )
            probe = json.loads(r.stdout)
            duration = float(probe.get("format", {}).get("duration", 0))
            streams = probe.get("streams", [])
            audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        except Exception as e:
            logger.warning(f"[TG上传] 探测音频信息失败: {e}")
            return file_path, False

        if duration <= 0 or not audio_stream:
            logger.warning(f"[TG上传] 时长无效 ({duration}) 或无音频流, 无法压缩")
            return file_path, False

        # 计算目标码率 (bits/s), 留 5% 余量给容器开销
        target_bitrate = int(max_bytes * 8 / duration * 0.95)
        # 最低码率保护: 语音 24kbps 仍可听
        if target_bitrate < 24000:
            target_bitrate = 24000
            logger.warning(f"[TG上传] 目标码率过低, 限制为 24kbps")

        codec = audio_stream.get("codec_name", "")
        sample_rate = audio_stream.get("sample_rate", "44100")

        fd, compressed_path = tempfile.mkstemp(suffix=".m4a", prefix="compressed_")
        os.close(fd)

        def _run_ffmpeg(bitrate: int, use_vbr: bool = False) -> tuple[int, str]:
            """执行一次压缩, 返回 (exit_code, stderr_text)。"""
            cmd = [
                "ffmpeg", "-y",
                "-i", file_path,
                "-vn",                     # 不要视频流（封面图等）
                "-map", "0:a:0",           # 只取第一个音频流
                "-codec:a", "aac",
                "-ar", "44100",            # 标准化采样率
                "-ac", "2",
                "-movflags", "+faststart",
            ]
            if use_vbr:
                cmd += ["-q:a", "2"]       # VBR 质量模式 (2=高质量)
            else:
                cmd += ["-b:a", str(bitrate)]
            cmd.append(compressed_path)

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                return result.returncode, result.stderr
            except subprocess.TimeoutExpired:
                return 1, "压缩超时 (600s)"
            except Exception as e:
                return 1, str(e)

        # 第一次尝试：CBR 目标码率
        retcode, stderr_text = _run_ffmpeg(target_bitrate)
        if retcode != 0:
            logger.warning(f"[TG上传] 压缩失败 (CBR {target_bitrate//1000}kbps): {stderr_text[:2000]}")
            # 降级重试：VBR 质量模式
            logger.info(f"[TG上传] 降级重试: VBR 质量模式")
            retcode, stderr_text = _run_ffmpeg(0, use_vbr=True)

        if retcode != 0:
            logger.warning(f"[TG上传] 压缩降级也失败: {stderr_text[:2000]}")
            if os.path.exists(compressed_path):
                os.unlink(compressed_path)
            return file_path, False

        compressed_size = os.path.getsize(compressed_path)
        if compressed_size > 0 and compressed_size < file_size:
            logger.info(f"[TG上传] 压缩完成: {file_size // 1024 // 1024}MB -> "
                       f"{compressed_size // 1024 // 1024}MB "
                       f"(码率 {target_bitrate // 1000}kbps)")
            return compressed_path, True
        else:
            logger.warning(f"[TG上传] 压缩后文件无效或更大, 使用原文件")
            os.unlink(compressed_path)
            return file_path, False
    except Exception as e:
        logger.warning(f"[TG上传] 压缩失败: {e}")
        if os.path.exists(compressed_path):
            os.unlink(compressed_path)
        return file_path, False


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

    # 文件超过 48MB 时自动压缩 (TG Bot API 限制 50MB)
    upload_path, is_temp = compress_audio_if_needed(file_path)
    if is_temp:
        file_size = os.path.getsize(upload_path)

    try:
        # 压缩后仍超过 50MB, 用 sendDocument 最后尝试
        if file_size > 50 * 1024 * 1024:
            return upload_document_to_telegram(
                upload_path, bot_token, chat_id, title, caption, max_retries)

        api_url = f"{_TG_API_BASE}/bot{bot_token}/sendAudio"
        original_name = os.path.basename(file_path)

        for attempt in range(1, max_retries + 1):
            try:
                with open(upload_path, "rb") as f:
                    files = {"audio": (original_name, f)}
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
                    logger.info(f"[TG上传] 成功: {original_name} "
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
    finally:
        if is_temp and os.path.exists(upload_path):
            os.unlink(upload_path)


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

    # 预压缩大文件 (仅一次, 避免多 token 重试时重复压缩)
    actual_path, is_temp = compress_audio_if_needed(file_path)

    # Round-robin 选择起始 Token 索引
    global _token_counter
    with _token_counter_lock:
        start_idx = _token_counter % len(bot_tokens)
        _token_counter = (_token_counter + 1) % len(bot_tokens)

    # 重排 Token 列表，从 start_idx 开始
    ordered_tokens = bot_tokens[start_idx:] + bot_tokens[:start_idx]
    used_idx = start_idx  # 实际使用的 Token 在原数组中的索引

    acquired = False
    if serial:
        _UPLOAD_LOCK.acquire()
        acquired = True

    try:
        result = upload_audio_to_telegram(
            actual_path, ordered_tokens[0], chat_id, title, caption,
        )

        if not result.get("ok") and len(ordered_tokens) > 1:
            for i, token in enumerate(ordered_tokens[1:], 1):
                logger.info(f"[TG上传] Token {start_idx} 失败，尝试 Token {(start_idx + i) % len(bot_tokens)}...")
                result = upload_audio_to_telegram(
                    actual_path, token, chat_id, title, caption,
                )
                if result.get("ok"):
                    used_idx = (start_idx + i) % len(bot_tokens)
                    break

        if result.get("ok") and interval > 0:
            time.sleep(interval)

        result["bot_token_idx"] = used_idx
        return result
    finally:
        if acquired:
            _UPLOAD_LOCK.release()
        if is_temp and os.path.exists(actual_path):
            os.unlink(actual_path)
