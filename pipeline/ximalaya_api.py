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
from html import unescape
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

ALBUMS_API = BASE_URL + "/revision/category/v2/albums"
ALBUM_INFO_API = BASE_URL + "/revision/album/v1/simple"
ALL_CATEGORY_INFO_API = BASE_URL + "/revision/category/allCategoryInfo"
METADATA_INFO_API = BASE_URL + "/revision/category/v2/metadata/info"

# 默认分类列表 (拼音code: (categoryId, 中文名))
# 运行时可通过 get_categories() 从 API 动态更新
DEFAULT_CATEGORIES = {
    "yinyue": (2, "音乐"),
    "youshengshu": (3, "有声书"),
    "yule": (4, "娱乐"),
    "waiyu": (5, "外语"),
    "ertong": (6, "儿童"),
    "shangye": (8, "商业财经"),
    "lishi": (9, "历史"),
    "xiangsheng": (12, "相声评书"),
    "gerenchengzhang": (13, "个人成长"),
    "guangbojv": (15, "广播剧"),
    "youshengtushu": (1001, "有声图书"),
    "renwenguoxue": (1002, "人文国学"),
    "redian": (1005, "热点"),
    "shenghuo": (1006, "生活"),
    "xinhongse": (1054, "新红色频道"),
    "xuanyi": (1061, "悬疑"),
    "jiankang": (1062, "健康"),
    "qiche": (1065, "汽车"),
}

# 向后兼容别名
CATEGORIES = DEFAULT_CATEGORIES

# 排序方式: 0=综合, 1=最火, 2=最新
SORT_MAP = {
    "default": 0,
    "mostplays": 1,
    "updates": 2,
}

# 每页专辑数 (API 最大 50)
ALBUMS_PER_PAGE = 50

QUALITY_PRIORITY = ["M4A_24", "MP3_32", "MP3_64", "M4A_64", "M4A_128"]

# 所有可选音质
ALL_QUALITIES = ["M4A_24", "MP3_32", "MP3_64", "M4A_64", "M4A_128"]

def parse_quality_priority(quality_str: str | None) -> list[str]:
    """将逗号分隔的音质字符串解析为优先级列表。

    无效值会被过滤，未列出的音质追加到末尾作为兜底。
    """
    if not quality_str or not quality_str.strip():
        return list(QUALITY_PRIORITY)
    parts = [q.strip().upper() for q in quality_str.split(",") if q.strip()]
    # 过滤无效值
    valid = [q for q in parts if q in ALL_QUALITIES]
    # 追加未列出的有效音质作为兜底
    for q in ALL_QUALITIES:
        if q not in valid:
            valid.append(q)
    return valid

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
# 工具函数
# ═══════════════════════════════════════════════════════════

def strip_html(html_str: str) -> str:
    """去除 HTML 标签，返回纯文本。"""
    if not html_str:
        return ""
    text = re.sub(r'<img[^>]*>', '', html_str)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def fix_cover_url(cover: str) -> str:
    """修复封面 URL（补全 https: 前缀）。"""
    if not cover:
        return ""
    if cover.startswith("//"):
        return "https:" + cover
    if cover.startswith("http://"):
        return "https:" + cover[5:]
    if not cover.startswith("http"):
        return "https://" + cover
    return cover


# ═══════════════════════════════════════════════════════════
# 专辑详情 API (/revision/album/v1/simple)
# ═══════════════════════════════════════════════════════════

def fetch_album_detail(album_id: str, headers: dict | None = None,
                       proxies: dict | None = None) -> dict:
    """获取专辑详情（封面、分类、简介、主播等）。

    API: /revision/album/v1/simple?albumId={id}
    """
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        resp = requests.get(ALBUM_INFO_API, params={"albumId": album_id},
                            headers=hdrs, timeout=15, proxies=proxies or None)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != 200:
            logger.warning(f"专辑详情 API 返回 ret={data.get('ret')} msg={data.get('msg', '')}")
            return {}

        main_info = data.get("data", {}).get("albumPageMainInfo", {})
        if not main_info:
            return {}

        return {
            "albumId": album_id,
            "albumTitle": main_info.get("albumTitle", ""),
            "albumUrl": f"https://www.ximalaya.com/album/{album_id}",
            "cover": fix_cover_url(main_info.get("cover", "")),
            "categoryId": main_info.get("categoryId", 0),
            "categoryTitle": main_info.get("categoryTitle", ""),
            "anchorUid": main_info.get("anchorUid", 0),
            "anchorName": main_info.get("anchorName", ""),
            "intro": strip_html(main_info.get("detailRichIntro", "")),
            "shortIntro": main_info.get("shortIntro", ""),
            "recommendReason": main_info.get("recommendReason", ""),
            "isPaid": main_info.get("isPaid", False),
            "isFinished": main_info.get("isFinished", 0),
            "playCount": main_info.get("playCount", 0),
            "subscribeCount": main_info.get("subscribeCount", 0),
            "vipType": main_info.get("vipType", 0),
            "createDate": main_info.get("createDate", ""),
            "updateDate": main_info.get("updateDate", ""),
        }
    except Exception as e:
        logger.warning(f"获取专辑详情失败 (albumId={album_id}): {e}")
        return {}


