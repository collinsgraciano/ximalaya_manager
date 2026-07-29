#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""喜马拉雅有声书 Colab Worker — 轮询 VPS 认领任务并处理。

工作流程：
1. 轮询 VPS API 认领任务 (GET /api/jobs/claim)
2. 对每个章节：
   a. 下载音频 (喜马拉雅 mobile-playpage API + AES 解密)
   b. DeepFilter 降噪 (可选)
   c. 上传到 Telegram (Bot API sendAudio)
   d. 上报结果 (POST /api/jobs/{job_id}/chapter)
3. 全部完成后标记任务完成 (POST /api/jobs/{job_id}/complete)

用法（Colab 中运行）：
    # 安装依赖
    !pip install requests pycryptodome tqdm pydub

    # 设置参数后运行
    VPS_URL = "http://your-vps:59388"
    WORKER_ID = "colab_001"
    WORKER_TOKEN = "your_worker_token"

    !python ximalaya_colab_worker.py --vps-url $VPS_URL --worker-id $WORKER_ID --worker-token $WORKER_TOKEN
"""

from __future__ import annotations

import os
import sys
import time
import json
import shutil
import tempfile
import logging
import argparse
import threading
import requests

# 修复 Windows/Colab 控制台编码
if hasattr(sys, 'stdout'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("colab_worker")

# ═══════════════════════════════════════════════════════════
# 依赖检查
# ═══════════════════════════════════════════════════════════

def ensure_deps():
    """安装缺失的依赖。"""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        logger.info("安装 pycryptodome...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pycryptodome"])

    try:
        from tqdm import tqdm
    except ImportError:
        logger.info("安装 tqdm...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "tqdm"])

    try:
        from pydub import AudioSegment
    except ImportError:
        logger.info("安装 pydub...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pydub"])


# ═══════════════════════════════════════════════════════════
# Colab Worker
# ═══════════════════════════════════════════════════════════

class ColabWorker:
    """Colab Worker 客户端。"""

    def __init__(self, vps_url: str, worker_id: str, worker_token: str):
        self.vps_url = vps_url.rstrip("/")
        self.worker_id = worker_id
        self.worker_token = worker_token
        self.config: dict = {}
        self._heartbeat_stop = threading.Event()

    # ─── HTTP 工具 ───

    def _get(self, path: str, params: dict | None = None) -> dict:
        """GET 请求 VPS API。"""
        params = params or {}
        params["worker_token"] = self.worker_token
        params["worker_id"] = self.worker_id
        url = f"{self.vps_url}{path}"
        resp = requests.get(url, params=params, timeout=30)
        return resp.json()

    def _post(self, path: str, data: dict | None = None) -> dict:
        """POST 请求 VPS API。"""
        url = f"{self.vps_url}{path}"
        resp = requests.post(
            url,
            json=data or {},
            params={"worker_token": self.worker_token, "worker_id": self.worker_id},
            timeout=30,
        )
        return resp.json()

    # ─── 心跳 ───

    def _heartbeat_loop(self):
        """后台心跳线程，每 30s 发送一次。"""
        while not self._heartbeat_stop.is_set():
            try:
                self._post("/api/worker/heartbeat", {"worker_id": self.worker_id})
            except Exception:
                pass
            self._heartbeat_stop.wait(30)

    def start_heartbeat(self):
        t = threading.Thread(target=self._heartbeat_loop, daemon=True)
        t.start()
        logger.info("心跳线程已启动")

    def stop_heartbeat(self):
        self._heartbeat_stop.set()

    # ─── 配置 ───

    def fetch_config(self) -> dict:
        """从 VPS 获取配置。"""
        try:
            resp = self._get("/api/config", {"worker_id": self.worker_id})
            if resp.get("ok"):
                self.config = resp.get("config", {})
                logger.info(f"配置获取成功: TG tokens={len(self.config.get('tg_bot_tokens', []))}, "
                           f"deepfilter={self.config.get('enable_deepfilter', True)}")

                # 初始化代理池
                self._init_proxy()

                return self.config
        except Exception as e:
            logger.error(f"配置获取失败: {e}")
        return {}

    def _init_proxy(self):
        """根据配置初始化代理池（仅一次，后续调用直接跳过）。"""
        from pipeline.proxy_pool import get_pool
        if get_pool() is not None:
            return  # 已初始化，跳过

        if not self.config.get("proxy_enabled"):
            return

        proxy_list = self.config.get("proxy_list", [])

        # 当手动列表为空时，从 PROXY_LIST_URL 自动发现中国代理
        if not proxy_list:
            list_url = self.config.get("proxy_list_url", "")
            if list_url:
                from pipeline.proxy_pool import auto_discover_proxies
                verify_country = self.config.get("proxy_verify_country", "中国")
                max_tests = int(self.config.get("proxy_max_tests", 100))
                timeout = int(self.config.get("proxy_timeout", 10))
                logger.info(f"PROXY_LIST 为空，从 URL 自动发现代理: {list_url}")
                proxy_list = auto_discover_proxies(
                    list_url=list_url,
                    verify_country=verify_country,
                    max_tests=max_tests,
                    timeout=min(timeout, 5),
                )

        if not proxy_list:
            logger.warning("代理已启用但无可用代理（手动列表和自动发现均为空）")
            return

        from pipeline.proxy_pool import init_pool
        init_pool(
            proxy_list=proxy_list,
            test_url=self.config.get("proxy_test_url", "https://www.ximalaya.com"),
            dead_retry_minutes=int(self.config.get("proxy_dead_retry_minutes", 5)),
            timeout=int(self.config.get("proxy_timeout", 10)),
        )
        pool = get_pool()
        if pool:
            stats = pool.health_check()
            logger.info(f"代理池初始化: {stats['alive']}/{stats['total']} 可用")

    # ─── 任务认领 ───

    def claim_job(self) -> dict | None:
        """认领任务。"""
        try:
            resp = self._get("/api/jobs/claim", {"worker_id": self.worker_id})
            if resp.get("ok"):
                job = resp.get("job", {})
                logger.info(f"认领任务 #{job.get('job_id')}: {job.get('book_name', '')}")
                return job
        except Exception as e:
            logger.error(f"认领任务失败: {e}")
        return None

    # ─── 章节处理 ───

    def process_chapter(self, job_id: int, chapter: dict, book_id: str) -> dict:
        """处理单个章节：下载 → 降噪 → 上传TG → 上报。

        返回上报结果。
        """
        chapter_id = chapter["chapter_id"]
        chapter_name = chapter.get("chapter_name", "")

        # 从 audio_url 提取 trackId
        # audio_url 格式: https://www.ximalaya.com/sound/{trackId}
        track_id = chapter_id  # chapter_id 就是 trackId

        # ─── 1. 下载音频 ───
        logger.info(f"  下载章节: {chapter_name} (trackId={track_id})")

        tmp_dir = tempfile.mkdtemp(prefix="xm_chapter_")
        audio_path = os.path.join(tmp_dir, f"{chapter.get('chapter_order', 0):04d}_{track_id}.m4a")

        try:
            # 使用喜马拉雅 API 下载
            from pipeline.ximalaya_api import download_track, parse_quality_priority

            cookie = self.config.get("xm_cookie", "")
            headers = {"Cookie": cookie} if cookie else None

            download_interval = self.config.get("download_interval", 1.5)

            # 获取代理
            from pipeline.proxy_pool import get_pool
            pool = get_pool()
            proxies = pool.get() if pool else None

            # 音质优先级
            quality_priority = parse_quality_priority(self.config.get("audio_quality"))

            status, file_size = download_track(track_id, audio_path, headers=headers,
                                                proxies=proxies, quality_priority=quality_priority)
            if status not in ("downloaded", "skipped"):
                # 下载失败时标记代理不可用
                if pool and proxies:
                    proxy_url = proxies.get("http") or proxies.get("https")
                    if proxy_url:
                        pool.mark_dead(proxy_url)
                return self._report_chapter(job_id, chapter_id, "failed", error_message=f"下载失败: {status}")
            if status == "skipped" and file_size < 1000:
                return self._report_chapter(job_id, chapter_id, "failed", error_message="文件太小")

            time.sleep(download_interval)

            # ─── 2. DeepFilter 降噪 ───
            if self.config.get("enable_deepfilter", True):
                logger.info(f"  降噪中: {chapter_name}")
                try:
                    from pipeline.deepfilter import denoise_audio_keep_format, setup_deep_filter

                    # 确保二进制就绪
                    if not os.path.exists(
                        os.path.join(os.environ.get("DEEPFILTER_DIR", "/content/.deepfilter"),
                                     "deep-filter-0.5.6-x86_64-unknown-linux-musl")
                    ):
                        setup_deep_filter()

                    seg_min = self.config.get("deepfilter_segment_minutes", 60)
                    model = self.config.get("deepfilter_model", "DeepFilterNet2")
                    denoised_path = audio_path.replace(".m4a", "_denoised.m4a")
                    denoised_path = denoise_audio_keep_format(audio_path, denoised_path, seg_min, model=model)

                    # 用降噪后的文件
                    if os.path.exists(denoised_path) and os.path.getsize(denoised_path) > 0:
                        audio_path = denoised_path
                except Exception as e:
                    logger.warning(f"  降噪失败，使用原始音频: {e}")

            # ─── 3. 上传到 Telegram ───
            logger.info(f"  上传TG: {chapter_name}")
            from pipeline.tg_upload import upload_with_token_rotation

            bot_tokens = self.config.get("tg_bot_tokens", [])
            chat_id = self.config.get("tg_chat_id", "")
            serial = self.config.get("tg_serial_upload", True)
            interval = self.config.get("tg_upload_interval", 3.0)

            if not bot_tokens or not chat_id:
                return self._report_chapter(job_id, chapter_id, "failed",
                                           error_message="TG Bot Token 或 Chat ID 未配置")

            result = upload_with_token_rotation(
                file_path=audio_path,
                bot_tokens=bot_tokens,
                chat_id=chat_id,
                title=chapter_name[:64],
                caption=chapter_name,
                serial=serial,
                interval=interval,
            )

            if not result.get("ok"):
                return self._report_chapter(job_id, chapter_id, "failed",
                                           error_message=result.get("error", "上传失败"))

            # ─── 4. 上报结果 ───
            return self._report_chapter(job_id, chapter_id, "uploaded",
                                       telegram_file_id=result.get("file_id", ""),
                                       telegram_message_id=result.get("message_id", 0),
                                       telegram_bot_id=result.get("bot_token_idx"),
                                       telegram_bot_user_id=result.get("bot_user_id"))

        except Exception as e:
            logger.error(f"  章节处理异常: {e}", exc_info=True)
            return self._report_chapter(job_id, chapter_id, "failed", error_message=str(e))
        finally:
            # 清理临时文件
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _report_chapter(self, job_id: int, chapter_id: str, upload_status: str,
                        telegram_file_id: str = "", telegram_message_id: int = 0,
                        telegram_bot_id: int | None = None,
                        telegram_bot_user_id: int | None = None,
                        error_message: str = "") -> dict:
        """上报章节处理结果。"""
        try:
            resp = self._post(f"/api/jobs/{job_id}/chapter", {
                "chapter_id": str(chapter_id),
                "upload_status": upload_status,
                "telegram_file_id": telegram_file_id,
                "telegram_message_id": telegram_message_id,
                "telegram_bot_id": telegram_bot_id,
                "telegram_bot_user_id": telegram_bot_user_id,
                "error_message": error_message,
            })
            status_text = "OK" if upload_status == "uploaded" else "FAIL"
            logger.info(f"  [{status_text}] 章节 {chapter_id}: {upload_status}"
                       + (f" file_id={telegram_file_id[:20]}..." if telegram_file_id else "")
                       + (f" err={error_message}" if error_message else ""))
            return resp
        except Exception as e:
            logger.error(f"  上报失败: {e}")
            return {"ok": False, "error": str(e)}

    # ─── 任务完成 ───

    def complete_job(self, job_id: int, result: dict | None = None):
        """标记任务完成。"""
        resp = self._post(f"/api/jobs/{job_id}/complete", {"result": result})
        logger.info(f"任务 #{job_id} 已完成")
        return resp

    def fail_job(self, job_id: int, error_message: str):
        """标记任务失败。"""
        resp = self._post(f"/api/jobs/{job_id}/fail", {"error_message": error_message})
        logger.error(f"任务 #{job_id} 失败: {error_message}")
        return resp

    def release_job(self):
        """退出时释放自己 processing 的任务。"""
        try:
            resp = self._post("/api/jobs/release", {})
            if resp.get("ok") and resp.get("released", 0) > 0:
                logger.info(f"已释放 {resp['released']} 个未完成任务")
        except Exception as e:
            logger.warning(f"释放任务失败: {e}")

    # ─── 主循环 ───

    def run(self, poll_interval: int = 10, max_jobs: int = 0):
        """主循环：轮询认领任务并处理。

        Args:
            poll_interval: 无任务时等待秒数
            max_jobs: 最大处理任务数 (0=不限)
        """
        logger.info(f"Colab Worker 启动: {self.worker_id}")
        logger.info(f"VPS: {self.vps_url}")

        # 获取配置
        self.fetch_config()

        # 启动心跳
        self.start_heartbeat()

        jobs_done = 0
        try:
            while True:
                if max_jobs > 0 and jobs_done >= max_jobs:
                    logger.info(f"已处理 {jobs_done} 个任务，退出")
                    break

                # 认领任务
                job = self.claim_job()
                if not job:
                    logger.info(f"无待处理任务，等待 {poll_interval}s...")
                    time.sleep(poll_interval)
                    continue

                job_id = job["job_id"]
                book_id = job.get("book_id", "")
                chapters = job.get("chapters", [])
                total = len(chapters)

                if not chapters:
                    # 无待处理章节，可能已全部上传
                    self.complete_job(job_id, {"note": "no pending chapters"})
                    jobs_done += 1
                    continue

                logger.info(f"开始处理任务 #{job_id}: {job.get('book_name', '')} ({total} 章节)"
                           + (" [找回之前未完成的任务]" if job.get("reclaimed") else ""))

                # 刷新配置（确保最新 TG token 等）
                self.fetch_config()

                success_count = 0
                fail_count = 0

                for i, chapter in enumerate(chapters):
                    logger.info(f"  [{i+1}/{total}] {chapter.get('chapter_name', '')}")

                    result = self.process_chapter(job_id, chapter, book_id)

                    if chapter.get("upload_status") == "uploaded" or (result and result.get("upload_status") == "uploaded"):
                        success_count += 1
                    else:
                        fail_count += 1

                # 标记任务完成
                if fail_count == 0:
                    self.complete_job(job_id, {"success": success_count, "failed": fail_count})
                else:
                    self.complete_job(job_id, {"success": success_count, "failed": fail_count,
                                               "note": f"{fail_count} chapters failed"})

                jobs_done += 1
                logger.info(f"任务 #{job_id} 完成: 成功={success_count}, 失败={fail_count}")

        except KeyboardInterrupt:
            logger.info("用户中断")
        finally:
            self.stop_heartbeat()
            self.release_job()
            logger.info("Worker 已停止")


# ═══════════════════════════════════════════════════════════
# 主函数
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="喜马拉雅 Colab Worker")
    parser.add_argument("--vps-url", required=True, help="VPS API 地址 (如 http://your-vps:59388)")
    parser.add_argument("--worker-id", default="", help="Worker ID (默认自动生成)")
    parser.add_argument("--worker-token", required=True, help="Worker 认证 Token")
    parser.add_argument("--poll-interval", type=int, default=10, help="无任务时等待秒数")
    parser.add_argument("--max-jobs", type=int, default=0, help="最大处理任务数 (0=不限)")
    parser.add_argument("--install-deps", action="store_true", help="自动安装依赖")
    args = parser.parse_args()

    # 生成 Worker ID
    worker_id = args.worker_id or f"colab_{os.urandom(4).hex()}"

    # 安装依赖
    if args.install_deps:
        ensure_deps()

    # 确保 pipeline 包可导入
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    for candidate in [parent_dir, script_dir, "/app"]:
        if os.path.isdir(os.path.join(candidate, "pipeline")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            break

    # 创建并运行 Worker
    worker = ColabWorker(
        vps_url=args.vps_url,
        worker_id=worker_id,
        worker_token=args.worker_token,
    )
    worker.run(poll_interval=args.poll_interval, max_jobs=args.max_jobs)


if __name__ == "__main__":
    main()
