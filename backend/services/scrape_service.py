"""采集服务 — 喜马拉雅分类采集 + 章节列表获取。"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from psycopg import sql
from psycopg.types.json import Jsonb
from datetime import datetime

from ..database import fetch_one, fetch_all, fetch_val, execute, execute_returning, execute_batch
from pipeline.ximalaya_api import (
    scrape_category as _scrape_category,
    get_all_tracks as _get_all_tracks,
    get_album_info as _get_album_info,
    normalize_album_record,
    CATEGORIES,
    get_categories as _get_api_categories,
)
from pipeline.proxy_pool import init_pool, get_proxy, get_pool, get_random_proxy, auto_discover_proxies

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 代理池后台刷新线程
# ═══════════════════════════════════════════════════════════

_proxy_refresh_thread: threading.Thread | None = None
_proxy_refresh_stop = threading.Event()


def _proxy_refresh_loop():
    """后台线程：每隔 PROXY_REFRESH_HOURS 小时自动发现新代理并合并到池中。"""
    while not _proxy_refresh_stop.is_set():
        # 读取刷新间隔（每次从 DB 读，支持运行时修改）
        try:
            refresh_hours = float(fetch_val(
                "SELECT setting_value FROM public.global_settings WHERE setting_key = 'PROXY_REFRESH_HOURS'",
                (),
            ) or "2")
        except Exception:
            refresh_hours = 2.0
        if refresh_hours <= 0:
            refresh_hours = 2.0

        wait_sec = refresh_hours * 3600
        logger.info(f"[代理刷新] 下次刷新: {refresh_hours:.1f} 小时后")

        # 等待（每分钟检查一次 stop 信号）
        for _ in range(int(wait_sec // 60)):
            if _proxy_refresh_stop.wait(timeout=60):
                return

        # 执行刷新
        try:
            _do_proxy_refresh()
        except Exception as e:
            logger.error(f"[代理刷新] 刷新失败: {e}")


def _do_proxy_refresh():
    """执行一次代理刷新：从 URL 获取新代理，去重合并到池 + DB 缓存。"""
    pool = get_pool()
    if pool is None:
        return

    rows = fetch_all(
        "SELECT setting_key, setting_value FROM public.global_settings "
        "WHERE setting_key IN ('PROXY_LIST_URL', 'PROXY_VERIFY_COUNTRY', 'PROXY_MAX_TESTS', "
        "'PROXY_TIMEOUT', 'PROXY_VERIFIED_CACHE')"
    )
    settings_map = {row["setting_key"]: row["setting_value"] for row in (rows or [])}

    list_url = settings_map.get("PROXY_LIST_URL", "")
    if not list_url:
        logger.warning("[代理刷新] 未配置 PROXY_LIST_URL，跳过")
        return

    verify_country = settings_map.get("PROXY_VERIFY_COUNTRY", "中国")
    max_tests = int(settings_map.get("PROXY_MAX_TESTS", "100"))
    timeout = int(settings_map.get("PROXY_TIMEOUT", "10"))

    logger.info(f"[代理刷新] 从 URL 获取新鲜代理: {list_url}")
    fresh = auto_discover_proxies(
        list_url=list_url,
        verify_country=verify_country,
        max_tests=max_tests,
        timeout=min(timeout, 5),
    )
    if not fresh:
        logger.warning("[代理刷新] 未发现新代理")
        return

    # 合并到内存池（去重 + 只检测新代理）
    added = pool.add_proxies(fresh)
    if added == 0:
        logger.info("[代理刷新] 无新代理需要添加（全部已存在）")
        return

    # 合并到 DB 缓存（去重）
    try:
        cache_raw = fetch_val(
            "SELECT setting_value FROM public.global_settings WHERE setting_key = 'PROXY_VERIFIED_CACHE'",
            (),
        )
        existing = []
        if cache_raw:
            existing = json.loads(cache_raw).get("proxies", [])

        merged = list(dict.fromkeys(existing + pool.get_alive_proxies()))
        _persist_alive_proxies(merged)
        logger.info(f"[代理刷新] DB 缓存已更新: {len(existing)} → {len(merged)} 个代理")
    except Exception as e:
        logger.warning(f"[代理刷新] 更新 DB 缓存失败: {e}")


def _start_proxy_refresh_thread():
    """启动后台代理刷新线程（如果尚未启动）。"""
    global _proxy_refresh_thread
    if _proxy_refresh_thread is not None and _proxy_refresh_thread.is_alive():
        return
    _proxy_refresh_stop.clear()
    _proxy_refresh_thread = threading.Thread(target=_proxy_refresh_loop, daemon=True, name="proxy-refresh")
    _proxy_refresh_thread.start()
    logger.info("[代理刷新] 后台线程已启动")


def _stop_proxy_refresh_thread():
    """停止后台代理刷新线程。"""
    _proxy_refresh_stop.set()
    if _proxy_refresh_thread:
        _proxy_refresh_thread.join(timeout=5)


# ═══════════════════════════════════════════════════════════
# 采集进度状态（线程安全）
# ═══════════════════════════════════════════════════════════

_scrape_state: dict = {
    "active": False,
    "status": "idle",          # idle / running / done / stopped / error
    "categories": [],
    "category_index": 0,
    "category_total": 0,
    "current_category": "",
    "current_category_name": "",
    "current_page": 0,
    "total_albums": 0,
    "saved_albums": 0,
    "new_this_page": 0,
    "message": "",
    "log": [],
    "error": "",
    "started_at": "",
    "finished_at": "",
}
_scrape_lock = threading.Lock()
_scrape_stop_flag = threading.Event()


def get_scrape_status() -> dict:
    """获取当前采集进度（线程安全副本）。"""
    with _scrape_lock:
        return dict(_scrape_state)


def stop_scrape() -> bool:
    """请求停止正在运行的采集任务。"""
    with _scrape_lock:
        if not _scrape_state["active"]:
            return False
        _scrape_stop_flag.set()
        _scrape_state["message"] = "正在停止..."
        return True


def get_xm_cookie() -> str:
    """从全局设置读取喜马拉雅 Cookie。"""
    row = fetch_one("SELECT setting_value FROM public.global_settings WHERE setting_key = %s", ("XM_COOKIE",))
    return (row or {}).get("setting_value", "")


def _build_headers(cookie: str = "") -> dict:
    """构建请求头。"""
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _persist_alive_proxies(alive_list: list[str]):
    """把存活代理列表回写 DB 缓存。"""
    cache_json = json.dumps({"proxies": alive_list, "verified_at": datetime.now().isoformat()})
    execute(
        sql.SQL("UPDATE public.global_settings SET setting_value = %s WHERE setting_key = 'PROXY_VERIFIED_CACHE'"),
        (cache_json,),
    )


def _on_proxy_dead(proxy_url: str):
    """代理被踢出时的回调：从 DB 缓存中同步删除。"""
    try:
        cache_raw = fetch_val(
            "SELECT setting_value FROM public.global_settings WHERE setting_key = 'PROXY_VERIFIED_CACHE'",
            (),
        )
        if not cache_raw:
            return
        cache = json.loads(cache_raw)
        proxies = cache.get("proxies", [])
        if proxy_url in proxies:
            proxies.remove(proxy_url)
            _persist_alive_proxies(proxies)
            logger.info(f"已从缓存删除失效代理: {proxy_url} (缓存剩余 {len(proxies)} 个)")
    except Exception as e:
        logger.warning(f"从缓存删除失效代理失败: {e}")


def _init_proxy_pool():
    """从全局设置读取代理配置并初始化代理池。

    代理列表优先级：
    1. PROXY_LIST 手动填写 → 直接使用，不缓存
    2. PROXY_VERIFIED_CACHE 缓存 → 永久复用，直到代理失效才删除
    3. 缓存为空 → 从 PROXY_LIST_URL 自动发现 → 存入缓存

    代理池只初始化一次，后续调用直接复用。
    代理失效时（health_check 或运行时 mark_dead）实时从缓存删除。

    返回 (proxy_enabled, proxy_proxies_dict)。
    如果代理未启用或未配置，返回 (False, {})。
    """
    # 已初始化则直接复用
    existing_pool = get_pool()
    if existing_pool is not None:
        return True, get_proxy()

    rows = fetch_all(
        "SELECT setting_key, setting_value FROM public.global_settings "
        "WHERE setting_key IN ('PROXY_ENABLED', 'PROXY_LIST', 'PROXY_LIST_URL', "
        "'PROXY_VERIFY_COUNTRY', 'PROXY_MAX_TESTS', "
        "'PROXY_VERIFIED_CACHE', 'PROXY_TEST_URL', "
        "'PROXY_DEAD_RETRY_MINUTES', 'PROXY_TIMEOUT')"
    )
    settings_map = {row["setting_key"]: row["setting_value"] for row in (rows or [])}

    proxy_enabled = settings_map.get("PROXY_ENABLED", "false").lower() == "true"
    if not proxy_enabled:
        return False, {}

    test_url = settings_map.get("PROXY_TEST_URL", "https://www.ximalaya.com")
    dead_retry_minutes = int(settings_map.get("PROXY_DEAD_RETRY_MINUTES", "5"))
    timeout = int(settings_map.get("PROXY_TIMEOUT", "10"))

    proxy_list_raw = settings_map.get("PROXY_LIST", "")
    proxy_list = [line.strip() for line in proxy_list_raw.splitlines() if line.strip()]

    # 模式1: 手动列表有值 → 检测一次后用
    if proxy_list:
        init_pool(proxy_list=proxy_list, test_url=test_url,
                  dead_retry_minutes=dead_retry_minutes, timeout=timeout,
                  on_dead=_on_proxy_dead)
        pool = get_pool()
        if pool:
            stats = pool.health_check()
            logger.info(f"代理池健康检测(手动列表): {stats['alive']}/{stats['total']} 可用")
        return True, get_proxy()

    # 模式2/3: 手动列表为空 → 读缓存或自动发现
    cached_proxies = None
    from_url = False

    cache_raw = settings_map.get("PROXY_VERIFIED_CACHE", "")
    if cache_raw:
        try:
            cache = json.loads(cache_raw)
            if cache.get("proxies"):
                cached_proxies = cache["proxies"]
                logger.info(f"复用缓存代理: {len(cached_proxies)} 个 (永久缓存，失效即删)")
        except Exception as e:
            logger.warning(f"解析代理缓存失败: {e}")

    # 缓存为空 → 从 URL 自动发现
    if not cached_proxies:
        cached_proxies = _auto_discover_and_cache(settings_map, timeout)
        if not cached_proxies:
            return False, {}
        from_url = True

    init_pool(proxy_list=cached_proxies, test_url=test_url,
              dead_retry_minutes=dead_retry_minutes, timeout=timeout,
              on_dead=_on_proxy_dead)

    # 只有从 URL 获取时才做一次 health_check，缓存复用直接用
    if from_url:
        pool = get_pool()
        if pool:
            stats = pool.health_check()
            logger.info(f"代理池健康检测(从URL获取): {stats['alive']}/{stats['total']} 可用")

            alive = pool.get_alive_proxies()
            if stats["dead"] > 0 and alive:
                _persist_alive_proxies(alive)
                logger.info(f"已更新缓存: 踢除 {stats['dead']} 个失效代理, 保留 {len(alive)} 个")

            # 全部失效 → 清缓存重新发现
            if stats["alive"] == 0 and stats["total"] > 0:
                logger.warning("所有代理已失效，重新发现...")
                execute(
                    sql.SQL("UPDATE public.global_settings SET setting_value = '' WHERE setting_key = 'PROXY_VERIFIED_CACHE'"),
                    (),
                )
                new_proxies = _auto_discover_and_cache(settings_map, timeout)
                if new_proxies:
                    init_pool(proxy_list=new_proxies, test_url=test_url,
                              dead_retry_minutes=dead_retry_minutes, timeout=timeout,
                              on_dead=_on_proxy_dead)
                    pool = get_pool()
                    if pool:
                        stats = pool.health_check()
                        logger.info(f"重新发现后健康检测: {stats['alive']}/{stats['total']} 可用")
                        alive = pool.get_alive_proxies()
                        if alive:
                            _persist_alive_proxies(alive)
                else:
                    logger.warning("重新发现代理失败，使用直连")
                    return False, {}
    else:
        logger.info(f"缓存代理直接启用: {len(cached_proxies)} 个 (跳过健康检测)")

    # 启动后台代理刷新线程（定时自动发现新代理合并到池）
    _start_proxy_refresh_thread()

    return True, get_proxy()


def _auto_discover_and_cache(settings_map: dict, timeout: int) -> list[str]:
    """从 URL 自动发现中国代理，结果存入数据库缓存。"""
    list_url = settings_map.get("PROXY_LIST_URL", "")
    if not list_url:
        logger.warning("PROXY_LIST 为空且未配置 PROXY_LIST_URL")
        return []

    verify_country = settings_map.get("PROXY_VERIFY_COUNTRY", "中国")
    max_tests = int(settings_map.get("PROXY_MAX_TESTS", "100"))

    logger.info(f"从 URL 自动发现代理: {list_url}")
    proxies = auto_discover_proxies(
        list_url=list_url,
        verify_country=verify_country,
        max_tests=max_tests,
        timeout=min(timeout, 5),
    )
    if not proxies:
        logger.warning("自动发现未找到可用中国代理")
        return []

    # 存入缓存
    cache_json = json.dumps({"proxies": proxies, "verified_at": datetime.now().isoformat()})
    execute(
        sql.SQL("UPDATE public.global_settings SET setting_value = %s WHERE setting_key = 'PROXY_VERIFIED_CACHE'"),
        (cache_json,),
    )
    logger.info(f"已验证代理已缓存: {len(proxies)} 个 (永久保存，失效即删)")
    return proxies


# ═══════════════════════════════════════════════════════════
# 分类采集
# ═══════════════════════════════════════════════════════════

def _save_albums_to_db(albums: list[dict], category: str) -> int:
    """批量保存专辑到数据库，返回实际写入数量。"""
    batch_params = []
    for album in albums:
        book_id = f"xm_{album['albumId']}"
        book_data = {
            "albumId": album["albumId"],
            "albumCover": album.get("cover", ""),
            "albumCoverPath": album.get("albumCoverPath", ""),
            "intro": album.get("intro", ""),
            "albumPlayCount": album.get("albumPlayCount", 0),
            "isPaid": album.get("isPaid", False),
            "isFinished": album.get("isFinished", 0),
            "anchorId": album.get("anchorId", 0),
            "anchorName": album.get("anchorName", ""),
            "vipType": album.get("vipType", 0),
            "albumUrl": album.get("albumUrl", ""),
        }
        batch_params.append((
            book_id, album["albumTitle"], album.get("anchorName", ""),
            category, album.get("albumTrackCount", 0), Jsonb(book_data),
        ))

    if batch_params:
        execute_batch(
            sql.SQL("""
                INSERT INTO public.books (book_id, book_name, author, category, total_chapters, book_data, book_status, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', now())
                ON CONFLICT (book_id) DO UPDATE SET
                    book_name = EXCLUDED.book_name,
                    author = EXCLUDED.author,
                    category = EXCLUDED.category,
                    total_chapters = EXCLUDED.total_chapters,
                    book_data = EXCLUDED.book_data,
                    updated_at = now()
            """),
            batch_params,
        )

    return len(batch_params)


def start_scrape(categories: list[str], max_pages: int = 0, sort: str = "default",
                 free_only: bool = False, max_albums: int = 0) -> bool:
    """启动后台采集任务（非阻塞）。如果已有任务在运行则返回 False。"""
    with _scrape_lock:
        if _scrape_state["active"]:
            return False
        _scrape_stop_flag.clear()
        _scrape_state.update({
            "active": True,
            "status": "running",
            "categories": categories,
            "category_index": 0,
            "category_total": len(categories),
            "current_category": "",
            "current_category_name": "",
            "current_page": 0,
            "total_albums": 0,
            "saved_albums": 0,
            "new_this_page": 0,
            "message": "开始采集",
            "log": [],
            "error": "",
            "started_at": datetime.now().strftime("%H:%M:%S"),
            "finished_at": "",
        })

    thread = threading.Thread(
        target=_scrape_background,
        args=(categories, max_pages, sort, free_only, max_albums),
        daemon=True,
    )
    thread.start()
    return True


def _scrape_background(categories: list[str], max_pages: int, sort: str,
                       free_only: bool, max_albums: int):
    """后台采集线程函数。"""
    cookie = get_xm_cookie()
    headers = _build_headers(cookie)
    proxy_enabled, proxies = _init_proxy_pool()

    # 跨分类共享去重集合
    shared_seen_ids: set = set()

    def _log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with _scrape_lock:
            _scrape_state["log"].append(f"[{ts}] {msg}")
            _scrape_state["log"] = _scrape_state["log"][-100:]

    def _should_stop():
        return _scrape_stop_flag.is_set()

    try:
        for idx, cat in enumerate(categories):
            if _scrape_stop_flag.is_set():
                _log("用户手动停止")
                break

            # 分类之间间隔 3 秒
            if idx > 0:
                _log("分类切换间隔 3 秒...")
                time.sleep(3)
                if _scrape_stop_flag.is_set():
                    break

            cat_info = CATEGORIES.get(cat)
            cat_name = cat_info[1] if isinstance(cat_info, tuple) else (cat_info or cat)
            with _scrape_lock:
                _scrape_state["current_category"] = cat
                _scrape_state["current_category_name"] = cat_name
                _scrape_state["category_index"] = idx + 1
                _scrape_state["current_page"] = 0
                _scrape_state["new_this_page"] = 0
                _scrape_state["message"] = f"正在采集: {cat_name} ({idx + 1}/{len(categories)})"
            _log(f"开始采集分类: {cat_name}")

            # 创建采集任务记录
            task = execute_returning(
                sql.SQL("""
                    INSERT INTO public.xm_scrape_tasks (category, category_name, status, created_at)
                    VALUES (%s, %s, 'running', now())
                    RETURNING task_id
                """),
                (cat, cat_name),
            )
            task_id = task["task_id"] if task else 0

            cat_stats = {"total": 0, "saved": 0}

            def on_page_done(page, new_albums, total_so_far, sub_info=None,
                             _cat=cat, _cat_name=cat_name, _stats=cat_stats):
                saved = _save_albums_to_db(new_albums, _cat)
                _stats["total"] = total_so_far
                _stats["saved"] += saved
                label = f"{_cat_name}/{sub_info['name']}" if sub_info else _cat_name
                with _scrape_lock:
                    _scrape_state["current_page"] = page
                    _scrape_state["total_albums"] += len(new_albums)
                    _scrape_state["saved_albums"] += saved
                    _scrape_state["new_this_page"] = len(new_albums)
                    _scrape_state["log"].append(
                        f"[{datetime.now().strftime('%H:%M:%S')}] {label} 第{page}页: "
                        f"+{len(new_albums)} 新专辑 (入库{saved}), 累计 {total_so_far}"
                    )
                    _scrape_state["log"] = _scrape_state["log"][-100:]

            albums = _scrape_category(
                cat, max_pages=max_pages, sort=sort,
                free_only=free_only, max_albums=max_albums,
                headers=headers, proxies=proxies or None,
                on_page_done=on_page_done,
                should_stop=_should_stop,
                shared_seen_ids=shared_seen_ids,
            )

            # 更新任务记录
            execute(
                sql.SQL("UPDATE public.xm_scrape_tasks SET status = %s, total_albums = %s, "
                        "processed_albums = %s, finished_at = now() WHERE task_id = %s"),
                ("cancelled" if _scrape_stop_flag.is_set() else "done",
                 cat_stats["total"], cat_stats["saved"], task_id),
            )
            _log(f"分类 {cat_name} 完成: {cat_stats['total']} 个专辑, 入库 {cat_stats['saved']}")

        with _scrape_lock:
            _scrape_state["status"] = "stopped" if _scrape_stop_flag.is_set() else "done"
            _scrape_state["active"] = False
            _scrape_state["message"] = "已停止" if _scrape_stop_flag.is_set() else "采集完成"
            _scrape_state["finished_at"] = datetime.now().strftime("%H:%M:%S")

    except Exception as e:
        logger.error(f"采集异常: {e}", exc_info=True)
        with _scrape_lock:
            _scrape_state["status"] = "error"
            _scrape_state["active"] = False
            _scrape_state["error"] = str(e)
            _scrape_state["message"] = f"采集失败: {e}"
            _scrape_state["finished_at"] = datetime.now().strftime("%H:%M:%S")
            _scrape_state["log"].append(
                f"[{datetime.now().strftime('%H:%M:%S')}] 错误: {e}"
            )


def import_album_by_url(url: str) -> dict:
    """通过喜马拉雅专辑 URL 导入专辑：获取专辑信息入库 + 获取章节列表。

    支持的 URL 格式:
      https://www.ximalaya.com/album/77789950
      https://www.ximalaya.com/album/77789950/
      纯数字 albumId: 77789950

    返回:
        {"ok": True, "book_id": ..., "album_title": ..., "total_tracks": ..., "saved_tracks": ...}
    """
    import re

    # 从 URL 提取 albumId
    match = re.search(r'album/(\d+)', url)
    if match:
        album_id = match.group(1)
    elif url.strip().isdigit():
        album_id = url.strip()
    else:
        return {"ok": False, "error": f"无法从 URL 解析专辑 ID: {url}"}

    book_id = f"xm_{album_id}"

    cookie = get_xm_cookie()
    headers = _build_headers(cookie)
    _, proxies = _init_proxy_pool()

    # 获取专辑信息
    album_info = _get_album_info(album_id, headers, proxies=proxies or None)
    if not album_info:
        return {"ok": False, "error": f"无法获取专辑信息 (albumId={album_id})"}

    album_title = album_info.get("albumTitle", f"xm_{album_id}")
    total_chapters = int(album_info.get("totalCount", 0))

    # 入库 books 表（不存在则插入，存在则更新名称）
    book_data = {
        "albumId": album_id,
        "albumUrl": f"https://www.ximalaya.com/album/{album_id}",
    }
    execute(
        sql.SQL("""
            INSERT INTO public.books (book_id, book_name, category, total_chapters, book_data, book_status, updated_at)
            VALUES (%s, %s, '', %s, %s, 'pending', now())
            ON CONFLICT (book_id) DO UPDATE SET
                book_name = EXCLUDED.book_name,
                total_chapters = EXCLUDED.total_chapters,
                updated_at = now()
        """),
        (book_id, album_title, total_chapters, Jsonb(book_data)),
    )

    # 获取章节列表并入库
    result = scrape_album_tracks(book_id, proxies=proxies or None)
    return result


def scrape_album_tracks(book_id: str, proxies: dict | None = None) -> dict:
    """获取专辑的所有章节列表并保存到数据库。

    book_id 格式: xm_{albumId}
    proxies: 可选，外部传入的代理字典。如为 None 则从代理池获取。
    """
    # 从 book_id 提取 album_id
    if not book_id.startswith("xm_"):
        return {"ok": False, "error": "book_id 格式错误，应为 xm_{albumId}"}
    album_id = book_id[3:]

    cookie = get_xm_cookie()
    headers = _build_headers(cookie)

    # 代理：外部传入优先，否则初始化代理池获取
    if proxies is None:
        _, proxies = _init_proxy_pool()

    # 获取章节列表
    tracks, album_title = _get_all_tracks(album_id, headers, proxies=proxies or None)

    if not tracks:
        return {"ok": False, "error": "未获取到章节列表"}

    # 批量写入 audiobook_chapters
    batch_params = []
    for track in tracks:
        chapter_id = str(track["trackId"])
        audio_url = f"https://www.ximalaya.com/sound/{track['trackId']}"
        batch_params.append((
            book_id, chapter_id, album_title, track["title"],
            audio_url, track.get("orderNo", 0), track.get("duration", 0),
        ))

    if batch_params:
        execute_batch(
            sql.SQL("""
                INSERT INTO public.audiobook_chapters
                    (book_id, chapter_id, book_name, chapter_name, audio_url,
                     upload_status, chapter_order, duration)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
                ON CONFLICT (book_id, chapter_id) DO UPDATE SET
                    chapter_name = EXCLUDED.chapter_name,
                    audio_url = EXCLUDED.audio_url,
                    chapter_order = EXCLUDED.chapter_order,
                    duration = EXCLUDED.duration
            """),
            batch_params,
        )

    saved_count = len(batch_params)

    # 更新 books 表的 total_chapters 和 book_data.chapters
    book_row = fetch_one("SELECT book_data FROM public.books WHERE book_id = %s", (book_id,))
    if book_row and book_row.get("book_data"):
        book_data = book_row["book_data"]
        if isinstance(book_data, str):
            book_data = json.loads(book_data)
        book_data["chapters"] = [
            {
                "chapterId": str(t["trackId"]),
                "chapterName": t["title"],
                "mp3Url": f"https://www.ximalaya.com/sound/{t['trackId']}",
                "orderNo": t.get("orderNo", 0),
                "duration": t.get("duration", 0),
            }
            for t in tracks
        ]
        execute(
            sql.SQL("UPDATE public.books SET total_chapters = %s, book_data = %s, updated_at = now() WHERE book_id = %s"),
            (len(tracks), Jsonb(book_data), book_id),
        )
    else:
        execute(
            sql.SQL("UPDATE public.books SET total_chapters = %s, updated_at = now() WHERE book_id = %s"),
            (len(tracks), book_id),
        )

    return {
        "ok": True,
        "book_id": book_id,
        "album_title": album_title,
        "total_tracks": len(tracks),
        "saved_tracks": saved_count,
    }


# ═══════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════

def get_albums(page: int = 1, page_size: int = 20, category: str = "",
               search: str = "", status: str = "") -> dict:
    """分页查询专辑列表。"""
    conditions = []
    params: list = []

    if category:
        conditions.append("category = %s")
        params.append(category)
    if search:
        conditions.append("(book_name ILIKE %s OR author ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    if status:
        conditions.append("book_status = %s")
        params.append(status)

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # 总数
    total = fetch_val(f"SELECT COUNT(*) FROM public.books{where_clause}", tuple(params) if params else None)
    total = int(total or 0)

    # 分页
    offset = (page - 1) * page_size
    rows = fetch_all(
        sql.SQL("SELECT book_id, book_name, author, category, total_chapters, book_data, book_status, updated_at "
                "FROM public.books{} ORDER BY updated_at DESC LIMIT %s OFFSET %s").format(
            sql.SQL(where_clause)
        ),
        tuple(params + [page_size, offset]),
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "albums": rows or [],
    }


def get_album_detail(book_id: str) -> dict | None:
    """获取专辑详情。"""
    return fetch_one(
        "SELECT book_id, book_name, author, category, total_chapters, book_data, book_status, status, tags, note, created_at, updated_at "
        "FROM public.books WHERE book_id = %s",
        (book_id,),
    )


def get_album_chapters(book_id: str, page: int = 1, page_size: int = 50,
                       status: str = "") -> dict:
    """分页查询专辑章节列表。"""
    conditions = ["book_id = %s"]
    params: list = [book_id]

    if status:
        conditions.append("upload_status = %s")
        params.append(status)

    where_clause = " AND ".join(conditions)

    total = fetch_val(
        f"SELECT COUNT(*) FROM public.audiobook_chapters WHERE {where_clause}",
        tuple(params),
    )
    total = int(total or 0)

    offset = (page - 1) * page_size
    rows = fetch_all(
        sql.SQL("SELECT book_id, chapter_id, book_name, chapter_name, audio_url, "
                "telegram_file_id, telegram_message_id, telegram_bot_id, telegram_bot_user_id, "
                "upload_status, uploaded_at, worker_id, claimed_at, error_message, chapter_order, duration, "
                "original_telegram_file_id, original_telegram_message_id, "
                "original_telegram_bot_id, original_telegram_bot_user_id "
                "FROM public.audiobook_chapters WHERE {} ORDER BY chapter_order LIMIT %s OFFSET %s").format(
            sql.SQL(where_clause)
        ),
        tuple(params + [page_size, offset]),
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "chapters": rows or [],
    }


def delete_album(book_id: str) -> int:
    """删除专辑及其章节。"""
    execute(sql.SQL("DELETE FROM public.audiobook_chapters WHERE book_id = %s"), (book_id,))
    return execute(sql.SQL("DELETE FROM public.books WHERE book_id = %s"), (book_id,))


def delete_all_albums() -> int:
    """删除所有专辑及其章节。"""
    execute(sql.SQL("DELETE FROM public.audiobook_chapters"))
    return execute(sql.SQL("DELETE FROM public.books"))


# ═══════════════════════════════════════════════════════════
# 批量章节采集（后台线程）
# ═══════════════════════════════════════════════════════════

_tracks_state: dict = {
    "active": False,
    "status": "idle",
    "total": 0,
    "done": 0,
    "failed": 0,
    "workers": 1,
    "current_book": "",
    "message": "",
    "log": [],
    "started_at": "",
    "finished_at": "",
}
_tracks_lock = threading.Lock()
_tracks_stop_flag = threading.Event()


def get_tracks_status() -> dict:
    with _tracks_lock:
        return dict(_tracks_state)


def stop_tracks_scrape() -> bool:
    with _tracks_lock:
        if not _tracks_state["active"]:
            return False
        _tracks_stop_flag.set()
        _tracks_state["message"] = "正在停止..."
        return True


def start_scrape_all_tracks(max_workers: int = 5) -> bool:
    with _tracks_lock:
        if _tracks_state["active"]:
            return False
        _tracks_stop_flag.clear()
        _tracks_state.update({
            "active": True,
            "status": "running",
            "total": 0,
            "done": 0,
            "failed": 0,
            "workers": max_workers,
            "current_book": "",
            "message": "开始获取章节",
            "log": [],
            "started_at": datetime.now().strftime("%H:%M:%S"),
            "finished_at": "",
        })

    thread = threading.Thread(target=_scrape_all_tracks_background, args=(max_workers,), daemon=True)
    thread.start()
    return True


def _scrape_all_tracks_background(max_workers: int = 5):
    cookie = get_xm_cookie()
    headers = _build_headers(cookie)
    proxy_enabled, _ = _init_proxy_pool()

    def _log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with _tracks_lock:
            _tracks_state["log"].append(f"[{ts}] {msg}")
            _tracks_state["log"] = _tracks_state["log"][-100:]

    def _scrape_one_book(idx: int, total: int, book_id: str, book_name: str):
        """单个专辑章节采集任务（在线程池中执行）。"""
        if _tracks_stop_flag.is_set():
            return

        max_retries = 3
        for attempt in range(max_retries):
            if _tracks_stop_flag.is_set():
                return

            # 每次尝试随机选不同代理
            proxies = get_random_proxy() if proxy_enabled else None
            proxy_tag = ""
            if proxies and proxies.get("http"):
                proxy_tag = f" [{proxies['http'].split('//')[-1]}]"
            else:
                proxy_tag = " [直连]"

            if attempt == 0:
                with _tracks_lock:
                    _tracks_state["current_book"] = f"{book_name} ({idx + 1}/{total})"
                    _tracks_state["message"] = f"正在获取: {book_name} ({idx + 1}/{total})"
                _log(f"[{idx+1}/{total}] {book_name}{proxy_tag}")
            else:
                _log(f"  重试 {book_name} (第{attempt+1}次){proxy_tag}")

            try:
                result = scrape_album_tracks(book_id, proxies=proxies or None)
                if result.get("ok"):
                    with _tracks_lock:
                        _tracks_state["done"] += 1
                    _log(f"  成功 {book_name}: {result.get('total_tracks', 0)} 集")
                    return
                else:
                    err = result.get("error", "未知错误")
                    if attempt < max_retries - 1:
                        _log(f"  失败 {book_name}: {err}, 换代理重试...")
                        continue
                    with _tracks_lock:
                        _tracks_state["failed"] += 1
                    _log(f"  失败 {book_name}: {err}")
                    return
            except Exception as e:
                if attempt < max_retries - 1:
                    _log(f"  异常 {book_name}: {e}, 换代理重试...")
                    continue
                with _tracks_lock:
                    _tracks_state["failed"] += 1
                _log(f"  异常 {book_name}: {e}")
                return

    try:
        rows = fetch_all(
            sql.SQL("""
                SELECT b.book_id, b.book_name,
                       COALESCE(b.total_chapters, 0) AS total_chapters,
                       COALESCE(c.chapter_count, 0) AS chapter_count
                FROM public.books b
                LEFT JOIN (
                    SELECT book_id, COUNT(*) AS chapter_count
                    FROM public.audiobook_chapters
                    GROUP BY book_id
                ) c ON c.book_id = b.book_id
                WHERE COALESCE(c.chapter_count, 0) = 0
                   OR COALESCE(c.chapter_count, 0) < COALESCE(b.total_chapters, 0)
                ORDER BY b.book_id
            """),
        )
        if not rows:
            _log("没有需要获取章节的专辑（所有专辑章节已完整）")
            with _tracks_lock:
                _tracks_state["status"] = "done"
                _tracks_state["active"] = False
                _tracks_state["message"] = "没有需要获取章节的专辑"
                _tracks_state["finished_at"] = datetime.now().strftime("%H:%M:%S")
            return

        # 统计跳过的数量
        all_count = fetch_val("SELECT COUNT(*) FROM public.books")
        all_count = int(all_count or 0)
        skipped = all_count - len(rows)

        total = len(rows)
        with _tracks_lock:
            _tracks_state["total"] = total
        _log(f"共 {all_count} 个专辑, 跳过 {skipped} 个已完整, 待处理 {total} 个, {max_workers} 线程并发, 代理{'已启用' if proxy_enabled else '未启用(直连)'}")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for idx, row in enumerate(rows):
                if _tracks_stop_flag.is_set():
                    break
                book_id = row["book_id"]
                book_name = row.get("book_name", book_id)
                future = executor.submit(_scrape_one_book, idx, total, book_id, book_name)
                futures[future] = book_name

            for future in as_completed(futures):
                if _tracks_stop_flag.is_set():
                    break
                try:
                    future.result()
                except Exception as e:
                    book_name = futures[future]
                    with _tracks_lock:
                        _tracks_state["failed"] += 1
                    _log(f"  未捕获异常 {book_name}: {e}")

        with _tracks_lock:
            _tracks_state["status"] = "stopped" if _tracks_stop_flag.is_set() else "done"
            _tracks_state["active"] = False
            _tracks_state["message"] = "已停止" if _tracks_stop_flag.is_set() else "全部完成"
            _tracks_state["finished_at"] = datetime.now().strftime("%H:%M:%S")

    except Exception as e:
        logger.error(f"批量章节采集异常: {e}", exc_info=True)
        with _tracks_lock:
            _tracks_state["status"] = "error"
            _tracks_state["active"] = False
            _tracks_state["message"] = f"错误: {e}"
            _tracks_state["finished_at"] = datetime.now().strftime("%H:%M:%S")


def get_categories() -> dict:
    """返回可用分类列表 {pinyin: name} 供前端使用。"""
    cats = _get_api_categories()
    return {k: v[1] if isinstance(v, tuple) else v for k, v in cats.items()}
