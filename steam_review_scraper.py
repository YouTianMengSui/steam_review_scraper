#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam游戏评测爬虫脚本
功能：11种常用语言及“全部语言”评测爬取，支持数量限制
      评测类型筛选（全部/好评/差评）
      购买来源筛选（Steam商店/非Steam/全部）
      游戏时长实时筛选（边爬边筛）
      用户昵称获取（需Steam Web API Key）
      导出CSV+TXT报告，含测评超链接
      记录爬取起止时间，统计语言分布及好评率
      爬取前获取官方评测总数，完成后对比显示完整性
新增：按实际购买来源（Steam直购/非Steam直购）统计好评率（使用 steam_purchase 字段）
      并在TXT报告中显示每条评测的购买来源
"""

import argparse
import sys
import time
import random
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

# 第三方依赖检查
try:
    import requests
    import pandas as pd
    import urllib3
except ImportError as e:
    print(f"缺失依赖: {e}\n请安装: pip install requests pandas urllib3")
    input("按回车键退出...")
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== 常量配置 ==========
STEAM_STORE_API = "https://store.steampowered.com/api/appdetails"
STEAM_REVIEWS_API_TEMPLATE = "https://store.steampowered.com/appreviews/{}"
STEAM_USER_API = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"

REQUEST_TIMEOUT = 5
REQUEST_RETRIES = 2
RETRY_BACKOFF = 0.2
REVIEWS_PER_PAGE = 100
BASE_SLEEP = 0.2
RANDOM_JITTER = 0.05
GAME_NAME_TIMEOUT = 5
GAME_NAME_WORKER_TIMEOUT = 6

DEFAULT_LANGUAGE = "schinese"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 语言菜单映射
LANGUAGE_MENU_MAP = {
    "1": ("english", "英语"),
    "2": ("schinese", "简体中文"),
    "3": ("japanese", "日语"),
    "4": ("russian", "俄语"),
    "5": ("french", "法语"),
    "6": ("german", "德语"),
    "7": ("spanish", "西班牙语"),
    "8": ("brazilian", "葡萄牙语"),
    "9": ("koreana", "韩语"),
    "10": ("tchinese", "繁體中文"),
    "11": ("all", "全部语言"),
}

LANG_CODE_TO_CN_FULL = {
    "english": "英语", "schinese": "简体中文", "japanese": "日语", "russian": "俄语",
    "french": "法语", "german": "德语", "spanish": "西班牙语", "brazilian": "葡萄牙语",
    "koreana": "韩语", "tchinese": "繁體中文", "all": "全部语言",
    "vietnamese": "越南语", "polish": "波兰语", "thai": "泰语", "ukrainian": "乌克兰语",
    "bulgarian": "保加利亚语", "czech": "捷克语", "danish": "丹麦语", "dutch": "荷兰语",
    "finnish": "芬兰语", "greek": "希腊语", "hungarian": "匈牙利语", "indonesian": "印度尼西亚语",
    "italian": "意大利语", "norwegian": "挪威语", "portuguese": "葡萄牙语-葡萄牙",
    "romanian": "罗马尼亚语", "swedish": "瑞典语", "turkish": "土耳其语", "latinamerican": "西班牙语-拉丁美洲",
    "malay": "马来语", "catalan": "加泰罗尼亚语", "arabic": "阿拉伯语", "unknown": "未知语言"
}

REVIEW_FILTER_MAP = {
    "recent":   ("全部", "all"),
    "positive": ("仅好评", "positive"),
    "negative": ("仅差评", "negative"),
}
DEFAULT_REVIEW_FILTER = "recent"

PURCHASE_TYPE_MAP = {
    "1": ("all", "所有购买方式"),
    "2": ("steam", "仅Steam商店购买"),
    "3": ("non_steam", "仅非Steam购买"),
}
DEFAULT_PURCHASE_TYPE = "all"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ========== 通用工具 ==========
def retry_request(method: str, url: str, silent: bool = False, timeout: int = REQUEST_TIMEOUT, **kwargs) -> Optional[requests.Response]:
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    kwargs["headers"] = headers
    kwargs["verify"] = False

    last_exception = None
    for attempt in range(REQUEST_RETRIES):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 0.5)
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        pass
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < REQUEST_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1) + random.uniform(0, 0.5))
        except requests.exceptions.HTTPError as e:
            if not silent:
                logger.debug(f"HTTP错误 {e.response.status_code}: {url}")
            return None
        except Exception as e:
            last_exception = e
            break
    if not silent:
        logger.debug(f"请求失败: {url} - {last_exception}")
    return None


def format_playtime(minutes: int) -> str:
    if minutes <= 0:
        return "0秒"
    total_seconds = int(minutes * 60)
    hours, remainder = divmod(total_seconds, 3600)
    mins, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}小时")
    if mins:
        parts.append(f"{mins}分钟")
    if secs:
        parts.append(f"{secs}秒")
    return "".join(parts)


def ts_to_str(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else ""


def safe_int(value, default=0) -> int:
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


# ========== 数据获取 ==========
def get_game_names(appid: int, interactive_on_fail: bool = True) -> Tuple[str, str]:
    print("获取游戏信息...", end=" ", flush=True)

    def fetch(lang: str) -> Optional[str]:
        try:
            resp = requests.get(STEAM_STORE_API, params={"appids": appid, "l": lang},
                                timeout=GAME_NAME_TIMEOUT, headers={"User-Agent": USER_AGENT}, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if data.get(str(appid), {}).get("success"):
                    return data[str(appid)]["data"]["name"]
        except Exception:
            pass
        return None

    eng = chn = None
    with ThreadPoolExecutor(max_workers=2) as ex:
        eng_future = ex.submit(fetch, "english")
        chn_future = ex.submit(fetch, "schinese")
        try:
            eng = eng_future.result(timeout=GAME_NAME_WORKER_TIMEOUT)
        except FuturesTimeoutError:
            pass
        try:
            chn = chn_future.result(timeout=GAME_NAME_WORKER_TIMEOUT)
        except FuturesTimeoutError:
            pass

    print("完成")
    if eng or chn:
        if eng is None:
            eng = chn
        if chn is None or chn == eng:
            chn = eng
        return eng, chn

    print("警告: 无法获取游戏名称(建议更换加速器/代理)，是否继续爬取评测？(y/n，默认n): ", end="")
    choice = input().strip().lower()
    if choice in ('y', 'yes'):
        return "未知游戏", "未知游戏"
    else:
        print("已取消")
        sys.exit(0)


def get_reviews_summary(appid: int, language: str = "all",
                        review_filter: str = "recent",
                        purchase_type: str = "all") -> Optional[Dict]:
    """获取评测摘要（官方统计）"""
    url = STEAM_REVIEWS_API_TEMPLATE.format(appid)
    params = {
        "json": 1,
        "language": language,
        "filter": review_filter,
        "purchase_type": purchase_type,
        "num_per_page": 1,
        "cursor": "*"
    }
    resp = retry_request("GET", url, params=params, silent=False)
    if not resp:
        return None
    try:
        data = resp.json()
        return data.get("query_summary")
    except Exception:
        return None


def get_steam_reviews(appid: int, language: str = "english", max_reviews: int = 0,
                      review_filter: str = "recent", min_hours: float = 0.0,
                      time_filter_field: str = "playtime_forever",
                      purchase_type: str = "all") -> Tuple[List[Dict], float, float]:
    start_time = time.time()
    url = STEAM_REVIEWS_API_TEMPLATE.format(appid)
    cursor = "*"
    filtered = []
    total_raw = 0
    min_minutes = min_hours * 60 if min_hours > 0 else 0
    target = max_reviews if max_reviews > 0 else float("inf")

    filter_display = REVIEW_FILTER_MAP.get(review_filter, ("全部", "all"))[0]
    purchase_display = dict(PURCHASE_TYPE_MAP.values()).get(purchase_type, "所有购买方式")
    logger.info(f"抓取 {appid} | 语言:{language} | 类型:{filter_display} | 购买:{purchase_display} | 时长≥{min_hours}h | 目标:{max_reviews or '不限'}")

    while len(filtered) < target:
        params = {
            "json": 1,
            "language": language,
            "filter": review_filter,
            "purchase_type": purchase_type,
            "num_per_page": REVIEWS_PER_PAGE,
            "cursor": cursor
        }
        resp = retry_request("GET", url, params=params, silent=True)
        if not resp:
            logger.warning("API请求失败，停止抓取")
            break

        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"JSON解析失败: {e}")
            break

        reviews = data.get("reviews")
        if not isinstance(reviews, list) or not reviews:
            break

        total_raw += len(reviews)

        for r in reviews:
            if min_minutes > 0:
                author = r.get("author") or {}
                playtime = safe_int(author.get(time_filter_field))
                if playtime < min_minutes:
                    continue
            filtered.append(r)
            if len(filtered) >= target:
                break

        logger.info(f"进度: 原始 {total_raw} → 有效 {len(filtered)}")

        cursor = data.get("cursor", "")
        if not cursor:
            break
        if len(filtered) < target:
            time.sleep(BASE_SLEEP + random.uniform(0, RANDOM_JITTER))

    end_time = time.time()
    logger.info(f"抓取结束: 有效 {len(filtered)} 条，耗时 {end_time - start_time:.1f}s")
    return filtered, start_time, end_time


def validate_api_key(api_key: str) -> bool:
    if not api_key:
        return False
    test_steamid = "76561197960287930"
    params = {"key": api_key, "steamids": test_steamid}
    try:
        resp = requests.get(STEAM_USER_API, params=params, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": USER_AGENT}, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            players = data.get("response", {}).get("players")
            return players is not None and len(players) > 0
        return False
    except Exception:
        return False


def get_user_names(steamids: List[str], api_key: str) -> Dict[str, str]:
    if not api_key or not steamids:
        return {}
    unique_ids = list(set(steamids))
    result = {}
    for i in range(0, len(unique_ids), 100):
        batch = unique_ids[i:i+100]
        params = {"key": api_key, "steamids": ",".join(batch)}
        resp = retry_request("GET", STEAM_USER_API, params=params, silent=False)
        if not resp:
            logger.warning(f"昵称批次 {i//100+1} 失败")
            continue
        try:
            data = resp.json()
            players = data.get("response", {}).get("players", [])
            for p in players:
                result[p["steamid"]] = p.get("personaname", "Unknown")
        except Exception as e:
            logger.warning(f"解析昵称失败: {e}")
        if i + 100 < len(unique_ids):
            time.sleep(0.5)
    if not result:
        logger.warning("未获取到任何昵称，将使用SteamID代替")
    return result


def enrich_with_usernames(reviews: List[Dict], api_key: Optional[str]) -> None:
    if not api_key:
        for r in reviews:
            author = r.setdefault("author", {})
            author["personaname"] = author.get("steamid", "未知")
        return
    steamids = [r["author"]["steamid"] for r in reviews if "author" in r and "steamid" in r["author"]]
    if not steamids:
        return
    name_map = get_user_names(steamids, api_key)
    success = 0
    for r in reviews:
        sid = r["author"].get("steamid", "")
        r["author"]["personaname"] = name_map.get(sid, sid)
        if sid in name_map:
            success += 1
    if success == 0:
        logger.warning("所有昵称获取失败，使用SteamID替代")
    else:
        logger.info(f"昵称获取成功: {success}/{len(reviews)}")


# ========== 导出 ==========
def build_filename(appid: int, language: str, review_type: str, purchase_type: str) -> str:
    return f"steam_{appid}_{language}_{review_type}_{purchase_type}"


def save_to_csv(reviews: List[Dict], appid: int, language: str, review_type: str, purchase_type: str) -> None:
    if not reviews:
        return
    rows = []
    for r in reviews:
        a = r.get("author", {})
        steamid = a.get("steamid", "")
        review_link = f"https://steamcommunity.com/profiles/{steamid}/recommended/{appid}/"
        rows.append({
            "用户SteamID": steamid,
            "用户昵称": a.get("personaname", ""),
            "评测链接": review_link,
            "评测语言": r.get("language", ""),
            "评测内容": r.get("review", ""),
            "创建时间": ts_to_str(r.get("timestamp_created", 0)),
            "更新时间": ts_to_str(r.get("timestamp_updated", 0)),
            "是否推荐": r.get("voted_up", False),
            "有用数": safe_int(r.get("votes_up")),
            "有趣数": safe_int(r.get("votes_funny")),
            "评论数": safe_int(r.get("comment_count")),
            "游戏总时长(分钟)": safe_int(a.get("playtime_forever")),
            "评测时游戏时长(分钟)": safe_int(a.get("playtime_at_review")),
            "最近两周时长(分钟)": safe_int(a.get("playtime_last_two_weeks")),
            "最后游玩时间": ts_to_str(a.get("last_played", 0))
        })
    df = pd.DataFrame(rows)
    filename = build_filename(appid, language, review_type, purchase_type) + ".csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"CSV已保存: {filename}")


def save_to_txt(reviews: List[Dict], appid: int, game_eng: str, game_chn: str,
                language: str, review_type: str, purchase_type: str) -> None:
    if not reviews:
        return
    filename = build_filename(appid, language, review_type, purchase_type) + ".txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"{'='*80}\nSteam游戏评测报告\n游戏ID: {appid}\n英文名: {game_eng}\n中文名: {game_chn}\n")
        f.write(f"查询语言: {language}\n评测类型: {review_type}\n购买来源: {purchase_type}\n")
        f.write(f"评测总数: {len(reviews)}\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}\n\n")
        for idx, r in enumerate(reviews, 1):
            a = r.get("author", {})
            steamid = a.get("steamid", "")
            review_url = f"https://steamcommunity.com/profiles/{steamid}/recommended/{appid}/"
            
            # ---- 新增：获取购买来源（基于 steam_purchase 字段） ----
            sp = r.get("steam_purchase")
            if sp is True:
                purchase_source = "Steam直购"
            elif sp is False:
                purchase_source = "非Steam直购"
            else:
                purchase_source = "未知来源"
            # -----------------------------------------------------

            f.write(f"[评测 #{idx}]\n")
            f.write(f"用户: {a.get('personaname', a.get('steamid', '未知'))}\n")
            f.write(f"SteamID: {steamid}\n")
            f.write(f"评测链接: {review_url}\n")
            f.write(f"推荐: {'推荐' if r.get('voted_up') else '不推荐'}\n")
            f.write(f"语言: {r.get('language', '未知')}\n")
            # ---- 新增：写入购买来源 ----
            f.write(f"购买来源: {purchase_source}\n")
            # ----------------------------
            f.write(f"创建: {ts_to_str(r.get('timestamp_created', 0))}\n")
            f.write(f"更新: {ts_to_str(r.get('timestamp_updated', 0))}\n")
            f.write(f"总时长: {format_playtime(safe_int(a.get('playtime_forever')))}\n")
            f.write(f"评测时时长: {format_playtime(safe_int(a.get('playtime_at_review')))}\n")
            f.write(f"最近两周: {format_playtime(safe_int(a.get('playtime_last_two_weeks')))}\n")
            f.write(f"最后游戏: {ts_to_str(a.get('last_played', 0))}\n")
            f.write(f"有用: {safe_int(r.get('votes_up'))}  有趣: {safe_int(r.get('votes_funny'))}  评论: {safe_int(r.get('comment_count'))}\n")
            f.write(f"内容:\n{r.get('review', '无内容')}\n{'-'*80}\n\n")
    print(f"TXT已保存: {filename}")


# ========== 统计与展示 ==========
def print_details(reviews: List[Dict], max_display: int = 10):
    if not reviews:
        return
    print(f"\n共 {len(reviews)} 条评测，显示前{max_display}条:\n")
    for i, r in enumerate(reviews[:max_display], 1):
        a = r.get("author", {})
        print(f"[{i}] {a.get('personaname', a.get('steamid', '??'))} | {'推荐' if r.get('voted_up') else '不推荐'} | 评测时长: {format_playtime(safe_int(a.get('playtime_at_review')))}")
        txt = r.get("review", "")
        print(f"    内容: {txt[:150] + '...' if len(txt) > 150 else txt}\n{'-'*50}")


def print_summary(reviews: List[Dict], start_time: float, end_time: float, official_total: Optional[int] = None):
    if not reviews:
        return
    total = len(reviews)
    positive = sum(1 for r in reviews if r.get("voted_up"))

    # ---- 语言统计 ----
    lang_count = {}
    lang_positive = {}
    for r in reviews:
        lang = r.get("language", "unknown")
        lang_count[lang] = lang_count.get(lang, 0) + 1
        if r.get("voted_up"):
            lang_positive[lang] = lang_positive.get(lang, 0) + 1

    # ---- 购买来源统计（使用 steam_purchase 字段） ----
    purchase_stats = {}  # key: "steam" / "non_steam" / "unknown"
    for r in reviews:
        sp = r.get("steam_purchase")
        if sp is True:
            purchase = "steam"
        elif sp is False:
            purchase = "non_steam"
        else:
            purchase = "unknown"
        if purchase not in purchase_stats:
            purchase_stats[purchase] = {"total": 0, "positive": 0}
        purchase_stats[purchase]["total"] += 1
        if r.get("voted_up"):
            purchase_stats[purchase]["positive"] += 1

    # ---- 输出 ----
    print(f"\n===== 统计摘要 =====")
    print(f"爬取时段: {datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')} → {datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')} (耗时 {end_time - start_time:.2f}s)")
    print(f"总评测数: {total}")
    print(f"推荐: {positive}  不推荐: {total-positive}  推荐率: {positive/total*100:.1f}%")
    if official_total is not None and official_total > 0:
        ratio = total / official_total * 100
        print(f"官方总数: {official_total}  |  抓取完整性: {ratio:.1f}%")
        if total < official_total:
            print("提示: 由于Steam API分页限制，可能无法抓取全部评测。")

    # 购买来源统计输出
    print("\n购买来源统计 (购买方式 / 数量 / 好评 / 差评 / 好评率):")
    purchase_display = {
        "steam": "Steam直购",
        "non_steam": "非Steam直购",
        "unknown": "未知来源"
    }
    for purchase, stats in sorted(purchase_stats.items(), key=lambda x: x[1]["total"], reverse=True):
        cnt = stats["total"]
        pos = stats["positive"]
        neg = cnt - pos
        rate = pos / cnt * 100 if cnt > 0 else 0
        name = purchase_display.get(purchase, purchase)
        print(f"  {name}: {cnt}  好评: {pos}  差评: {neg}  好评率: {rate:.1f}%")

    # 语言分布
    print("\n语言分布 (语言 / 数量 / 占比 / 好评 / 差评 / 好评率):")
    for lang_code, cnt in sorted(lang_count.items(), key=lambda x: x[1], reverse=True):
        pos_cnt = lang_positive.get(lang_code, 0)
        neg_cnt = cnt - pos_cnt
        pos_rate = pos_cnt / cnt * 100 if cnt > 0 else 0
        lang_name = LANG_CODE_TO_CN_FULL.get(lang_code, lang_code)
        print(f"  {lang_name}: {cnt} ({cnt/total*100:.1f}%)  好评: {pos_cnt}  差评: {neg_cnt}  好评率: {pos_rate:.1f}%")


# ========== 核心处理 ==========
def process_reviews_core(reviews: List[Dict], api_key: Optional[str],
                         appid: int, game_eng: str, game_chn: str,
                         language: str, review_type: str, purchase_type: str,
                         start_time: float, end_time: float,
                         official_total: Optional[int] = None) -> bool:
    if not reviews:
        print("无符合条件的评测")
        return False

    enrich_with_usernames(reviews, api_key)
    print_details(reviews)
    print_summary(reviews, start_time, end_time, official_total)
    save_to_csv(reviews, appid, language, review_type, purchase_type)
    save_to_txt(reviews, appid, game_eng, game_chn, language, review_type, purchase_type)
    return True


def process_game(appid: int, api_key: Optional[str], language: str, max_reviews: int,
                 review_filter: str, purchase_type: str, min_hours: float,
                 time_filter_field: str, interactive: bool = False) -> bool:
    eng, chn = get_game_names(appid)
    print(f"游戏: {eng} / {chn}")

    if interactive:
        language, max_reviews, review_filter, purchase_type, min_hours, time_filter_field = get_query_params_interactive()

    summary = get_reviews_summary(appid, language, review_filter, purchase_type)
    official_total = None
    if summary:
        official_total = summary.get("total_reviews", 0)
        official_pos = summary.get("total_positive", 0)
        if official_total > 0:
            print(f"Steam官方数据: 总评测 {official_total} 条, 好评 {official_pos} 条, 好评率 {official_pos/official_total*100:.1f}%")
        else:
            print("官方返回总数为0，可能该游戏无评测或API限制")
    else:
        print("无法获取官方评测总数(建议更换加速器/代理)，将不进行完整性对比")

    reviews, start_ts, end_ts = get_steam_reviews(
        appid, language, max_reviews, review_filter,
        min_hours=min_hours, time_filter_field=time_filter_field,
        purchase_type=purchase_type
    )

    review_type_display = REVIEW_FILTER_MAP.get(review_filter, ("全部", "all"))[1]
    return process_reviews_core(reviews, api_key, appid, eng, chn,
                                language, review_type_display, purchase_type,
                                start_ts, end_ts, official_total)


# ========== 交互式参数获取 ==========
def get_query_params_interactive():
    print("\n选择评测语言:")
    for k, (_, name) in LANGUAGE_MENU_MAP.items():
        print(f"{k}. {name}")
    while True:
        lang_choice = input("请输入数字 (回车默认简体中文): ").strip()
        if lang_choice == "":
            language = DEFAULT_LANGUAGE
            break
        if lang_choice in LANGUAGE_MENU_MAP:
            language = LANGUAGE_MENU_MAP[lang_choice][0]
            break
        print("无效选择，请输入 1-11")
    print(f"已选: {LANGUAGE_MENU_MAP.get(lang_choice, ('', '简体中文'))[1] if lang_choice else '简体中文'}")

    try:
        max_reviews = int(input("获取数量 (0=无上限，回车默认0): ").strip() or "0")
        max_reviews = max(0, max_reviews)
    except ValueError:
        max_reviews = 0

    print("\n选择评测类型:")
    type_options = [(k, v[0]) for k, v in REVIEW_FILTER_MAP.items()]
    for i, (code, display) in enumerate(type_options, 1):
        print(f"{i}. {display}")
    type_code_to_api = {str(i): code for i, (code, _) in enumerate(type_options, 1)}
    while True:
        type_choice = input("请输入数字 (回车默认全部): ").strip()
        if type_choice == "":
            review_filter = DEFAULT_REVIEW_FILTER
            break
        if type_choice in type_code_to_api:
            review_filter = type_code_to_api[type_choice]
            break
        print("无效选择")
    print(f"已选: {REVIEW_FILTER_MAP[review_filter][0]}")

    print("\n选择购买来源:")
    for k, (_, name) in PURCHASE_TYPE_MAP.items():
        print(f"{k}. {name}")
    while True:
        purchase_choice = input("请输入数字 (回车默认所有购买方式): ").strip()
        if purchase_choice == "":
            purchase_type = DEFAULT_PURCHASE_TYPE
            break
        if purchase_choice in PURCHASE_TYPE_MAP:
            purchase_type = PURCHASE_TYPE_MAP[purchase_choice][0]
            break
        print("无效选择")
    print(f"已选: {PURCHASE_TYPE_MAP.get(purchase_choice, ('', '所有购买方式'))[1] if purchase_choice else '所有购买方式'}")

    min_hours = 0.0
    time_filter_field = "playtime_forever"
    time_choice = input("\n是否按游戏时长筛选？(y/n，回车默认n): ").strip().lower()
    if time_choice in ('y', 'yes'):
        field_choice = input("基准:1.游戏总时长  2.评测时游戏时长 (回车默认1): ").strip() or "1"
        time_filter_field = "playtime_forever" if field_choice == "1" else "playtime_at_review"
        while True:
            try:
                min_hours = float(input("最少小时数: ").strip())
                if min_hours >= 0:
                    break
                print("小时数不能为负")
            except ValueError:
                print("请输入数字")
        if max_reviews == 0 and min_hours > 0:
            print("注意: 未设数量上限且有时长过滤，可能耗时较长，请耐心等待。")
            input("按 Enter 继续...")

    return language, max_reviews, review_filter, purchase_type, min_hours, time_filter_field


# ========== 命令行解析 ==========
def parse_arguments():
    parser = argparse.ArgumentParser(description="Steam游戏评测爬虫工具")
    parser.add_argument("--appid", type=int, help="游戏ID")
    parser.add_argument("--api-key", help="Steam Web API Key (获取用户昵称)")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help=f"语言代码，默认 {DEFAULT_LANGUAGE}")
    parser.add_argument("--max", type=int, default=0, help="最大获取数量 (0=无上限)")
    parser.add_argument("--filter-type", choices=["recent", "positive", "negative"], default="recent",
                        help="评测类型: recent(全部), positive(仅好评), negative(仅差评)")
    parser.add_argument("--purchase", choices=["all", "steam", "non_steam"], default="all",
                        help="购买来源: all(所有), steam(仅Steam商店), non_steam(非Steam)")
    parser.add_argument("--min-hours", type=float, default=0, help="最少游戏时长(小时)")
    parser.add_argument("--filter-field", choices=["playtime_forever", "playtime_at_review"],
                        default="playtime_forever", help="时长筛选字段")
    return parser.parse_args()


# ========== 主程序 ==========
def main():
    args = parse_arguments()

    if args.appid:
        success = process_game(
            args.appid, args.api_key, args.language, args.max,
            args.filter_type, args.purchase, args.min_hours, args.filter_field,
            interactive=False
        )
        sys.exit(0 if success else 1)

    print("=" * 60)
    print("Steam游戏评测爬虫工具")
    print("支持: 语言/数量/类型/购买来源/时长筛选 | CSV+TXT报告")
    print("=" * 60)

        # ------------------- 新增：预检测 Steam 连接 -------------------
    print("\n正在测试 Steam 连接...")
    test_appid = 730  # 使用 CS:GO 作为测试
    test_eng, test_chn = get_game_names(test_appid, interactive_on_fail=False)
    if test_eng == "未知游戏" or test_eng is None:
        print("⚠️ 警告: 无法获取 Steam 游戏信息，可能网络连接或代理存在问题。")
        print("如果继续，后续爬取可能失败或无法正确获取游戏名称。")
        choice = input("是否继续？(y/n，默认n): ").strip().lower()
        if choice not in ('y', 'yes'):
            print("退出程序。")
            return
    else:
        print(f"✓ Steam 连接正常，测试游戏: {test_eng} / {test_chn}")
    # -------------------------------------------------------------
    
    api_key = None
    need_key = input("\n是否输入 Steam Web API Key? \n(Key只在本窗口使用，仅用于获取用户昵称，并不会上传服务器，不填写key脚本依然正常使用，仅仅只是无法获取正确的用户昵称)\n！！！注意每个API Key每日最多可调用10万次，短时间频繁使用，额度/账户可能会被V社封禁，请谨慎使用！！！\n请输入y/n，回车默认n): ").strip().lower()
    if need_key in ('y', 'yes'):
        raw_key = input("请输入API Key: ").strip()
        if raw_key:
            print("验证中...")
            if validate_api_key(raw_key):
                api_key = raw_key
                print("✓ API Key有效，将显示用户昵称")
            else:
                print("✗ 无效，将只显示SteamID")
        else:
            print("未提供，仅显示SteamID")

    while True:
        user_input = input("\n请输入游戏ID (或 q 退出): ").strip()
        if user_input.lower() in ("q", "quit", "exit"):
            break
        try:
            appid = int(user_input)
        except ValueError:
            print("错误: 请输入数字ID")
            continue

        process_game(appid, api_key, DEFAULT_LANGUAGE, 0, DEFAULT_REVIEW_FILTER,
                     DEFAULT_PURCHASE_TYPE, 0.0, "playtime_forever", interactive=True)

        while True:
            print("\n" + "-" * 40)
            print("下一步:")
            print("1. 重新查询当前游戏 (修改参数)")
            print("2. 查询其他游戏")
            print("3. 退出程序")
            choice = input("请选择 (1/2/3，默认3): ").strip() or "3"
            if choice == "1":
                process_game(appid, api_key, DEFAULT_LANGUAGE, 0, DEFAULT_REVIEW_FILTER,
                             DEFAULT_PURCHASE_TYPE, 0.0, "playtime_forever", interactive=True)
            elif choice == "2":
                break
            elif choice == "3":
                return
            else:
                print("无效选项，请输入 1、2 或 3")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        logger.exception(f"程序异常: {e}")
        input("按回车键退出...")