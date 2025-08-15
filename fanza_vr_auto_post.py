#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿（本文抽出・Cookie対応・www優先・404除外）

ポイント（今回の修正）
- `FORCE_DETAIL_DOMAIN` の既定を `www` に。`video` 側で404を踏むケース（savr系など）を回避
- 候補URLに `https://www.dmm.co.jp/av/-/detail/=/cid=.../` と `https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=.../` を追加
- 詳細取得時に **HTTP 404** を検知したら即座に次候補へ（テキスト判定前にスキップ）
- レスポンスは **bytes→BeautifulSoup** でパースし、文字化けやmeta不一致を吸収
- 年齢認証ページは検知してフォールバック（回避はしない）

必要 Secrets/Env
  WP_URL / WP_USER / WP_PASS / DMM_API_ID / DMM_AFFILIATE_ID / CATEGORY
任意 Env
  POST_LIMIT=2 / RECENT_DAYS=3 / MAX_PAGES=6 / HITS=30 / SCRAPE_DESC=1
  AGE_GATE_COOKIE="ckcy=1; age_check_done=1"（自分の年齢同意Cookie）
  FORCE_DETAIL_DOMAIN=www（www優先。videoにしたいときだけ変更）
"""

import os
import re
import json
import html
import time
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime, timedelta

# ---- Py3.10+ 互換パッチ（古いライブラリの collections.* 参照対策）----
import collections as _collections
import collections.abc as _abc
for _name in ("Iterable", "Mapping", "MutableMapping", "Sequence"):
    if not hasattr(_collections, _name) and hasattr(_abc, _name):
        setattr(_collections, _name, getattr(_abc, _name))
# ---------------------------------------------------------------------

import pytz
import requests
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import media, posts
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client

# ▼ HTMLパース（beautifulsoup4 / lxml 推奨）
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"

# ===== 可変パラメータ（envで上書き可） =====
MAX_PAGES   = int(os.environ.get("MAX_PAGES", "6"))   # 探索最大ページ数（1ページ=HITS件）
HITS        = int(os.environ.get("HITS", "30"))       # 1ページ取得件数
POST_LIMIT  = int(os.environ.get("POST_LIMIT", "2"))  # 1回の実行で投稿する最大件数
RECENT_DAYS = int(os.environ.get("RECENT_DAYS", "3")) # 直近何日を“新作”とみなすか
SCRAPE_DESC = os.environ.get("SCRAPE_DESC", "1") == "1"  # 1=本文抽出有効
AGE_GATE_COOKIE = os.environ.get("AGE_GATE_COOKIE", "").strip()  # 例: "ckcy=1; age_check_done=1"
FORCE_DETAIL_DOMAIN = os.environ.get("FORCE_DETAIL_DOMAIN", "www").strip()  # 既定を www
# =========================================

NG_DESCRIPTIONS = [
    "From here on, it will be an adult site",
    "18歳未満", "未成年", "18才未満",
    "アダルト商品を取り扱う", "成人向け", "アダルトサイト", "ご利用は18歳以上",
]

AGE_MARKERS = [
    "18歳未満", "未満の方のアクセス", "成人向け", "アダルトサイト",
    "under the age of 18", "age verification"
]

# ------------------ 共通ユーティリティ ------------------

def now_jst() -> datetime:
    return datetime.now(pytz.timezone('Asia/Tokyo'))


def parse_jst_date(s: str) -> datetime:
    jst = pytz.timezone('Asia/Tokyo')
    return jst.localize(datetime.strptime(s, "%Y-%m-%d %H:%M:%S"))


def get_env(key: str, required: bool = True, default=None):
    v = os.environ.get(key, default)
    if required and not v:
        raise RuntimeError(f"環境変数 {key} が設定されていません")
    return v


def make_affiliate_link(url: str, aff_id: str) -> str:
    parsed = urlparse(url)
    qs = dict(parse_qsl(parsed.query))
    qs["affiliate_id"] = aff_id
    new_query = urlencode(qs)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def is_valid_description(desc: str) -> bool:
    if not desc or len(desc.strip()) < 30:
        return False
    for ng in NG_DESCRIPTIONS:
        if ng in desc:
            return False
    return True

# ------------------ フォールバック（API/自動生成） ------------------

def fallback_description(item: dict) -> str:
    ii = item.get("iteminfo", {}) or {}
    for key in ("description", "comment", "story"):
        val = (item.get(key) or ii.get(key) or "").strip()
        if 20 <= len(val) <= 800 and is_valid_description(val):
            return val
    cast = "、".join([a.get("name", "") for a in ii.get("actress", []) if isinstance(a, dict)])
    label = "、".join([l.get("name", "") for l in ii.get("label", []) if isinstance(l, dict)])
    genres = "、".join([g.get("name", "") for g in ii.get("genre", []) if isinstance(g, dict)])
    volume = item.get("volume", "")
    title = item.get("title", "")
    base = f"{title}。ジャンル：{genres}。出演：{cast}。レーベル：{label}。収録時間：{volume}。"
    return base if len(base) > 10 else "FANZA（DMM）VR動画の自動投稿です。"

# ------------------ 本文抽出（セレクタ強化＋bytes→Soup） ------------------

def _clean_text(s: str) -> str:
    s = html.unescape(s or "").strip()
    s = re.sub(r"\s{2,}", " ", s)
    for b in ["18歳未満", "成人向け", "アダルトサイト", "ご利用は18歳以上", "年齢認証", "無修正"]:
        s = s.replace(b, "")
    return s.strip()


def extract_main_description_from_html_bytes(html_bytes: bytes) -> str | None:
    if not SCRAPE_DESC or not BeautifulSoup or not html_bytes:
        return None
    try:
        try:
            soup = BeautifulSoup(html_bytes, "lxml")
        except Exception:
            soup = BeautifulSoup(html_bytes, "html.parser")
    except Exception:
        return None

    # 年齢認証/404っぽい判定（軽量）
    raw_text = soup.get_text(" ", strip=True)
    if any(k in raw_text for k in AGE_MARKERS):
        return None

    candidates: list[str] = []

    # 1) DMMでよく見る本文ブロック
    for sel in [
        "div.mg-b20.lh4",
        "div#introduction", "section#introduction", "div.introduction", "section.introduction",
        "[data-contents='introduction']",
        ".vbox .txt", ".d-item__intro", "#performer + div",
        ".txt",
    ]:
        for n in soup.select(sel):
            t = n.get_text("\n", strip=True)
            if t:
                candidates.append(t)

    # 2) 見出し→直後の段落群
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        ht = (h.get_text(strip=True) or "")
        if any(k in ht for k in ["作品紹介", "作品内容", "ストーリー", "あらすじ", "解説"]):
            parts: list[str] = []
            sib = h.find_next_sibling()
            while sib and sib.name not in ["h1", "h2", "h3", "h4"]:
                if sib.name in ["p", "div", "section"]:
                    t = sib.get_text(" ", strip=True)
                    if t:
                        parts.append(t)
                sib = sib.find_next_sibling()
            if parts:
                candidates.append("\n".join(parts))

    # 3) 長めの段落（保険）
    for p in soup.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t and len(t) >= 60:
            candidates.append(t)

    def ok(s: str) -> bool:
        s = s.strip()
        if not (60 <= len(s) <= 1200):
            return False
        for ng in ["利用規約", "Cookie", "会員登録", "プライバシー"]:
            if ng in s:
                return False
        return True

    best = None
    best_score = -1
    for c in candidates:
        c2 = _clean_text(c)
        if not ok(c2):
            continue
        score = len(c2) + 20 * (c2.count("。") + c2.count("！") + c2.count("？"))
        if score > best_score:
            best = c2
            best_score = score
    if best:
        return best

    # 4) og:description / meta description / JSON-LD
    m = soup.select_one('meta[property="og:description"]')
    if m and m.get("content"):
        d = _clean_text(m["content"])
        if 30 <= len(d) <= 700 and is_valid_description(d):
            return d

    m = soup.select_one('meta[name="description"]')
    if m and m.get("content"):
        d = _clean_text(m["content"])
        if 30 <= len(d) <= 700 and is_valid_description(d):
            return d

    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.get_text(strip=True))
        except Exception:
            continue
        arr = data if isinstance(data, list) else [data]
        for jd in arr:
            if isinstance(jd, dict) and "description" in jd:
                d = _clean_text(str(jd["description"]))
                if 30 <= len(d) <= 700 and is_valid_description(d):
                    return d
    return None

# ------------------ URL候補生成（www/video 両面） ------------------

def _strip_affiliate_params(u: str) -> str:
    try:
        pu = urlparse(u)
        q = dict(parse_qsl(pu.query))
        for k in list(q.keys()):
            if k.lower() in {"affiliate_id", "affi_id", "uid", "af_id"}:
                q.pop(k, None)
        return urlunparse((pu.scheme, pu.netloc, pu.path, pu.params, urlencode(q), pu.fragment))
    except Exception:
        return u


def _extract_cid(u: str) -> str:
    m = re.search(r"(?:cid|id)=([a-z0-9_]+)", u)
    return m.group(1) if m else ""


def _build_candidate_urls(item: dict, original_url: str) -> list[str]:
    urls: list[str] = []
    base = _strip_affiliate_params(original_url)
    urls.append(base)

    cid = (item.get("content_id") or item.get("product_id") or _extract_cid(base) or "").strip()
    if cid:
        urls.extend([
            f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/digital/vrvideo/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/vrvideo/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/av/-/detail/=/cid={cid}/",       # 追加
            f"https://www.dmm.co.jp/mono/dvd/-/detail/=/cid={cid}/", # 追加（保険）
            f"https://video.dmm.co.jp/av/content/?id={cid}",
        ])

    extra: list[str] = []
    for u in list(urls):
        try:
            pu = urlparse(u)
            if pu.netloc.startswith("video."):
                extra.append(urlunparse((pu.scheme, "www." + pu.netloc.split(".",1)[1], pu.path, pu.params, pu.query, pu.fragment)))
            elif pu.netloc.startswith("www."):
                extra.append(urlunparse((pu.scheme, "video." + pu.netloc.split(".",1)[1], pu.path.replace("/digital/", "/av/"), pu.params, pu.query, pu.fragment)))
        except Exception:
            pass
    urls.extend(extra)

    if FORCE_DETAIL_DOMAIN in ("video", "www"):
        pref = "video." if FORCE_DETAIL_DOMAIN == "video" else "www."
        urls.sort(key=lambda x: 0 if urlparse(x).netloc.startswith(pref) else 1)

    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# ------------------ 説明文抽出（本文→メタ→JSONLD→フォールバック） ------------------

def fetch_description_from_detail_page(url: str, item: dict) -> str:
    if not SCRAPE_DESC:
        return fallback_description(item)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://video.dmm.co.jp/",
    }
    if AGE_GATE_COOKIE:
        headers["Cookie"] = AGE_GATE_COOKIE

    last_err = None
    candidates = _build_candidate_urls(item, url)

    for i, u in enumerate(candidates, 1):
        try:
            resp = requests.get(u, headers=headers, timeout=12, allow_redirects=True)
            # 404 は即スキップ（video側で多発するため）
            if resp.status_code == 404:
                print(f"説明抽出: 404 / {u}")
                continue
            html_bytes = resp.content  # bytesで受ける
            desc = extract_main_description_from_html_bytes(html_bytes)
            if desc and is_valid_description(desc):
                print(f"説明抽出: OK / {u}")
                return desc
            # 年齢認証/未検出の場合は次候補へ
        except Exception as e:
            last_err = e
            print(f"説明抽出失敗({i}/{len(candidates)}): {u} ({e})")
            time.sleep(0.2)

    if last_err:
        print(f"説明抽出最終エラー: {last_err}")
    return fallback_description(item)

# ------------------ VR判定・発売済み判定 ------------------

def contains_vr(item: dict) -> bool:
    ii = item.get("iteminfo", {}) or {}
    names = [g.get("name", "") for g in ii.get("genre", []) if isinstance(g, dict)]
    joined = " ".join(names)
    return ("VR" in joined) or ("ＶＲ" in joined) or ("バーチャル" in joined)


def is_released(item: dict) -> bool:
    ds = item.get("date")
    if not ds:
        return False
    try:
        return parse_jst_date(ds) <= now_jst()
    except Exception:
        return False

# ------------------ DMM API 呼び出し ------------------

def dmm_request(params: dict) -> dict:
    r = requests.get(DMM_API_URL, params=params, timeout=12)
    if r.status_code != 200:
        try:
            print("---- DMM API Error ----")
            print(r.text[:2000])
            print("-----------------------")
        finally:
            r.raise_for_status()
    data = r.json()
    res = data.get("result", {})
    if isinstance(res, dict) and res.get("status") == "NG":
        msg = res.get("message") or res.get("error", "")
        raise RuntimeError(f"DMM API NG: {msg}")
    return res


def fetch_all_vr_released_sorted() -> list[dict]:
    API_ID = get_env("DMM_API_ID")
    AFF_ID = get_env("DMM_AFFILIATE_ID")
    all_items: list[dict] = []

    def base_params(offset: int, use_keyword: bool = True) -> dict:
        p = {
            "api_id": API_ID,
            "affiliate_id": AFF_ID,
            "site": "FANZA",
            "service": "digital",
            "floor": "videoa",   # VR単品
            "sort": "date",
            "output": "json",
            "hits": HITS,
            "offset": offset,    # 1, 1+HITS, ...
        }
        if use_keyword:
            p["keyword"] = "VR"
        return p

    for page in range(MAX_PAGES):
        offset = 1 + page * HITS
        print(f"[page {page+1}] fetch (offset={offset}) with keyword=VR")
        try:
            res = dmm_request(base_params(offset, use_keyword=True))
            items = res.get("items", []) or []
        except Exception as e:
            print(f"keyword=VR で失敗: {e} → keywordなしで再試行")
            try:
                res = dmm_request(base_params(offset, use_keyword=False))
                items = res.get("items", []) or []
            except Exception as e2:
                print(f"keywordなしでも失敗: {e2} → これ以上このページは進めません")
                break
        print(f"取得件数: {len(items)}")
        if not items:
            break
        all_items.extend(items)

    released = [it for it in all_items if contains_vr(it) and is_released(it)]
    released.sort(key=lambda x: x.get('date', ''), reverse=True)
    print(f"VR発売済み件数: {len(released)}（日付降順）")
    return released

# ------------------ 分割（直近/バックログ） ------------------

def split_recent_and_backlog(items: list[dict]) -> tuple[list[dict], list[dict]]:
    boundary = now_jst() - timedelta(days=RECENT_DAYS)
    recent, backlog = [], []
    for it in items:
        try:
            dt = parse_jst_date(it["date"])
        except Exception:
            backlog.append(it)
            continue
        if dt >= boundary:
            recent.append(it)
        else:
            backlog.append(it)
    return recent, backlog

# ------------------ メディア/投稿 ------------------

def upload_image(wp: Client, url: str):
    try:
        data = requests.get(url, timeout=12).content
        name = os.path.basename(url.split("?")[0])
        media_data = {"name": name, "type": "image/jpeg", "bits": xmlrpc_client.Binary(data)}
        res = wp.call(media.UploadFile(media_data))
        return res.get("id")
    except Exception as e:
        print(f"画像アップロード失敗: {url} ({e})")
        return None


def create_wp_post(item: dict, wp: Client, category: str, aff_id: str) -> bool:
    title = item.get("title", "")

    # 既投稿チェック（タイトル一致）
    existing = wp.call(GetPosts({"post_status": "publish", "s": title}))
    if any(p.title == title for p in existing):
        print(f"→ 既投稿: {title}（スキップ）")
        return False

    # 画像
    images: list[str] = []
    siu = item.get("sampleImageURL", {}) or {}
    if "sample_l" in siu and "image" in siu["sample_l"]:
        images = siu["sample_l"]["image"]
    elif "sample_s" in siu and "image" in siu["sample_s"]:
        images = siu["sample_s"]["image"]
    if not images:
        print(f"→ サンプル画像なし: {title}（スキップ）")
        return False
    thumb_id = upload_image(wp, images[0]) if images else None

    # タグ
    tags: set[str] = set()
    ii = item.get("iteminfo", {}) or {}
    for key in ("label", "maker", "actress", "genre"):
        if key in ii and ii[key]:
            for v in ii[key]:
                if isinstance(v, dict) and "name" in v:
                    tags.add(v["name"])

    aff_link = make_affiliate_link(item["URL"], aff_id)
    desc = fetch_description_from_detail_page(item["URL"], item)

    parts: list[str] = []
    parts.append(f'<p><a href="{aff_link}" target="_blank"><img src="{images[0]}" alt="{title}"></a></p>')
    parts.append(f'<p><a href="{aff_link}" target="_blank">{title}</a></p>')
    parts.append(f'<div>{desc}</div>')
    for img in images[1:]:
        parts.append(f'<p><img src="{img}" alt="{title}"></p>')
    parts.append(f'<p><a href="{aff_link}" target="_blank"><img src="{images[0]}" alt="{title}"></a></p>')
    parts.append(f'<p><a href="{aff_link}" target="_blank">{title}</a></p>')

    post = WordPressPost()
    post.title = title
    post.content = "\n".join(parts)
    if thumb_id:
        post.thumbnail = thumb_id
    post.terms_names = {"category": [category], "post_tag": list(tags)}
    post.post_status = "publish"
    wp.call(posts.NewPost(post))
    print(f"✔ 投稿完了: {title}")
    return True

# ------------------ メイン ------------------

def main():
    jst_now = now_jst()
    print(f"[{jst_now.strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始（www優先／直近{RECENT_DAYS}日→バックログ／SCRAPE_DESC={'ON' if SCRAPE_DESC else 'OFF'}）")
    try:
        WP_URL = get_env('WP_URL').strip()
        WP_USER = get_env('WP_USER')
        WP_PASS = get_env('WP_PASS')
        CATEGORY = get_env('CATEGORY')
        AFF_ID = get_env('DMM_AFFILIATE_ID')
        wp = Client(WP_URL, WP_USER, WP_PASS)

        all_released = fetch_all_vr_released_sorted()
        recent, backlog = split_recent_and_backlog(all_released)
        print(f"直近{RECENT_DAYS}日: {len(recent)} / バックログ: {len(backlog)}")

        posted = 0
        for item in recent:
            if create_wp_post(item, wp, CATEGORY, AFF_ID):
                posted += 1
                if posted >= POST_LIMIT:
                    break
        if posted < POST_LIMIT:
            for item in backlog:
                if create_wp_post(item, wp, CATEGORY, AFF_ID):
                    posted += 1
                    if posted >= POST_LIMIT:
                        break

        if posted == 0:
            print("新規投稿なし（該当なし or 既投稿のみ）")
        else:
            print(f"合計投稿数: {posted}")
    except Exception as e:
        print(f"エラー: {e}")
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿終了")


if __name__ == "__main__":
    main()
