#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿スクリプト

機能概要:
- DMM Affiliate API を floor=videoa / videoc で横断取得
- VR厳密判定:
    A) URL: media_type=vr or /vrvideo/
    B) CID: *vrNN / dsvrNN など
    C) タイトルにVRトークン ＋ ジャンルにもVR系語彙
  → タイトルだけVRは除外
- 発売判定は「日時」ではなく **可用性** で決定:
    1) サンプル画像の HEAD/GET が 200 かつ 10KB 以上
    2) 個別詳細ページ（video./www.）が 200
  どちらか満たさない場合は未公開扱いで除外（=前倒し防止）
"""

# ====== 標準ライブラリ ======
import html
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# ====== サードパーティ ======
import pytz
import requests
from bs4 import BeautifulSoup

# ====== collections.Iterable 互換パッチ（wordpress_xmlrpc 対策） ======
import collections as _collections
import collections.abc as _collections_abc
for _name in ("Iterable", "Mapping", "MutableMapping", "Sequence"):
    if not hasattr(_collections, _name) and hasattr(_collections_abc, _name):
        setattr(_collections, _name, getattr(_collections_abc, _name))

from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.compat import xmlrpc_client
from wordpress_xmlrpc.methods import media, posts, taxonomies
from wordpress_xmlrpc.methods.posts import GetPosts


# ============================================================
# 定数・設定
# ============================================================
DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"
JST = pytz.timezone("Asia/Tokyo")

# HTTPタイムアウト（秒）
HEAD_TIMEOUT = 10
GET_TIMEOUT = 12
API_TIMEOUT = 15

# 画像可用性判定の閾値
MIN_IMAGE_BYTES = 10 * 1024      # 10KB 以上で「実体あり」とみなす
IMAGE_PROBE_BYTES = 16 * 1024    # 確認用に読み込む最大バイト数
IMAGE_CHUNK_SIZE = 4096
MAX_IMAGES_TO_CHECK = 2          # 先頭から何枚まで可用性チェックするか

# APIリクエスト間のスリープ（秒）
API_SLEEP_SECONDS = 0.2

# 日付フォーマット候補
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

# VR判定用トークン
VR_TITLE_KEYWORDS = ["【VR】", "VR専用", "8K VR", "8KVR", "ハイクオリティVR"]
VR_GENRE_WORDS = [
    "VR", "ＶＲ", "VR専用", "8KVR", "8K VR",
    "ハイクオリティVR", "VR動画", "VR作品",
]
VR_CID_PATTERN = re.compile(r"(?:^|[^a-z])(dsvr|idvr|[a-z]*vr)\d{2,}")
VR_TITLE_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])VR(?![A-Za-z0-9])")


class Config:
    """環境変数から取得する実行時設定。"""

    POST_LIMIT = int(os.getenv("POST_LIMIT", "2"))
    RECENT_DAYS = int(os.getenv("RECENT_DAYS", "3"))
    HITS = int(os.getenv("HITS", "10"))
    # 通常時に取得するページ数（POST_LIMITに達したら早期終了）
    MAX_PAGES = int(os.getenv("MAX_PAGES", "2"))
    # 通常範囲で見つからなかった場合の上限（フォールバック拡張）
    MAX_PAGES_FALLBACK = int(os.getenv("MAX_PAGES_FALLBACK", "6"))
    FLOORS = [f.strip() for f in os.getenv("FLOORS", "videoa,videoc").split(",") if f.strip()]
    AGE_GATE_COOKIE = os.getenv("AGE_GATE_COOKIE", "").strip()
    # 詳細ページから説明文をスクレイピングするか（"1" で有効）
    SCRAPE_DESC = os.getenv("SCRAPE_DESC", "0") == "1"
    # 詳細ページ取得時の優先ドメイン（"www" / "video" / "" のいずれか）
    FORCE_DETAIL_DOMAIN = os.getenv("FORCE_DETAIL_DOMAIN", "").strip().lower()
    # 発売前（dateが未来）のアイテムを除外するか（デフォルト無効：予約商品も投稿）
    EXCLUDE_PRE_RELEASE = os.getenv("EXCLUDE_PRE_RELEASE", "0") == "1"
    # WordPressのタグとして登録する iteminfo フィールド（カンマ区切り）
    # デフォルト: ジャンル＋出演者＋メーカー名
    TAG_FIELDS = [
        f.strip() for f in os.getenv("TAG_FIELDS", "genre,actress,maker").split(",") if f.strip()
    ]
    # タグ数の上限（多すぎるとSEO的に逆効果）
    MAX_TAGS = int(os.getenv("MAX_TAGS", "30"))
    # タグ名と一致する既存WPカテゴリも自動でチェック（割り当て）するか
    AUTO_MATCH_CATEGORY = os.getenv("AUTO_MATCH_CATEGORY", "1") == "1"
    # スクレイピングのデバッグ出力（"1"で詳細ログ、"2"でHTMLをファイル保存）
    SCRAPE_DEBUG = os.getenv("SCRAPE_DEBUG", "1")


# 説明文として採用する文字数の範囲
DESC_MIN_LEN = 20
DESC_MAX_LEN = 1200

# スクレイピング時の説明文セレクタ候補（本文用 ＝ 最優先）
# video.dmm.co.jp（新UI）と www.dmm.co.jp（旧UI）の両方に対応
DESC_SELECTORS_BODY = [
    # ===== www.dmm.co.jp (旧UI / Adult Video) - 説明文の定番位置 =====
    "div.mg-b20.lh4 p.mg-b20",
    "div.mg-b20.lh4 p",
    "div.mg-b20.lh4",
    "p.mg-b20.lh4",
    # ===== 詳細説明・あらすじ系 =====
    'div[class*="comment"] p',
    'div[class*="story"] p',
    'div[class*="synopsis"] p',
    'div[class*="caption"] p',
    'div[class*="introduction"] p',
    'div[class*="text-overflow"]',
    'p[class*="text-overflow"]',
    # ===== video.dmm.co.jp (新UI / React) =====
    '[data-e2e="description"]',
    '[data-e2e="summary"]',
    '[data-testid="description"]',
    '[data-testid="summary"]',
    'div[class*="summary__txt"]',
    'div[class*="Summary__text"]',
    'section[class*="summary"] p',
    'section[class*="description"] p',
    'div[class*="ProductDescription"] p',
    'div[class*="productDescription"] p',
    'div[class*="ProductDetail"] p',
    # ===== descriptionで囲われた汎用パターン（user指摘） =====
    '[class*="description"]:not([class*="title"]):not([class*="header"]):not([class*="label"])',
    '[id*="description"]',
    '[itemprop="description"]',
]

# メタタグ系（最終フォールバック）- 本文が取れない時のみ使用
DESC_SELECTORS_META = [
    'meta[property="og:description"]',
    'meta[name="description"]',
    'meta[name="twitter:description"]',
]

# JSON-LDから説明文を取り出すときに見るキー
JSONLD_DESC_KEYS = ("description", "abstract", "headline")

# JSON（__NEXT_DATA__ 等）内で説明文として扱うキー名
JSON_DESC_KEYS = (
    "description", "longDescription", "shortDescription",
    "comment", "caption", "summary", "synopsis",
    "story", "abstract", "body", "text", "content",
    "introduction", "outline", "overview",
)

# 「メタ情報っぽい」と判定するためのパターン（スコアペナルティ用）
METADATA_LIKE_PATTERNS = (
    "ジャンル：", "ジャンル:",
    "出演：", "出演:",
    "メーカー：", "メーカー:",
    "シリーズ：", "シリーズ:",
    "レーベル：", "レーベル:",
    "収録時間：", "収録時間:",
    "監督：", "監督:",
    "品番：", "品番:",
)


# ============================================================
# 共通ユーティリティ
# ============================================================
def now_jst() -> datetime:
    """日本時間の現在時刻を返す。"""
    return datetime.now(JST)


def parse_jst_date(value: str) -> datetime:
    """文字列をJSTのdatetimeにパース。失敗時は1970-01-01を返す。"""
    text = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return JST.localize(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return JST.localize(datetime(1970, 1, 1))


def get_env(key: str, required: bool = True) -> Optional[str]:
    """環境変数を取得。required=True で未設定なら例外。"""
    value = os.getenv(key)
    if required and not value:
        raise RuntimeError(f"環境変数 {key} が未設定です")
    return value


def make_affiliate_link(url: str, affiliate_id: str) -> str:
    """URLにアフィリエイトIDを付与して返す。"""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query["affiliate_id"] = affiliate_id
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(query), parsed.fragment,
    ))


def join_names(entries: Iterable[Any]) -> str:
    """[{'name': ...}, ...] 形式のリストから名前を「、」で連結。"""
    return "、".join(
        entry.get("name", "") for entry in entries if isinstance(entry, dict)
    )


def extract_sample_images(item: dict) -> list[str]:
    """アイテムからサンプル画像URLのリストを抽出（large優先、small次点）。"""
    sample_url = item.get("sampleImageURL") or {}
    large = (sample_url.get("sample_l") or {}).get("image") or []
    small = (sample_url.get("sample_s") or {}).get("image") or []
    return large or small or []


# ============================================================
# VR判定（超厳密）
# ============================================================
def _has_vr_token_in_title(title: str) -> bool:
    """タイトル中に独立したVRトークンまたは特定キーワードがあるか。"""
    title = title or ""
    if VR_TITLE_TOKEN_PATTERN.search(title):
        return True
    return any(keyword in title for keyword in VR_TITLE_KEYWORDS)


def _genre_has_vr_words(iteminfo: dict) -> bool:
    """ジャンル名にVR系語彙が含まれるか。"""
    genres = (iteminfo or {}).get("genre", [])
    joined = " ".join(g.get("name", "") for g in genres if isinstance(g, dict))
    return any(
        re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", joined)
        for word in VR_GENRE_WORDS
    )


def _url_indicates_vr(url: str) -> bool:
    """URLからVR作品であることを判定。"""
    try:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query))
        if query.get("media_type", "").lower() == "vr":
            return True
        if "/vrvideo/" in parsed.path:
            return True
    except Exception:
        pass
    return False


def _cid_indicates_vr(item: dict) -> bool:
    """CID（コンテンツID）の命名規則からVRを判定。"""
    cid = (item.get("content_id") or item.get("product_id") or "").lower()
    return bool(VR_CID_PATTERN.search(cid))


def contains_vr(item: dict) -> bool:
    """3つの判定基準（URL/CID/タイトル+ジャンル）でVR作品か判定。"""
    if _url_indicates_vr(item.get("URL", "")):
        return True
    if _cid_indicates_vr(item):
        return True
    if (
        _has_vr_token_in_title(item.get("title", ""))
        and _genre_has_vr_words(item.get("iteminfo") or {})
    ):
        return True
    return False


# ============================================================
# 可用性（発売済み相当）判定
# ============================================================
def _headers_for_html() -> dict[str, str]:
    """HTMLページ取得用のHTTPヘッダを返す。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://video.dmm.co.jp/",
    }
    if Config.AGE_GATE_COOKIE:
        headers["Cookie"] = Config.AGE_GATE_COOKIE
    return headers


