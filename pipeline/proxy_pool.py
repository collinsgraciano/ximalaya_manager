"""代理池 — 按延迟排序 + 健康检测 + 死亡标记。

VPS 采集端和 Colab 下载端共用。
优先使用延迟低速度快的代理，死亡代理超时后自动恢复并重新测速。
"""

from __future__ import annotations

import time
import logging
import threading
import requests

logger = logging.getLogger(__name__)


def build_proxies_dict(proxy_url: str) -> dict:
    """将单个代理 URL 转为 requests 的 proxies 字典。"""
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}


class ProxyPool:
    """代理池，按延迟排序优先使用最快代理。"""

    def __init__(
        self,
        proxy_list: list[str],
        test_url: str = "https://www.ximalaya.com",
        dead_retry_minutes: int = 5,
        timeout: int = 10,
    ):
        self._test_url = test_url
        self._dead_retry_sec = dead_retry_minutes * 60
        self._timeout = timeout
        self._lock = threading.Lock()

        # 去重
        seen = set()
        self._all_proxies: list[str] = []
        for p in proxy_list:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                self._all_proxies.append(p)

        # 按延迟排序后的可用代理列表（health_check 后填充）
        self._sorted_proxies: list[str] = list(self._all_proxies)

        # proxy_url → 延迟秒数（-1 = 未测速）
        self._latency_map: dict[str, float] = {p: -1.0 for p in self._all_proxies}

        # proxy_url → dead_until timestamp (0 = alive)
        self._dead_map: dict[str, float] = {}

        # round-robin 计数器（在同延迟段内轮换）
        self._counter = 0

        if self._all_proxies:
            logger.info(f"代理池初始化: {len(self._all_proxies)} 个代理")

    def get(self) -> dict:
        """返回当前可用的最快代理，无可用时返回 {}（直连）。

        从按延迟排序的列表中选取第一个未死亡的代理。
        """
        if not self._sorted_proxies:
            return {}

        with self._lock:
            now = time.time()
            alive_proxies: list[str] = []

            for proxy_url in self._sorted_proxies:
                dead_until = self._dead_map.get(proxy_url, 0)
                if dead_until and now < dead_until:
                    continue  # 还在死亡期

                # 复活了，清除死亡标记
                if dead_until:
                    self._dead_map.pop(proxy_url, None)
                    logger.info(f"代理恢复: {proxy_url}")

                alive_proxies.append(proxy_url)

            if not alive_proxies:
                logger.warning("所有代理均不可用，使用直连")
                return {}

            # 在可用代理中，优先选延迟最低的
            # 使用 counter 在相同优先级内做 round-robin，避免总打同一个
            idx = self._counter % len(alive_proxies)
            self._counter += 1
            chosen = alive_proxies[idx]
            latency = self._latency_map.get(chosen, -1)
            latency_str = f"{latency:.2f}s" if latency > 0 else "未测速"
            logger.debug(f"选择代理: {chosen} (延迟 {latency_str})")

            return build_proxies_dict(chosen)

    def mark_dead(self, proxy_url: str):
        """标记代理临时不可用。"""
        with self._lock:
            self._dead_map[proxy_url] = time.time() + self._dead_retry_sec
            logger.warning(f"代理标记死亡: {proxy_url} ({self._dead_retry_sec // 60}分钟后重试)")

    def health_check(self) -> dict:
        """检测所有代理的连通性和延迟，按延迟重新排序。

        返回状态摘要，包含每个代理的延迟信息。
        """
        results = {"total": len(self._all_proxies), "alive": 0, "dead": 0, "details": []}
        if not self._all_proxies:
            return results

        for proxy_url in self._all_proxies:
            latency, ok = self._measure_latency(proxy_url)
            detail = {"proxy": proxy_url, "latency": latency, "ok": ok}
            results["details"].append(detail)

            if ok:
                results["alive"] += 1
                with self._lock:
                    self._dead_map.pop(proxy_url, None)
                    self._latency_map[proxy_url] = latency
                logger.info(f"[健康检测] 代理可用: {proxy_url} (延迟 {latency:.2f}s)")
            else:
                results["dead"] += 1
                self.mark_dead(proxy_url)
                with self._lock:
                    self._latency_map[proxy_url] = -1.0

        # 按延迟升序排序（快的在前），未测速/死亡的排最后
        with self._lock:
            self._sorted_proxies = sorted(
                self._all_proxies,
                key=lambda p: self._latency_map.get(p, 999) if self._latency_map.get(p, -1) > 0 else 999,
            )

        # 打印排序结果
        sorted_str = " > ".join(
            f"{p}({self._latency_map.get(p, -1):.2f}s)" if self._latency_map.get(p, -1) > 0
            else f"{p}(dead)"
            for p in self._sorted_proxies
        )
        logger.info(f"[健康检测] 代理延迟排序: {sorted_str}")

        return results

    def _measure_latency(self, proxy_url: str) -> tuple[float, bool]:
        """测量单个代理的连接延迟。

        返回 (延迟秒数, 是否可用)。
        """
        proxies = build_proxies_dict(proxy_url)
        try:
            start = time.time()
            resp = requests.get(
                self._test_url,
                proxies=proxies,
                timeout=self._timeout,
                allow_redirects=False,
            )
            elapsed = time.time() - start
            if resp.status_code < 500:
                return elapsed, True
            else:
                logger.info(f"[健康检测] 代理返回错误状态: {proxy_url} (HTTP {resp.status_code})")
                return 0, False
        except Exception as e:
            logger.info(f"[健康检测] 代理不可用: {proxy_url} ({e})")
            return 0, False

    def stats(self) -> dict:
        """返回代理池当前状态摘要。"""
        now = time.time()
        with self._lock:
            alive = sum(
                1 for p in self._all_proxies
                if not self._dead_map.get(p, 0) or now >= self._dead_map.get(p, 0)
            )
            dead = len(self._all_proxies) - alive
        return {
            "total": len(self._all_proxies),
            "alive": alive,
            "dead": dead,
            "sorted": list(self._sorted_proxies),
        }


# ═══════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════

_pool: ProxyPool | None = None


def init_pool(
    proxy_list: list[str],
    test_url: str = "https://www.ximalaya.com",
    dead_retry_minutes: int = 5,
    timeout: int = 10,
) -> ProxyPool:
    """初始化全局代理池。"""
    global _pool
    _pool = ProxyPool(proxy_list, test_url, dead_retry_minutes, timeout)
    return _pool


def get_pool() -> ProxyPool | None:
    """获取全局代理池（如未初始化返回 None）。"""
    return _pool


def get_proxy() -> dict:
    """便捷方法：从全局代理池获取下一个可用代理。"""
    if _pool is None:
        return {}
    return _pool.get()
