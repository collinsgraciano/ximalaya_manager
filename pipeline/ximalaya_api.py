"""喜马拉雅 API 封装 — 采集 + 下载 + AES 解密。

移植自 scrape_ximalaya_category.py 和 download_ximalaya.py，
重构为纯函数模块，VPS 和 Colab 端均可复用。
"""

from __future__ import annotations

import requests
import json
import time
import re
import base64
import binascii
import logging
from typing import Any

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════

BASE_URL = "https://www.ximalaya.com"

CATEGORIES = {
    "youshengshu": "有声书",
    "ertong": "儿童",
    "xiangsheng": "相声评书",
    "yinyue": "音乐",
    "lishi": "历史",
    "renwen": "人文",
    "shangye": "商业财经",
    "waiguo": "外语",
    "keji": "科技",
    "jiankang": "健康",
    "qinggan": "情感生活",
    "tiyu": "体育",
    "youxi": "游戏",
    "xiuxian": "休闲",
    "sanzijing": "广播剧",
    "guoxue": "国学",
    "jingji": "经济",
    "sheying": "摄影",
    "meishi": "美食",
    "lvyou": "旅游",
    "qiche": "汽车",
    "yingyu": "英语",
    "riyu": "日语",
    "hanyu": "韩语",
    "fayu": "法语",
    "deyu": "德语",
    "jiaoyu": "教育考试",
    "kexue": "科学",
    "xiaoshuo": "小说",
    "duanju": "短剧",
    "xiju": "戏剧",
    "pingshu": "评书",
}

SORT_MAP = {
    "default": "",
    "mostplays": "mostplays",
    "updates": "updates",
}

ALBUMS_PER_PAGE = 30

QUALITY_PRIORITY = ["M4A_128", "M4A_64", "MP3_64", "MP3_32", "M4A_24"]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "Referer": BASE_URL + "/",
}

# AES 解密密钥
_AES_KEY = binascii.unhexlify("aaad3e4fd540b0f79dca95606e72bf93")


# ═══════════════════════════════════════════════════════════
# AES 解密
# ═══════════════════════════════════════════════════════════

def crack_playurl(ciphertext: str) -> str:
    """AES-ECB 解密音频 URL。"""
    if not ciphertext or not isinstance(ciphertext, str):
        return ciphertext
    if AES is None:
        raise RuntimeError("pycryptodome 未安装，无法解密音频 URL")
    cipher = AES.new(_AES_KEY, AES.MODE_ECB)
    padded = ciphertext + "=" * (4 - len(ciphertext) % 4)
    plaintext = cipher.decrypt(base64.urlsafe_b64decode(padded))
    return re.sub(r"[^\x20-\x7E]", "", plaintext.decode("utf-8"))


# ═══════════════════════════════════════════════════════════
# SSR 页面解析 — 分类采集
# ═══════════════════════════════════════════════════════════

