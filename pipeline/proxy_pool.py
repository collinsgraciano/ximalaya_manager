"""代理池 — 自动发现 + 按延迟排序 + 健康检测 + 死亡踢出。

VPS 采集端和 Colab 下载端共用。
支持从 URL 自动获取代理列表并用 ip-api.com 筛选中国代理。
优先使用延迟低速度快的代理，死亡代理直接踢出不再重试。
"""

from __future__ import annotations

import time
import random
import logging
import threading
import requests
import concurrent.futures

logger = logging.getLogger(__name__)


def build_proxies_dict(proxy_url: str) -> dict:
    """将单个代理 URL 转为 requests 的 proxies 字典。"""
    if not proxy_url:
        return {}
    return {"http": proxy_url, "https": proxy_url}


def _test_proxy_china(proxy_full_str: str, timeout: int = 5) -> tuple[str, str] | None:
    """测试代理是否在中国且网络连通，返回 (proxy_url, 位置描述) 或 None。

    使用 http://ip-api.com/json/?lang=zh-CN 检测代理出口 IP 的地理位置。
    """
    proxies = {"http": proxy_full_str, "https": proxy_full_str}
    try:
        resp = requests.get(
            "http://ip-api.com/json/?lang=zh-CN",
            proxies=proxies,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            country = data.get("country", "Unknown")
            city = data.get("city", "")
            if country in ("China", "中国"):
                location = f"{country} - {city}" if city else country
                return proxy_full_str, location
    except Exception:
        pass
    return None


def _fetch_proxy_list_from_url(url: str) -> list[str]:
    """从单个 URL 获取代理列表，返回去重后的列表。"""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        # 解析空格或换行分隔的列表
        raw_list = [p.strip() for p in resp.text.replace("\n", " ").split(" ") if p.strip()]
    except Exception as e:
        logger.warning(f"获取代理列表失败 ({url}): {e}")
        return []

    # 补全协议前缀
    normalized: list[str] = []
    seen = set()
    for p in raw_list:
        if not p.startswith(("http://", "https://", "socks5://", "socks4://")):
            p = f"http://{p}"
        if p not in seen:
            seen.add(p)
            normalized.append(p)
    return normalized


def _fetch_multiple_urls_concurrent(urls: list[str]) -> list[str]:
    """并发从多个 URL 获取代理列表，合并去重返回。"""
    all_candidates: list[str] = []
    seen = set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(urls), 10)) as executor:
        future_to_url = {executor.submit(_fetch_proxy_list_from_url, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                proxies_from_url = future.result()
                logger.info(f"从 {url} 获取 {len(proxies_from_url)} 个候选代理")
                for p in proxies_from_url:
                    if p not in seen:
                        seen.add(p)
                        all_candidates.append(p)
            except Exception as e:
                logger.warning(f"获取代理列表失败 ({url}): {e}")

    return all_candidates


def auto_discover_proxies(
    list_url: str,
    verify_country: str = "中国",
    max_tests: int = 100,
    timeout: int = 5,
    fallback_list: list[str] | None = None,
) -> list[str]:
    """从一个或多个 URL 获取代理列表，并发检测筛选中国代理。

    Args:
        list_url: 代理列表 URL，多个用逗号或换行分隔。返回 text（每行一个 ip:port 或 protocol://ip:port）
        verify_country: 目标国家名（中英文均可），如 "中国" / "China"
        max_tests: 最多测试的候选代理数量
        timeout: 单个代理测试超时秒数
        fallback_list: 在线列表获取失败时的备用代理

    Returns:
        筛选后的中国代理 URL 列表（可能为空）
    """
    # 解析多个 URL（逗号或换行分隔）
    urls = [u.strip() for u in list_url.replace("\n", ",").split(",") if u.strip()]
    if not urls:
        logger.warning("未配置代理列表 URL")
        return []

    # 并发从所有 URL 获取代理并合并去重
    all_candidates = _fetch_multiple_urls_concurrent(urls)

    if not all_candidates:
        logger.warning("代理列表为空（所有 URL 获取失败）")
        if fallback_list:
            all_candidates = list(fallback_list)
        if not all_candidates:
            return []

    test_list = all_candidates[:max_tests]
    logger.info(f"共 {len(all_candidates)} 个候选代理 (去重后), 正在验证前 {len(test_list)} 个 (筛选国家: {verify_country})...")

    found: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_test_proxy_china, p, timeout): p for p in test_list}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                proxy_url, location = result
                found.append(proxy_url)
                logger.info(f"  匹配成功: {proxy_url} ({location})")

    if found:
        logger.info(f"成功找到 {len(found)} 个中国代理")
    else:
        logger.warning("列表中无可用中国代理")

    return found