# ═══════════════════════════════════════════════════════════
# 分类列表动态获取
# ═══════════════════════════════════════════════════════════

_cached_categories: dict | None = None
_cache_time: float = 0
_CACHE_TTL = 3600  # 1 小时


def fetch_category_info(headers: dict | None = None,
                         proxies: dict | None = None) -> dict:
    """从 allCategoryInfo API 获取完整分类信息 (含拼音和 categoryId)。

    返回 dict: {pinyin: (categoryId, title)}
    """
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        resp = requests.get(ALL_CATEGORY_INFO_API, headers=hdrs,
                            timeout=15, proxies=proxies or None)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        cats = {}
        for group in data:
            for cat in group.get("categories", []):
                cid = cat.get("categoryId")
                pinyin = cat.get("pinyin", "")
                title = cat.get("displayName", "")
                if pinyin and cid:
                    cats[pinyin] = (cid, title)
        return cats
    except Exception as e:
        logger.warning(f"获取分类信息失败: {e}")
        return {}


def get_categories(headers: dict | None = None,
                   proxies: dict | None = None) -> dict:
    """获取分类列表, 优先从 API 获取, 失败则用默认列表。

    返回 dict: {pinyin: (categoryId, title)}
    结果缓存 1 小时。
    """
    global _cached_categories, _cache_time
    import time as _time
    now = _time.time()
    if _cached_categories and (now - _cache_time) < _CACHE_TTL:
        return _cached_categories
    cats = fetch_category_info(headers, proxies)
    if cats:
        _cached_categories = cats
        _cache_time = now
        return cats
    return DEFAULT_CATEGORIES


# ═══════════════════════════════════════════════════════════
# 子分类树获取
# ═══════════════════════════════════════════════════════════

def fetch_subcategories(category_id: int, headers: dict | None = None,
                        proxies: dict | None = None) -> dict:
    """从 metadata/info API 获取分类的子分类树。

    网站URL格式: /category/a{catId}_b{bId}_c{cId}/
    树结构: 根(分类) > b层(组) > c层(叶子)

    Returns:
        {b_id: {"name": str, "subs": [{"id": c_id, "name": str}, ...]}}
    """
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        resp = requests.get(METADATA_INFO_API,
                            params={"categoryId": category_id},
                            headers=hdrs, timeout=15, proxies=proxies or None)
        resp.raise_for_status()
        metadata = resp.json().get("data", {}).get("metadata", [])

        groups: dict = {}  # b_id -> {name, subs: {c_id: name}}

        def walk(node, b_id=None, b_name=None):
            """递归遍历元数据树。

            - b_id=None: 根层, 子节点是 b 组
            - b_id=值: b 组内, 递归到叶子节点收集 c_id
            """
            if not isinstance(node, dict):
                return
            vid = node.get("id")
            vname = node.get("displayName", node.get("name", ""))
            children = node.get("metadataValues") or []

            if b_id is None:
                for child in children:
                    if isinstance(child, dict):
                        cid = child.get("id")
                        cname = child.get("displayName", child.get("name", ""))
                        groups[cid] = {"name": cname, "subs": {}}
                        walk(child, b_id=cid, b_name=cname)
            else:
                if children:
                    for child in children:
                        walk(child, b_id=b_id, b_name=b_name)
                else:
                    if vid not in groups[b_id]["subs"]:
                        groups[b_id]["subs"][vid] = vname

        for root in metadata:
            walk(root)

        result = {}
        for bid in sorted(groups.keys()):
            g = groups[bid]
            result[bid] = {
                "name": g["name"],
                "subs": [{"id": cid, "name": name}
                         for cid, name in sorted(g["subs"].items(), key=lambda x: x[1])],
            }
        return result
    except Exception as e:
        logger.warning(f"获取子分类失败 (category_id={category_id}): {e}")
        return {}


# ═══════════════════════════════════════════════════════════
# 分类采集 — JSON API
# ═══════════════════════════════════════════════════════════