def extract_albums_from_html(text: str) -> tuple[list[dict], dict]:
    """从 SSR 页面 HTML 中提取 __INITIAL_STATE__ 里的专辑列表和分页信息。

    返回 (albums, page_info)，page_info 可能包含 maxPageId, totalCount。
    """
    start = text.find("__INITIAL_STATE__")
    if start < 0:
        return [], {}

    eq = text.find("=", start)
    if eq < 0:
        return [], {}

    json_start = eq + 1
    while json_start < len(text) and text[json_start] in " \t":
        json_start += 1
    if json_start >= len(text) or text[json_start] != "{":
        return [], {}

    # 匹配闭合大括号
    depth = 0
    i = json_start
    in_str = False
    esc = False
    while i < len(text):
        c = text[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1

    raw = text[json_start:i]
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return [], {}

    # 递归查找 albums 列表
    def find_albums(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if (k == "albums" and isinstance(v, list) and v
                        and isinstance(v[0], dict) and "albumId" in v[0]):
                    return v
                if isinstance(v, (dict, list)):
                    result = find_albums(v)
                    if result:
                        return result
        elif isinstance(obj, list):
            for item in obj:
                result = find_albums(item)
                if result:
                    return result
        return None

    albums = find_albums(state) or []

    # 从 state 中提取分页信息
    page_info = _extract_page_info(state)

    return albums, page_info


def _extract_page_info(state: dict) -> dict:
    """从 __INITIAL_STATE__ 中提取分类页的分页信息。"""
    info = {}

    def _find_keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "maxPageId" and isinstance(v, int):
                    info["maxPageId"] = v
                elif k == "totalCount" and isinstance(v, int):
                    info["totalCount"] = v
                elif k == "pageCount" and isinstance(v, int):
                    info["pageCount"] = v
                if isinstance(v, (dict, list)):
                    _find_keys(v)
        elif isinstance(obj, list):
            for item in obj:
                _find_keys(item)

    _find_keys(state)
    return info


def fetch_category_page(category: str, page: int, sort: str = "",
                        headers: dict | None = None,
                        proxies: dict | None = None) -> tuple[list[dict], dict]:
    """抓取分类页面的专辑列表。

    返回 (albums, page_info)，page_info 可能包含 maxPageId, totalCount。
    """
    parts = [BASE_URL, "category", category, "reci1"]
    if sort:
        parts.append(sort)
    parts.append(f"p{page}")
    url = "/".join(parts)

    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        resp = requests.get(url, headers=hdrs, timeout=20, proxies=proxies or None)
        albums, page_info = extract_albums_from_html(resp.text)
        return albums, page_info
    except Exception as e:
        logger.warning(f"抓取分类页面失败 (page={page}): {e}")
        return [], {}


def normalize_album_record(album: dict) -> dict:
    """提取并标准化专辑字段。"""
    aid = album.get("albumId")
    return {
        "albumId": aid,
        "albumTitle": album.get("albumTitle", ""),
        "albumTrackCount": album.get("albumTrackCount", 0),
        "albumPlayCount": album.get("albumPlayCount", 0),
        "isPaid": album.get("isPaid", False),
        "isFinished": album.get("isFinished", 0),
        "anchorId": album.get("anchorId", 0),
        "anchorName": album.get("albumUserNickName", ""),
        "intro": album.get("intro", ""),
        "vipType": album.get("vipType", 0),
        "albumUrl": album.get("albumUrl", f"/album/{aid}"),
        # 封面 URL：喜马拉雅 CDN 格式
        "albumCoverPath": album.get("albumCoverPath", ""),
        "cover": _build_cover_url(aid, album.get("albumCoverPath", "")),
    }


def _build_cover_url(album_id: Any, cover_path: str) -> str:
    """构建封面图 URL。"""
    if cover_path:
        if cover_path.startswith("http"):
            return cover_path
        return f"https://imagev2.ximalaya.com/{cover_path}"
    if album_id:
        return f"https://imagev2.ximalaya.com/100/{album_id}.jpg"
    return ""


def scrape_category(category: str, max_pages: int = 0, sort: str = "default",
                    free_only: bool = False, max_albums: int = 0,
                    headers: dict | None = None,
                    proxies: dict | None = None,
                    on_page_done=None,
                    should_stop=None,
                    shared_seen_ids: set | None = None) -> list[dict]:
    """采集分类下的所有专辑，返回标准化专辑列表。

    Args:
        on_page_done: 回调 fn(page, new_albums, total_so_far) — 每页采集完成后调用，
                      new_albums 是本页新增的专辑列表（已标准化），可用于增量入库。
        should_stop: 回调 fn() -> bool — 返回 True 时停止采集（手动停止）。
        shared_seen_ids: 跨分类共享的去重集合，传入后同一专辑不会在不同分类中重复采集。
    """
    sort_param = SORT_MAP.get(sort, "")
    all_albums: list[dict] = []
    seen_ids: set = shared_seen_ids if shared_seen_ids is not None else set()

    page = 1
    no_new_count = 0
    MAX_NO_NEW = 3
    api_max_pages = 0  # 从 API 获取的真实最大页数

    while True:
        # 优先使用用户设定的 max_pages，未设定时用 API 返回的最大页数
        effective_max = max_pages if max_pages > 0 else api_max_pages
        if effective_max > 0 and page > effective_max:
            break
        if max_albums > 0 and len(all_albums) >= max_albums:
            break
        if should_stop and should_stop():
            logger.info("  用户手动停止采集")
            break

        albums, page_info = fetch_category_page(category, page, sort_param, headers, proxies)

        # 第一页时获取 API 的最大页数
        if page == 1 and page_info:
            api_max_pages = page_info.get("maxPageId", 0)
            total_count = page_info.get("totalCount", 0)
            if api_max_pages or total_count:
                logger.info(f"  分类 {category}: API 返回 maxPageId={api_max_pages}, totalCount={total_count}")

        if not albums:
            no_new_count += 1
            if no_new_count >= MAX_NO_NEW:
                logger.info(f"  连续 {MAX_NO_NEW} 次空页面, 停止采集")
                break
            page += 1
            time.sleep(1)
            continue

        page_new_albums: list[dict] = []
        for album in albums:
            aid = album.get("albumId")
            if aid is None or aid in seen_ids:
                continue
            seen_ids.add(aid)

            if free_only and album.get("isPaid"):
                continue

            record = normalize_album_record(album)
            all_albums.append(record)
            page_new_albums.append(record)

            if max_albums > 0 and len(all_albums) >= max_albums:
                break

        logger.info(f"  page {page}: {len(albums)} 个专辑, 新增 {len(page_new_albums)}, 累计 {len(all_albums)}")

        if on_page_done:
            on_page_done(page, page_new_albums, len(all_albums))

        if page_new_albums:
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= MAX_NO_NEW:
                logger.info(f"  连续 {MAX_NO_NEW} 页无新专辑, 停止采集")
                break

        page += 1
        time.sleep(0.5)

    return all_albums


# ═══════════════════════════════════════════════════════════
# 章节列表获取 — 移动端 API
# ═══════════════════════════════════════════════════════════

def get_track_list(album_id: str, page: int = 1,
                   headers: dict | None = None,
                   proxies: dict | None = None) -> dict:
    """获取分页音频列表（移动端 API）。"""
    url = "http://mobwsa.ximalaya.com/mobile/playlist/album/page"
    params = {"albumId": album_id, "pageId": page}
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    resp = requests.get(url, params=params, headers=hdrs, timeout=15, proxies=proxies or None)
    resp.raise_for_status()
    return resp.json()


def get_all_tracks(album_id: str, headers: dict | None = None,
                   proxies: dict | None = None) -> tuple[list[dict], str]:
    """获取全部音频列表，返回 (tracks, album_title)。

    每个 track: {trackId, title, orderNo, duration}
    """
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    first_page = get_track_list(album_id, 1, hdrs, proxies)
    max_page = first_page.get("maxPageId", 1)
    total = first_page.get("totalCount", 0)
    album_title = ""
    if first_page.get("list"):
        album_title = first_page["list"][0].get("albumTitle", "")
    logger.info(f"专辑 {album_id}: {album_title}, 总集数 {total}, 总页数 {max_page}")

    tracks: list[dict] = []
    for page in range(1, max_page + 1):
        if page == 1:
            data = first_page
        else:
            data = get_track_list(album_id, page, hdrs, proxies)
            time.sleep(0.5)
        for item in data.get("list", []):
            tracks.append({
                "trackId": item["trackId"],
                "title": item["title"],
                "orderNo": item["orderNo"],
                "duration": item["duration"],
            })
        if page % 10 == 0 or page == max_page:
            logger.info(f"  已获取 {page}/{max_page} 页, 累计 {len(tracks)} 集")
    return tracks, album_title


def get_album_info(album_id: str, headers: dict | None = None,
                   proxies: dict | None = None) -> dict:
    """获取专辑详情（从 track API 第一页提取）。"""
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    first_page = get_track_list(album_id, 1, hdrs, proxies)
    if not first_page.get("list"):
        return {}
    first_item = first_page["list"][0]
    return {
        "albumTitle": first_item.get("albumTitle", ""),
        "albumId": album_id,
        "totalCount": first_page.get("totalCount", 0),
        "maxPageId": first_page.get("maxPageId", 1),
    }


# ═══════════════════════════════════════════════════════════
# 音频下载 URL 获取
# ═══════════════════════════════════════════════════════════

def get_download_url(track_id: str, headers: dict | None = None,
                     max_retries: int = 3,
                     proxies: dict | None = None) -> tuple[str | None, int]:
    """通过 mobile-playpage API 获取音频下载 URL。

    返回 (url, file_size) 或 (None, 0)
    """
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    if "Referer" not in hdrs:
        hdrs["Referer"] = f"{BASE_URL}/album"

    for attempt in range(max_retries):
        try:
            ts = int(time.time() * 1000)
            resp = requests.get(
                f"{BASE_URL}/mobile-playpage/track/v3/baseInfo/{ts}",
                params={"device": "web", "trackId": str(track_id), "trackQualityLevel": "3"},
                headers=hdrs, timeout=15, proxies=proxies or None)
            data = resp.json()

            if data.get("ret") != 0:
                return None, 0

            track_info = data.get("trackInfo", {})
            play_urls = track_info.get("playUrlList", [])

            # 按质量优先级选择
            play_url_map = {pu.get("type"): pu for pu in play_urls if isinstance(pu, dict)}
            for quality in QUALITY_PRIORITY:
                pu = play_url_map.get(quality)
                if pu and pu.get("url"):
                    url = crack_playurl(pu["url"])
                    if url.startswith("http"):
                        return url, int(pu.get("fileSize", 0))

            # 取第一个可用的
            for pu in play_urls:
                if isinstance(pu, dict) and pu.get("url"):
                    url = crack_playurl(pu["url"])
                    if url.startswith("http"):
                        return url, int(pu.get("fileSize", 0))

            return None, 0

        except Exception:
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
            else:
                return None, 0

    return None, 0


def download_track(track_id: str, save_path: str, headers: dict | None = None,
                   max_retries: int = 3,
                   proxies: dict | None = None) -> tuple[str, int]:
    """下载单集音频到指定路径，返回 (status, file_size)。

    status: "downloaded" / "skipped" / "no_url" / "download_failed"
    """
    import os

    # 文件已存在则跳过
    if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
        return "skipped", os.path.getsize(save_path)

    download_url, file_size = get_download_url(track_id, headers, max_retries=max_retries, proxies=proxies)
    if not download_url:
        return "no_url", 0

    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    tmp_path = save_path + ".tmp"

    for attempt in range(max_retries):
        try:
            resp = requests.get(download_url, headers=hdrs, stream=True, timeout=120, proxies=proxies or None)
            resp.raise_for_status()

            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

            actual_size = os.path.getsize(tmp_path)
            if actual_size == 0:
                raise Exception("下载文件为空")

            import shutil
            shutil.move(tmp_path, save_path)
            return "downloaded", actual_size

        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                return f"download_failed: {e}", 0

    return "unknown_error", 0