class ProxyPool:
    """代理池，按延迟排序优先使用最快代理。死亡代理直接踢出。"""

    def __init__(
        self,
        proxy_list: list[str],
        test_url: str = "https://www.ximalaya.com",
        dead_retry_minutes: int = 5,
        timeout: int = 10,
        on_dead=None,
    ):
        self._test_url = test_url
        self._timeout = timeout
        self._lock = threading.Lock()
        self._on_dead = on_dead  # 回调: on_dead(proxy_url) 代理被踢出时调用

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

        # round-robin 计数器（随机起始, 避免多实例同时命中同一代理）
        self._counter = random.randint(0, max(len(self._all_proxies) - 1, 0))

        # 是否已做过 health_check
        self._checked = False

        if self._all_proxies:
            logger.info(f"代理池初始化: {len(self._all_proxies)} 个代理")

    def get(self) -> dict:
        """返回当前可用的最快代理，无可用时返回 {}（直连）。

        从按延迟排序的列表中 round-robin 选取。
        """
        if not self._sorted_proxies:
            return {}

        with self._lock:
            if not self._sorted_proxies:
                return {}

            idx = self._counter % len(self._sorted_proxies)
            self._counter += 1
            chosen = self._sorted_proxies[idx]
            latency = self._latency_map.get(chosen, -1)
            latency_str = f"{latency:.2f}s" if latency > 0 else "未测速"
            logger.debug(f"选择代理: {chosen} (延迟 {latency_str})")

            return build_proxies_dict(chosen)

    def get_random(self) -> dict:
        """随机返回一个可用代理，无可用时返回 {}（直连）。"""
        with self._lock:
            if not self._sorted_proxies:
                return {}
            chosen = random.choice(self._sorted_proxies)
            logger.debug(f"随机选择代理: {chosen}")
            return build_proxies_dict(chosen)

    def mark_dead(self, proxy_url: str):
        """将代理永久踢出代理池，并触发 on_dead 回调。"""
        with self._lock:
            if proxy_url in self._all_proxies:
                self._all_proxies.remove(proxy_url)
            if proxy_url in self._sorted_proxies:
                self._sorted_proxies.remove(proxy_url)
            self._latency_map.pop(proxy_url, None)
            remaining = len(self._sorted_proxies)
            logger.warning(f"代理踢出: {proxy_url} (剩余 {remaining} 个)")

        # 回调通知外部（如 VPS 端更新 DB 缓存）
        if self._on_dead:
            try:
                self._on_dead(proxy_url)
            except Exception as e:
                logger.warning(f"on_dead 回调失败: {e}")

    def add_proxies(self, new_proxies: list[str]) -> int:
        """合并新代理到池中（去重），只检测新代理，返回新增数量。"""
        with self._lock:
            existing = set(self._all_proxies)
            truly_new = [p.strip() for p in new_proxies if p.strip() and p.strip() not in existing]
            if not truly_new:
                return 0
            self._all_proxies.extend(truly_new)
            for p in truly_new:
                self._latency_map[p] = -1.0

        logger.info(f"代理池新增 {len(truly_new)} 个代理 (去重后), 开始检测...")

        # 并发检测新代理
        alive_results: list[tuple[str, float]] = []
        dead_proxies: list[str] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(self._measure_latency, p): p for p in truly_new}
            for future in concurrent.futures.as_completed(futures):
                proxy_url = futures[future]
                try:
                    latency, ok = future.result()
                except Exception:
                    latency, ok = 0, False
                if ok:
                    alive_results.append((proxy_url, latency))
                    logger.info(f"[补充检测] 新代理可用: {proxy_url} (延迟 {latency:.2f}s)")
                else:
                    dead_proxies.append(proxy_url)

        with self._lock:
            for proxy_url, latency in alive_results:
                self._latency_map[proxy_url] = latency
                if proxy_url not in self._sorted_proxies:
                    self._sorted_proxies.append(proxy_url)

        # 踢出检测失败的新代理
        for p in dead_proxies:
            self.mark_dead(p)

        added = len(alive_results)

        # 重新排序
        with self._lock:
            self._sorted_proxies = sorted(
                self._sorted_proxies,
                key=lambda p: self._latency_map.get(p, 999) if self._latency_map.get(p, -1) > 0 else 999,
            )

        logger.info(f"代理池补充完成: 新增 {added} 个可用, 总计 {len(self._sorted_proxies)} 个")
        return added

    def health_check(self) -> dict:
        """并发检测所有代理的连通性和延迟，按延迟重新排序。

        只应在初始化时调用一次。死亡代理直接踢出。
        """
        results = {"total": len(self._all_proxies), "alive": 0, "dead": 0, "details": []}
        if not self._all_proxies:
            return results

        all_proxies = list(self._all_proxies)

        # 并发检测所有代理
        alive_results: list[tuple[str, float]] = []
        dead_proxies: list[str] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(self._measure_latency, p): p for p in all_proxies}
            for future in concurrent.futures.as_completed(futures):
                proxy_url = futures[future]
                try:
                    latency, ok = future.result()
                except Exception:
                    latency, ok = 0, False
                results["details"].append({"proxy": proxy_url, "latency": latency, "ok": ok})
                if ok:
                    alive_results.append((proxy_url, latency))
                    logger.info(f"[健康检测] 代理可用: {proxy_url} (延迟 {latency:.2f}s)")
                else:
                    dead_proxies.append(proxy_url)

        results["alive"] = len(alive_results)
        results["dead"] = len(dead_proxies)

        with self._lock:
            for proxy_url, latency in alive_results:
                self._latency_map[proxy_url] = latency

        # 踢出死亡代理
        for p in dead_proxies:
            self.mark_dead(p)

        # 按延迟升序排序（快的在前）
        with self._lock:
            self._sorted_proxies = sorted(
                self._sorted_proxies,
                key=lambda p: self._latency_map.get(p, 999) if self._latency_map.get(p, -1) > 0 else 999,
            )
            self._checked = True

        if self._sorted_proxies:
            sorted_str = " > ".join(
                f"{p}({self._latency_map.get(p, -1):.2f}s)"
                for p in self._sorted_proxies
                if self._latency_map.get(p, -1) > 0
            )
            logger.info(f"[健康检测] 可用代理排序: {sorted_str}")
        else:
            logger.warning("[健康检测] 无可用代理")

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
        with self._lock:
            return {
                "total": len(self._all_proxies),
                "alive": len(self._sorted_proxies),
                "dead": 0,
                "sorted": list(self._sorted_proxies),
            }

    def get_alive_proxies(self) -> list[str]:
        """返回当前存活的代理列表（副本）。"""
        with self._lock:
            return list(self._sorted_proxies)


# ═══════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════

_pool: ProxyPool | None = None


def init_pool(
    proxy_list: list[str],
    test_url: str = "https://www.ximalaya.com",
    dead_retry_minutes: int = 5,
    timeout: int = 10,
    on_dead=None,
) -> ProxyPool:
    """初始化全局代理池。"""
    global _pool
    _pool = ProxyPool(proxy_list, test_url, dead_retry_minutes, timeout, on_dead=on_dead)
    return _pool


def get_pool() -> ProxyPool | None:
    """获取全局代理池（如未初始化返回 None）。"""
    return _pool


def get_proxy() -> dict:
    """便捷方法：从全局代理池获取下一个可用代理。"""
    if _pool is None:
        return {}
    return _pool.get()


def get_random_proxy() -> dict:
    """便捷方法：从全局代理池随机获取一个可用代理。"""
    if _pool is None:
        return {}
    return _pool.get_random()