def _candidate_detail_urls(item: dict) -> list[str]:
    """個別詳細ページの候補URLを重複排除して返す。"""
    cid = (item.get("content_id") or item.get("product_id") or "").strip().lower()
    item_url = item.get("URL", "")

    candidates: list[str] = []
    if cid:
        candidates.extend([
            f"https://video.dmm.co.jp/av/content/?id={cid}",
            f"https://www.dmm.co.jp/digital/vrvideo/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/vrvideo/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
        ])
    if item_url:
        candidates.append(item_url)

    seen: set[str] = set()
    unique: list[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _is_success_status(status_code: int) -> bool:
    """HTTPステータスが2xxか判定。"""
    return 200 <= status_code < 300


def _url_ok(url: str) -> bool:
    """URLが取得可能（2xx）か判定。HEAD不可なら GETでフォールバック。"""
    try:
        response = requests.head(
            url, headers=_headers_for_html(),
            timeout=HEAD_TIMEOUT, allow_redirects=True,
        )
        if response.status_code == 405:  # HEAD不可サイト
            response = requests.get(
                url, headers=_headers_for_html(),
                timeout=GET_TIMEOUT, allow_redirects=True, stream=True,
            )
        return _is_success_status(response.status_code)
    except Exception:
        return False


def _measure_streamed_size(response: requests.Response, max_bytes: int) -> int:
    """ストリーミングレスポンスから最大 max_bytes まで読み込み、読み込んだサイズを返す。"""
    size = 0
    for chunk in response.iter_content(IMAGE_CHUNK_SIZE):
        size += len(chunk)
        if size >= max_bytes:
            break
    return size


def _image_ok(url: str) -> bool:
    """画像URLが2xx かつ MIN_IMAGE_BYTES 以上のサイズか判定。"""
    try:
        response = requests.head(url, timeout=HEAD_TIMEOUT, allow_redirects=True)

        # HEAD不可 or サイズ不明 → GETで実体サイズを確認
        if response.status_code == 405 or response.headers.get("content-length") in (None, "0"):
            response = requests.get(url, timeout=GET_TIMEOUT, allow_redirects=True, stream=True)
            size = _measure_streamed_size(response, IMAGE_PROBE_BYTES)
            return _is_success_status(response.status_code) and size >= MIN_IMAGE_BYTES

        # Content-Length で判定
        try:
            content_length = int(response.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        return _is_success_status(response.status_code) and content_length >= MIN_IMAGE_BYTES
    except Exception:
        return False


def _any_image_available(images: list[str]) -> bool:
    """先頭から MAX_IMAGES_TO_CHECK 枚までを確認し、いずれか可用なら True。"""
    for image_url in images[:MAX_IMAGES_TO_CHECK]:
        if _image_ok(image_url):
            return True
    return False


def _any_detail_page_available(item: dict) -> bool:
    """詳細ページ候補のうちいずれか1つでも200ならTrue。"""
    return any(_url_ok(url) for url in _candidate_detail_urls(item))


def is_available_now(item: dict) -> bool:
    """
    "前倒し禁止" のための可用性判定。

    以下を両方満たす場合のみ True:
      - サンプル画像の実体が取得可能（>= MIN_IMAGE_BYTES）
      - 詳細ページが HTTP 200
    """
    images = extract_sample_images(item)
    if not _any_image_available(images):
        return False
    return _any_detail_page_available(item)


# ============================================================
# DMM API
# ============================================================
def dmm_request(params: dict) -> dict:
    """DMM ItemList API を呼び出し、result 部分を返す。失敗時は空dict。"""
    try:
        response = requests.get(DMM_API_URL, params=params, timeout=API_TIMEOUT)
    except requests.RequestException as e:
        print(f"[API] リクエスト失敗: {e}")
        return {}

    if response.status_code != 200:
        print(f"[API] Error {response.status_code}: {response.text[:200]}")
        return {}

    data = response.json()
    return data.get("result", {}) or {}


def base_params(offset: int, floor: str, use_keyword: bool = True) -> dict:
    """ItemList API の共通パラメータを生成。"""
    params = {
        "api_id": get_env("DMM_API_ID"),
        "affiliate_id": get_env("DMM_AFFILIATE_ID"),
        "site": "FANZA",
        "service": "digital",
        "floor": floor,        # videoa / videoc
        "sort": "date",
        "output": "json",
        "hits": Config.HITS,
        "offset": offset,      # 1起点
    }
    if use_keyword:
        params["keyword"] = "VR"
    return params


def is_pre_release(item: dict) -> bool:
    """API の date が未来の場合は発売前と判定。"""
    release_date = parse_jst_date(item.get("date", ""))
    return release_date > now_jst()


def _iter_floor_pages(floor: str, max_pages: int) -> Iterator[tuple[int, list[dict]]]:
    """指定フロアのページを1ページずつ遅延取得（ページ番号付き）。"""
    for page in range(max_pages):
        offset = 1 + page * Config.HITS
        print(f"[API] floor={floor} page={page + 1} offset={offset}")
        result = dmm_request(base_params(offset, floor, use_keyword=True))
        page_items = result.get("items", []) or []
        print(f"[API] 取得 {len(page_items)} 件")
        if not page_items:
            return
        yield page + 1, page_items
        time.sleep(API_SLEEP_SECONDS)


def iter_vr_available_items() -> Iterator[dict]:
    """
    全フロアからVR＋発売済＋可用性OKのアイテムを遅延列挙する。

    通常は MAX_PAGES まで取得して終了するが、呼び出し側が値を消費し続けた場合、
    自動的に MAX_PAGES_FALLBACK までフォールバック拡張する。
    呼び出し側が break すれば、それ以降のAPI/HTTPリクエストは発生しない。
    """
    print("[API] フロア横断取得開始 →", ",".join(Config.FLOORS))
    print(
        f"[API] MAX_PAGES={Config.MAX_PAGES} "
        f"（フォールバック拡張: 最大 {Config.MAX_PAGES_FALLBACK} ページまで）"
    )

    total_seen = 0
    total_vr = 0
    total_prerelease = 0
    total_available = 0
    upper_limit = max(Config.MAX_PAGES, Config.MAX_PAGES_FALLBACK)

    for floor in Config.FLOORS:
        fallback_announced = False
        for page_no, page_items in _iter_floor_pages(floor, upper_limit):
            # 通常範囲を超えたタイミングで一度だけログ出力
            if page_no > Config.MAX_PAGES and not fallback_announced:
                print(
                    f"[フォールバック] floor={floor} page={page_no} "
                    f"通常範囲({Config.MAX_PAGES}ページ)を超えたため拡張検索中"
                )
                fallback_announced = True

            total_seen += len(page_items)
            for item in page_items:
                if not contains_vr(item):
                    continue
                total_vr += 1

                # 発売日チェック（=正の発売判定）：date が未来なら予約品として除外
                # 予約品は画像も詳細ページも存在することがあるため、可用性だけでは判断不可
                if Config.EXCLUDE_PRE_RELEASE and is_pre_release(item):
                    total_prerelease += 1
                    print(f"  - [発売前スキップ] {item.get('date','')} {item.get('title', '')}")
                    continue

                # 発売済みであることを前提に、可用性チェック（画像・詳細ページの実体）
                if not is_available_now(item):
                    print(f"  - [NG / 実体取得失敗] {item.get('title', '')}")
                    continue

                total_available += 1
                print(f"  - [OK] {item.get('date','')} {item.get('title', '')}")
                yield item

    print(
        f"[API] 総取得: {total_seen} / VR判定: {total_vr} / "
        f"発売前除外: {total_prerelease} / 可用性OK: {total_available}"
    )


# ============================================================
# 本文フォールバック
# ============================================================
def _pick_existing_description(item: dict) -> Optional[str]:
    """item から既存の説明文を探す（DESC_MIN_LEN〜DESC_MAX_LEN文字の範囲）。"""
    iteminfo = item.get("iteminfo") or {}
    for key in ("description", "comment", "story"):
        value = (item.get(key) or iteminfo.get(key) or "").strip()
        if DESC_MIN_LEN <= len(value) <= DESC_MAX_LEN:
            return html.unescape(value)
    return None


def _filter_urls_by_domain(urls: list[str]) -> list[str]:
    """FORCE_DETAIL_DOMAIN が指定されていれば、対応するドメインのURLを優先。"""
    domain = Config.FORCE_DETAIL_DOMAIN
    if not domain:
        return urls
    preferred = [u for u in urls if f"{domain}.dmm.co.jp" in u]
    others = [u for u in urls if u not in preferred]
    return preferred + others


def _clean_text(text: str) -> str:
    """空白・改行を整理した文字列を返す。"""
    text = html.unescape(text or "")
    text = re.sub(r"[​‌‍﻿]", "", text)  # ゼロ幅文字除去
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_valid_description(text: str) -> bool:
    """説明文として妥当な長さ・内容か判定。"""
    if not text:
        return False
    if not (DESC_MIN_LEN <= len(text) <= DESC_MAX_LEN):
        return False
    # 日本語文字が含まれていることを軽くチェック
    return bool(re.search(r"[぀-ヿ一-鿿]", text))


def _score_description(text: str) -> int:
    """
    説明文候補のスコアを計算。
    長いほど高スコア。メタ情報っぽいラベルが多いとペナルティ。
    """
    if not _is_valid_description(text):
        return -1
    score = len(text)
    # メタ情報パターンが含まれているとペナルティ（1件あたり-200）
    for pattern in METADATA_LIKE_PATTERNS:
        if pattern in text:
            score -= 200
    # 「DMM」「FANZA」「無料サンプル」等のSEO定型句もペナルティ
    seo_patterns = ("DMM", "FANZA", "無料サンプル", "ダウンロード", "ストリーミング")
    for pattern in seo_patterns:
        if pattern in text:
            score -= 50
    return score


def _collect_candidates_from_selectors(
    soup: BeautifulSoup, selectors: list[str], label: str
) -> list[tuple[int, str, str]]:
    """セレクタ群から候補テキストを収集（スコア、本文、ラベル）のリストを返す。"""
    results: list[tuple[int, str, str]] = []
    for selector in selectors:
        try:
            elements = soup.select(selector)
        except Exception:
            continue
        for element in elements:
            if element.name == "meta":
                text = _clean_text(element.get("content", ""))
            else:
                text = _clean_text(element.get_text(" ", strip=True))
            score = _score_description(text)
            if score > 0:
                results.append((score, text, f"{label}:{selector}"))
    return results


def _collect_candidates_from_jsonld(soup: BeautifulSoup) -> list[tuple[int, str, str]]:
    """JSON-LDから候補テキストを収集。"""
    results: list[tuple[int, str, str]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            for key in JSONLD_DESC_KEYS:
                value = entry.get(key)
                if isinstance(value, str):
                    text = _clean_text(value)
                    score = _score_description(text)
                    if score > 0:
                        results.append((score, text, f"jsonld:{key}"))
    return results


def _walk_json_for_descriptions(
    obj: Any, max_depth: int = 12, _depth: int = 0
) -> Iterator[tuple[str, str]]:
    """
    JSON オブジェクトを再帰的に走査し、説明文として使えそうな (キー, 値) を yield する。
    キー名が JSON_DESC_KEYS に含まれ、値が文字列であれば対象。
    """
    if _depth > max_depth:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = str(key).lower()
            if isinstance(value, str) and any(k in key_lower for k in JSON_DESC_KEYS):
                yield (str(key), value)
            else:
                yield from _walk_json_for_descriptions(value, max_depth, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_for_descriptions(item, max_depth, _depth + 1)


def _collect_candidates_from_next_data(soup: BeautifulSoup) -> list[tuple[int, str, str]]:
    """
    Next.js の __NEXT_DATA__ や、その他のインライン JSON スクリプトから
    説明文候補を収集する。video.dmm.co.jp（React/Next.js製）対策。
    """
    results: list[tuple[int, str, str]] = []
    script_selectors = [
        ("script", {"id": "__NEXT_DATA__"}),
        ("script", {"id": "__NUXT_DATA__"}),
        ("script", {"id": "__APOLLO_STATE__"}),
        ("script", {"type": "application/json"}),
    ]
    seen_scripts: set[int] = set()

    for tag_name, attrs in script_selectors:
        for script in soup.find_all(tag_name, attrs=attrs):
            sid = id(script)
            if sid in seen_scripts:
                continue
            seen_scripts.add(sid)

            raw = script.string or script.get_text() or ""
            raw = raw.strip()
            if not raw or not (raw.startswith("{") or raw.startswith("[")):
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue

            for key, value in _walk_json_for_descriptions(data):
                text = _clean_text(value)
                score = _score_description(text)
                if score > 0:
                    results.append((score, text, f"json:{key}"))
    return results


def _collect_longest_paragraphs(soup: BeautifulSoup) -> list[tuple[int, str, str]]:
    """フォールバック: 全 p/div からスコア付き候補を収集。"""
    results: list[tuple[int, str, str]] = []
    for tag in soup.find_all(["p", "div"]):
        text = _clean_text(tag.get_text(" ", strip=True))
        score = _score_description(text)
        if score > 0:
            results.append((score, text, f"longest:{tag.name}"))
    return results


def _extract_description_from_html(html_text: str) -> Optional[str]:
    """
    HTMLから説明文を抽出する（スコアリング方式）。
    全戦略の候補を集めて、最もスコアが高いものを採用。
    """
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        soup = BeautifulSoup(html_text, "html.parser")

    candidates: list[tuple[int, str, str]] = []
    # 本文系セレクタ（最優先）
    candidates.extend(_collect_candidates_from_selectors(soup, DESC_SELECTORS_BODY, "body"))
    # Next.js / Nuxt / Apollo の埋め込みJSONから探索（video.dmm.co.jp等の動的レンダリング対策）
    candidates.extend(_collect_candidates_from_next_data(soup))
    # JSON-LD（構造化データ）
    candidates.extend(_collect_candidates_from_jsonld(soup))
    # メタタグ（最終手段）- 低めの最大スコアにするため上限を300に圧縮
    meta_candidates = _collect_candidates_from_selectors(soup, DESC_SELECTORS_META, "meta")
    meta_candidates = [(min(s, 300), t, l) for s, t, l in meta_candidates]
    candidates.extend(meta_candidates)
    # 最長のp/div（最終フォールバック）
    candidates.extend(_collect_longest_paragraphs(soup))

    if not candidates:
        return None

    # スコア降順で並べて最良を採用
    candidates.sort(key=lambda x: -x[0])
    best_score, best_text, best_label = candidates[0]
    print(f"  [scrape] 採用: {best_label} (score={best_score}, len={len(best_text)})")
    return best_text


def _dump_html_for_debug(item: dict, url: str, html_text: str) -> None:
    """SCRAPE_DEBUG=2 のとき、取得HTMLを outputs/ 配下に保存（解析用）。"""
    if Config.SCRAPE_DEBUG != "2":
        return
    try:
        cid = (item.get("content_id") or item.get("product_id") or "unknown").strip()
        safe_cid = re.sub(r"[^a-zA-Z0-9_-]", "_", cid)[:50]
        domain = urlparse(url).netloc.replace(".", "_")
        filename = f"debug_{safe_cid}_{domain}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html_text)
        print(f"  [scrape-debug] HTML保存: {filename} ({len(html_text)} bytes)")
    except Exception as e:
        print(f"  [scrape-debug] HTML保存失敗: {e}")


def scrape_description(item: dict) -> Optional[str]:
    """詳細ページから説明文をスクレイピングする。SCRAPE_DESCが無効なら何もしない。"""
    if not Config.SCRAPE_DESC:
        return None

    urls = _filter_urls_by_domain(_candidate_detail_urls(item))
    cid = item.get("content_id") or item.get("product_id") or "?"
    print(f"[scrape] CID={cid} 候補URL {len(urls)}件:")
    for u in urls:
        print(f"  - {u}")

    for url in urls:
        try:
            response = requests.get(
                url, headers=_headers_for_html(),
                timeout=GET_TIMEOUT, allow_redirects=True,
            )
            print(
                f"  [scrape] GET {url} → status={response.status_code} "
                f"final_url={response.url} len={len(response.text)}"
            )
            if not _is_success_status(response.status_code):
                continue

            # 年齢認証ページに飛ばされていないか判定
            final_url = response.url.lower()
            if "age_check" in final_url or "agecheck" in final_url:
                print(f"  [scrape] 年齢認証ページに転送（COOKIE未設定/期限切れの可能性）")
                continue

            response.encoding = response.apparent_encoding or response.encoding
            _dump_html_for_debug(item, url, response.text)

            description = _extract_description_from_html(response.text)
            if description:
                preview = description[:60].replace("\n", " ")
                print(f"  [scrape] 取得成功: len={len(description)} preview='{preview}...'")
                return description
            print(f"  [scrape] このURLからは説明文が抽出できず（次のURL試行）")
        except Exception as e:
            print(f"  [scrape] 失敗 {url}: {e}")
            continue

    print(f"  [scrape] 全URLで説明文取得失敗 → メタ情報フォールバックに移行")
    return None


def _compose_metadata_description(item: dict) -> str:
    """メタ情報から説明文を組み立てる。"""
    iteminfo = item.get("iteminfo") or {}
    cast = join_names(iteminfo.get("actress", []))
    label = join_names(iteminfo.get("label", []))
    genres = join_names(iteminfo.get("genre", []))
    series = join_names(iteminfo.get("series", []))
    maker = join_names(iteminfo.get("maker", []))
    title = item.get("title", "")
    volume = item.get("volume", "")

    description = (
        f"{title}。ジャンル：{genres}。出演：{cast}。"
        f"シリーズ：{series}。メーカー：{maker}。"
        f"レーベル：{label}。収録時間：{volume}。"
    )
    return description if len(description) > 10 else "FANZA（DMM）VR作品の自動紹介です。"


def build_intro_text(item: dict) -> Optional[str]:
    """
    紹介文（あらすじ・コメント本文）を取得する。
    優先順位: スクレイピング（SCRAPE_DESC=1のとき） → API説明 → None

    メタ情報（ジャンル一覧等）は含めず、純粋なストーリー/紹介本文のみを返す。
    取得できなければ None を返す（→ 投稿時はメタ情報セクションのみになる）。
    """
    if Config.SCRAPE_DESC:
        scraped = scrape_description(item)
        if scraped:
            return scraped

    existing = _pick_existing_description(item)
    if existing:
        return existing

    return None


def build_metadata_text(item: dict) -> str:
    """ジャンル・出演・シリーズ・メーカー等の構造化メタ情報テキストを生成。"""
    return _compose_metadata_description(item)


# 後方互換のため旧名も残す
def fallback_description(item: dict) -> str:
    """[非推奨] 互換用。新コードでは build_intro_text + build_metadata_text を使用。"""
    intro = build_intro_text(item)
    return intro or build_metadata_text(item)


# ============================================================
# WordPress投稿
# ============================================================
def upload_image(wp: Client, url: str) -> Optional[dict]:
    """
    画像をWPメディアにアップロードし、レスポンス（id, url, file, type など）を返す。
    失敗時 None。
    """
    try:
        data = requests.get(url, timeout=GET_TIMEOUT).content
        name = os.path.basename(url.split("?")[0])
        return wp.call(media.UploadFile({
            "name": name,
            "type": "image/jpeg",
            "bits": xmlrpc_client.Binary(data),
        }))
    except Exception as e:
        print(f"[画像アップロード失敗] {url}: {e}")
        return None


def _upload_all_images(wp: Client, image_urls: list[str]) -> list[dict]:
    """画像URLリストをすべてWPにアップロード。失敗したものはスキップして残りを返す。"""
    uploaded: list[dict] = []
    for index, url in enumerate(image_urls, start=1):
        result = upload_image(wp, url)
        if result:
            print(f"  [画像 {index}/{len(image_urls)}] アップロード成功 → {result.get('url', '')}")
            uploaded.append(result)
        else:
            print(f"  [画像 {index}/{len(image_urls)}] アップロード失敗（スキップ）")
    return uploaded


def _is_already_posted(wp: Client, title: str) -> bool:
    """同じタイトルの公開済み投稿があるか確認。"""
    try:
        existing = wp.call(GetPosts({"post_status": "publish", "s": title}))
        return any(post.title == title for post in existing)
    except Exception as e:
        print(f"[既投稿チェック失敗] {e}")
        return False


def _extract_tags_from_item(item: dict) -> list[str]:
    """
    item.iteminfo の指定フィールド（Config.TAG_FIELDS）から
    タグ用の名前リストを抽出し、重複排除して返す。
    """
    iteminfo = item.get("iteminfo") or {}
    tags: list[str] = []
    seen: set[str] = set()

    for field in Config.TAG_FIELDS:
        entries = iteminfo.get(field, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                tags.append(name)

    # 上限カット
    if Config.MAX_TAGS > 0 and len(tags) > Config.MAX_TAGS:
        tags = tags[: Config.MAX_TAGS]
    return tags


# 既存WPカテゴリ名のセッション内キャッシュ
_wp_category_cache: Optional[set[str]] = None


def _get_wp_category_names(wp: Client) -> set[str]:
    """
    既存のWordPressカテゴリ名一覧を取得する。
    1回の実行内で複数投稿があっても、初回取得後はキャッシュを使用する。
    """
    global _wp_category_cache
    if _wp_category_cache is not None:
        return _wp_category_cache
    try:
        terms = wp.call(taxonomies.GetTerms("category"))
        names = {
            (getattr(t, "name", "") or "").strip()
            for t in terms
            if getattr(t, "name", None)
        }
        names.discard("")
        _wp_category_cache = names
        print(f"[WP] 既存カテゴリ {len(names)} 件を取得（キャッシュ済み）")
    except Exception as e:
        print(f"[WP] カテゴリ一覧取得失敗: {e}")
        _wp_category_cache = set()
    return _wp_category_cache


def _find_matching_categories(names: list[str], existing: set[str]) -> list[str]:
    """names のうち、existing（既存カテゴリ名集合）に含まれるものを順序保持で返す。"""
    seen: set[str] = set()
    matched: list[str] = []
    for name in names:
        if name in existing and name not in seen:
            seen.add(name)
            matched.append(name)
    return matched


def _build_post_content(
    title: str,
    affiliate_link: str,
    images: list[str],
    intro_text: Optional[str],
    metadata_text: str,
) -> str:
    """
    投稿本文HTMLを組み立てる。

    構成:
      1. メインビジュアル（アフィリエイトリンク付き）
      2. タイトルリンク
      3. 紹介文セクション（あれば）：あらすじ・コメント本文
      4. メタ情報セクション：ジャンル・出演・メーカー等
      5. ギャラリー画像（2枚目以降）
      6. タイトルリンク（再掲）
    """
    affiliate_tag_attrs = 'target="_blank" rel="nofollow noopener"'

    parts: list[str] = [
        f'<p><a href="{affiliate_link}" {affiliate_tag_attrs}>'
        f'<img src="{images[0]}" alt="{title}"></a></p>',
        f'<p><a href="{affiliate_link}" {affiliate_tag_attrs}>{title}</a></p>',
    ]

    # 紹介文（あらすじ・コメント）セクション
    if intro_text:
        parts.append(
            f'<div class="vr-intro"><h3>作品紹介</h3>'
            f'<p>{intro_text}</p></div>'
        )

    # メタ情報（ジャンル・出演者・メーカー等）セクション
    parts.append(
        f'<div class="vr-meta"><h3>作品情報</h3>'
        f'<p>{metadata_text}</p></div>'
    )

    # ギャラリー画像
    parts.extend(
        f'<p><img src="{img}" alt="{title}"></p>' for img in images[1:]
    )

    # 末尾にタイトルリンク再掲
    parts.append(
        f'<p><a href="{affiliate_link}" {affiliate_tag_attrs}>{title}</a></p>'
    )
    return "\n".join(parts)


def create_wp_post(item: dict, wp: Client, category: str, affiliate_id: str) -> bool:
    """1件のVR作品をWordPressに投稿。成功時 True。"""
    title = item.get("title", "").strip()

    # VR再チェック（保険）
    if not contains_vr(item):
        print(f"→ 非VRスキップ: {title}")
        return False

    # 重複投稿チェック
    if _is_already_posted(wp, title):
        print(f"→ 既投稿: {title}")
        return False

    # 画像チェック
    source_images = extract_sample_images(item)
    if not source_images:
        print(f"→ 画像なしスキップ: {title}")
        return False

    # 全画像をWPメディアにアップロード
    print(f"[画像アップロード開始] {title} （{len(source_images)}枚）")
    uploaded = _upload_all_images(wp, source_images)
    if not uploaded:
        print(f"→ 全画像アップロード失敗スキップ: {title}")
        return False

    # 1枚目をサムネイルに、本文では全画像をWP側のURLで参照
    thumbnail_id = uploaded[0]["id"]
    wp_image_urls = [u["url"] for u in uploaded if u.get("url")]

    # 本文組み立て（紹介文セクション ＋ メタ情報セクションを分けて生成）
    affiliate_link = make_affiliate_link(item["URL"], affiliate_id)
    intro_text = build_intro_text(item)
    metadata_text = build_metadata_text(item)
    content = _build_post_content(
        title, affiliate_link, wp_image_urls, intro_text, metadata_text
    )

    # タグ抽出（ジャンル等）
    tags = _extract_tags_from_item(item)

    # カテゴリ組み立て：メインカテゴリ ＋ タグ名と一致する既存カテゴリ
    categories: list[str] = [category]
    matched_categories: list[str] = []
    if Config.AUTO_MATCH_CATEGORY and tags:
        existing_cats = _get_wp_category_names(wp)
        matched_categories = _find_matching_categories(tags, existing_cats)
        for cat in matched_categories:
            if cat not in categories:
                categories.append(cat)

    # 投稿
    post = WordPressPost()
    post.title = title
    post.content = content
    if thumbnail_id:
        post.thumbnail = thumbnail_id
    terms_names: dict[str, list[str]] = {"category": categories}
    if tags:
        terms_names["post_tag"] = tags
    post.terms_names = terms_names
    post.post_status = "publish"
    wp.call(posts.NewPost(post))

    matched_label = (
        f", 一致カテゴリ {len(matched_categories)} 件" if matched_categories else ""
    )
    print(
        f"✔ 投稿完了: {title} "
        f"（画像 {len(uploaded)}/{len(source_images)} 枚, "
        f"タグ {len(tags)} 件{matched_label}）"
    )
    return True


# ============================================================
# メイン
# ============================================================
def main() -> None:
    print(f"[{now_jst()}] VR新着投稿開始（VR超厳密＋可用性チェック / 早期終了モード）")
    print(
        f"[設定] POST_LIMIT={Config.POST_LIMIT}, "
        f"HITS={Config.HITS}, MAX_PAGES={Config.MAX_PAGES}, "
        f"MAX_PAGES_FALLBACK={Config.MAX_PAGES_FALLBACK}, "
        f"FLOORS={Config.FLOORS}, "
        f"SCRAPE_DESC={Config.SCRAPE_DESC}, "
        f"EXCLUDE_PRE_RELEASE={Config.EXCLUDE_PRE_RELEASE}"
    )

    wp = Client(get_env("WP_URL"), get_env("WP_USER"), get_env("WP_PASS"))
    category = get_env("CATEGORY")
    affiliate_id = get_env("DMM_AFFILIATE_ID")

    posted = 0
    for item in iter_vr_available_items():
        if create_wp_post(item, wp, category, affiliate_id):
            posted += 1
            if posted >= Config.POST_LIMIT:
                print(f"[早期終了] POST_LIMIT={Config.POST_LIMIT} 件に到達")
                break

    print(f"投稿数: {posted}")
    print(f"[{now_jst()}] 終了")


if __name__ == "__main__":
    main()