def fetch_category_page(category_id: int, page: int, sort: int = 0,
                        headers: dict | None = None,
                        proxies: dict | None = None) -> tuple[list[dict], dict]:
    """通过 JSON API 获取一页专辑列表。

    Args:
        category_id: 分类数字 ID
        page: 页码 (从 1 开始)
        sort: 排序方式 (0=综合, 1=最火, 2=最新)

    Returns:
        (albums_list, page_info) — page_info 包含 total, max_page
    """
    params = {
        "categoryId": category_id,
        "pageNum": page,
        "pageSize": ALBUMS_PER_PAGE,
        "sort": sort,
    }
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        resp = requests.get(ALBUMS_API, params=params, headers=hdrs,
                            timeout=20, proxies=proxies or None)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != 200:
            logger.warning(f"API 返回 ret={data.get('ret')} msg={data.get('msg', '')}")
            return [], {}
        d = data.get("data", {})
        albums = d.get("albums", [])
        total = d.get("total", 0)
        max_page = (total + ALBUMS_PER_PAGE - 1) // ALBUMS_PER_PAGE if total > 0 else 0
        return albums, {"total": total, "max_page": max_page}
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
    """构建封面图 URL。fdfs.xmcdn.com 是正确的图片 CDN 域名。"""
    if cover_path:
        if cover_path.startswith("http"):
            return cover_path
        return f"https://fdfs.xmcdn.com/{cover_path}"
    if album_id:
        return f"https://fdfs.xmcdn.com/group10/{album_id}.jpg"
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
        category: 分类拼音名 (如 "lishi") 或数字 ID 字符串
        on_page_done: 回调 fn(page, new_albums, total_so_far, sub_info=None)
        should_stop: 回调 fn() -> bool — 返回 True 时停止采集（手动停止）。
        shared_seen_ids: 跨分类共享的去重集合。
    """
    sort_val = SORT_MAP.get(sort, 0)

    # 解析 categoryId
    cat_info = CATEGORIES.get(category)
    if isinstance(cat_info, tuple):
        category_id = cat_info[0]
    elif str(category).isdigit():
        category_id = int(category)
    else:
        dynamic_cats = get_categories(headers, proxies)
        dyn_info = dynamic_cats.get(category)
        if isinstance(dyn_info, tuple):
            category_id = dyn_info[0]
        else:
            logger.warning(f"未知分类: {category}")
            return []

    all_albums: list[dict] = []
    seen_ids: set = shared_seen_ids if shared_seen_ids is not None else set()

    # 逐个目标采集
    _scrape_target(
        category_id, sort_val, max_pages, max_albums, free_only,
        headers, proxies, all_albums, seen_ids,
        on_page_done, should_stop, None,
    )

    return all_albums


def _scrape_target(target_id: int, sort_val: int, max_pages: int,
                   max_albums: int, free_only: bool,
                   headers: dict | None, proxies: dict | None,
                   all_albums: list[dict], seen_ids: set,
                   on_page_done, should_stop, sub_info: dict | None = None):
    """采集单个目标 (顶级分类或叶子子分类) 的专辑，追加到 all_albums。"""
    label = sub_info["name"] if sub_info else str(target_id)
    page = 1
    no_new_count = 0
    MAX_NO_NEW = 3
    api_max_pages = 0

    while True:
        effective_max = max_pages if max_pages > 0 else api_max_pages
        if effective_max > 0 and page > effective_max:
            break
        if max_albums > 0 and len(all_albums) >= max_albums:
            break
        if should_stop and should_stop():
            break

        albums, page_info = fetch_category_page(target_id, page, sort_val, headers, proxies)

        if page == 1 and page_info:
            api_max_pages = page_info.get("max_page", 0)
            total_count = page_info.get("total", 0)
            if api_max_pages or total_count:
                logger.info(f"  [{label}] 总数 {total_count}, 最大页 {api_max_pages}")

        if not albums:
            no_new_count += 1
            if no_new_count >= MAX_NO_NEW:
                logger.info(f"  [{label}] 连续 {MAX_NO_NEW} 次空页面, 跳过")
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
            if sub_info:
                record["subcategory"] = sub_info["name"]
                record["subcategory_b_id"] = sub_info["b_id"]
                record["subcategory_c_id"] = sub_info["c_id"]
            all_albums.append(record)
            page_new_albums.append(record)

            if max_albums > 0 and len(all_albums) >= max_albums:
                break

        logger.info(f"  [{label}] page {page}: {len(albums)} 个专辑, 新增 {len(page_new_albums)}, 累计 {len(all_albums)}")

        if on_page_done:
            on_page_done(page, page_new_albums, len(all_albums), sub_info=sub_info)

        if page_new_albums:
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= MAX_NO_NEW:
                logger.info(f"  [{label}] 连续 {MAX_NO_NEW} 页无新专辑, 跳过")
                break

        page += 1
        time.sleep(0.5)


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
                     proxies: dict | None = None,
                     quality_priority: list[str] | None = None) -> tuple[str | None, int]:
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
                headers=hdrs, timeout=10, proxies=proxies or None)
            data = resp.json()

            if data.get("ret") != 0:
                return None, 0

            track_info = data.get("trackInfo", {})
            play_urls = track_info.get("playUrlList", [])

            # 按质量优先级选择
            qp = quality_priority or QUALITY_PRIORITY
            play_url_map = {pu.get("type"): pu for pu in play_urls if isinstance(pu, dict)}
            for quality in qp:
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
                   proxies: dict | None = None,
                   quality_priority: list[str] | None = None) -> tuple[str, int]:
    """下载单集音频到指定路径，返回 (status, file_size)。

    status: "downloaded" / "skipped" / "no_url" / "download_failed"
    """
    import os

    # 文件已存在则跳过
    if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
        return "skipped", os.path.getsize(save_path)

    download_url, file_size = get_download_url(track_id, headers, max_retries=max_retries,
                                                proxies=proxies, quality_priority=quality_priority)
    if not download_url:
        return "no_url", 0

    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    tmp_path = save_path + ".tmp"

    for attempt in range(max_retries):
        try:
            resp = requests.get(download_url, headers=hdrs, stream=True, timeout=30, proxies=proxies or None)
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
