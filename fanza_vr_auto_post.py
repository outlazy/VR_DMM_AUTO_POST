#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）アフィリエイトAPIで VR 単品の新着を取得 → WordPress 自動投稿

◎仕様まとめ
- 直近RECENT_DAYS日以内の「発売済VR」を優先投稿。足りなければバックログ（発売済の過去作/新しい順）で補完
- APIは新着(sort=date)に未来日が混ざるので、ローカルで「発売済のみ」を抽出して発売日降順に整列
- DMM APIの offset は 1 始まり（1, 1+HITS, ...）に対応
- keyword=VR で 400/NG の場合は keyword なしに自動フォールバック
- 説明文は「商品ページ本文（イントロ）」を最優先で抽出（SCRAPE_DESC=1で有効・既定ON）
  - #introduction / .introduction / 見出し「作品紹介/内容/ストーリー/あらすじ/解説」の直下段落 等を総当り
  - 失敗時は og:description → meta description → JSON-LD → API説明 → 自動生成の順にフォールバック
  - 年齢認証ページ文面を検知したら即フォールバック（回避はしない）
- Python 3.10+ の collections.* 廃止対応モンキーパッチ（古いライブラリ対策）
- 既投稿はタイトル一致でスキップ（強化したければ content_id 埋め込み検知も拡張可）

◎必要な Secrets（GitHub Actions）
  WP_URL / WP_USER / WP_PASS / DMM_API_ID / DMM_AFFILIATE_ID / CATEGORY
◎オプション env（未設定なら右の既定値）
  MAX_PAGES=6, HITS=30, POST_LIMIT=2, RECENT_DAYS=3, SCRAPE_DESC=1
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
SCRAPE_DESC = os.environ.get("SCRAPE_DESC", "1") == "1"  # 1=商品ページ本文優先で抽出, 0=完全無効
# =========================================

NG_DESCRIPTIONS = [
    "From here on, it will be an adult site",
    "18歳未満", "未成年", "18才未満",
    "アダルト商品を取り扱う", "成人向け", "アダルトサイト", "ご利用は18歳以上",
]

def now_jst():
    return datetime.now(pytz.timezone('Asia/Tokyo'))

def parse_jst_date(s: str):
    jst = pytz.timezone('Asia/Tokyo')
    return jst.localize(datetime.strptime(s, "%Y-%m-%d %H:%M:%S"))

def get_env(key, required=True, default=None):
    v = os.environ.get(key, default)
    if required and not v:
        raise RuntimeError(f"環境変数 {key} が設定されていません")
    return v

def make_affiliate_link(url, aff_id):
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

# ---------- 説明文フォールバック ----------
def fallback_description(item):
    """APIの説明 or 自動生成。年齢認証ページ検出時やスクレイピング無効時もここに来る。"""
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

# ---------- 本文ブロック抽出 ----------
def extract_main_description(html_txt: str):
    """商品ページの本文（紹介/ストーリー等）から説明文を抽出。失敗時は None。"""
    if not SCRAPE_DESC or not BeautifulSoup or not html_txt:
        return None
    try:
        try:
            soup = BeautifulSoup(html_txt, "lxml")
        except Exception:
            soup = BeautifulSoup(html_txt, "html.parser")
    except Exception:
        return None

    candidates = []

    # 1) よくあるID/クラス
    for sel in [
        "#introduction", "section#introduction", "div#introduction",
        ".introduction", "section.introduction", '[data-contents="introduction"]',
        "#performer + div",
    ]:
        for n in soup.select(sel):
            txt = n.get_text(" ", strip=True)
            if txt:
                candidates.append(txt)

    # 2) 見出し「作品紹介/内容/ストーリー/あらすじ/解説」の直後の段落群
    for h in soup.find_all(["h2", "h3", "h4"]):
        ht = (h.get_text(strip=True) or "")
        if any(k in ht for k in ["作品紹介", "作品内容", "ストーリー", "あらすじ", "解説"]):
            parts = []
            sib = h.find_next_sibling()
            while sib and sib.name not in ["h2", "h3", "h4"]:
                if sib.name in ["p", "div", "section"]:
                    t = sib.get_text(" ", strip=True)
                    if t:
                        parts.append(t)
                sib = sib.find_next_sibling()
            if parts:
                candidates.append("\n".join(parts))

    # 3) やや長めの段落も保険で収集
    for p in soup.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t and len(t) >= 60:
            candidates.append(t)

    def clean(s: str) -> str:
        s = html.unescape(s or "").strip()
        s = re.sub(r"\s{2,}", " ", s)
        for b in ["18歳未満", "成人向け", "アダルトサイト", "ご利用は18歳以上", "年齢認証", "無修正"]:
            s = s.replace(b, "")
        return s.strip()

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
        c2 = clean(c)
        if not ok(c2):
            continue
        score = len(c2) + 20 * (c2.count("。") + c2.count("！") + c2.count("？"))
        if score > best_score:
            best = c2
            best_score = score
    return best

# ---------- 説明文抽出メイン ----------
def fetch_description_from_detail_page(url, item):
    """商品ページから説明文を抽出。年齢認証ページやNG時は即フォールバック。"""
    if not SCRAPE_DESC:
        return fallback_description(item)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://video.dmm.co.jp/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        html_txt = r.text

        # 年齢認証ページっぽい文言を検知したら即フォールバック（回避はしない）
        age_gate_markers = [
            "18歳未満", "未満の方のアクセス", "成人向け", "アダルトサイト",
            "under the age of 18", "age verification"
        ]
        if any(k in html_txt for k in age_gate_markers):
            return fallback_description(item)

        # 1) 本文ブロック優先
        main_desc = extract_main_description(html_txt)
        if main_desc and is_valid_description(main_desc):
            return main_desc

        # 2) og:description
        def clean(s: str) -> str:
            s = html.unescape(s).strip()
            for b in ["アダルトサイト", "18歳未満", "成人向け", "From here on", "ご利用は18歳以上"]:
                s = s.replace(b, "")
            s = re.sub(r"\s{2,}", " ", s)
            return s.strip()

        def ok(s: str) -> bool:
            return bool(s) and 30 <= len(s.strip()) <= 700 and is_valid_description(s)

        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html_txt, re.I)
        if m:
            desc = clean(m.group(1))
            if ok(desc):
                return desc

        # 3) meta name=description
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html_txt, re.I)
        if m:
            desc = clean(m.group(1))
            if ok(desc):
                return desc

        # 4) JSON-LD 総当り
        for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_txt, re.S | re.I):
            raw = m.group(1).strip()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for jd in candidates:
                if isinstance(jd, dict):
                    if "description" in jd:
                        desc = clean(str(jd["description"]))
                        if ok(desc):
                            return desc
                    sub = jd.get("subjectOf")
                    if isinstance(sub, dict) and "description" in sub:
                        desc = clean(str(sub["description"]))
                        if ok(desc):
                            return desc

    except Exception as e:
        print(f"商品ページ説明抽出失敗: {e}")
        time.sleep(0.2)  # 軽いバックオフ

    # 5) 最後はAPI系/自動生成へ
    return fallback_description(item)

# ---------- 判定 ----------
def contains_vr(item) -> bool:
    ii = item.get("iteminfo", {}) or {}
    names = [g.get("name", "") for g in ii.get("genre", []) if isinstance(g, dict)]
    joined = " ".join(names)
    return ("VR" in joined) or ("ＶＲ" in joined) or ("バーチャル" in joined)

def is_released(item) -> bool:
    ds = item.get("date")
    if not ds:
        return False
    try:
        return parse_jst_date(ds) <= now_jst()
    except Exception:
        return False

# ---- DMM共通呼び出し（詳細ログ & NG判定つき）----

def dmm_request(params):
    """DMM APIを叩いて、HTTPエラー時は本文を出しつつ例外、result.status=NGも例外化"""
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


def fetch_all_vr_released_sorted():
    """新着順ページを連結し、発売済みVRのみを発売日降順で返す（keyword=VRがNGなら自動フォールバック）"""
    API_ID = get_env("DMM_API_ID")
    AFF_ID = get_env("DMM_AFFILIATE_ID")
    all_items = []

    def base_params(offset, use_keyword=True):
        p = {
            "api_id": API_ID,
            "affiliate_id": AFF_ID,
            "site": "FANZA",
            "service": "digital",
            "floor": "videoa",   # VR単品はここに載る
            "sort": "date",      # 新着順（未来含む）
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


def split_recent_and_backlog(items):
    """直近RECENT_DAYS以内と、それ以外（バックログ）に分割"""
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


def upload_image(wp, url):
    try:
        data = requests.get(url, timeout=12).content
        name = os.path.basename(url.split("?")[0])
        media_data = {"name": name, "type": "image/jpeg", "bits": xmlrpc_client.Binary(data)}
        res = wp.call(media.UploadFile(media_data))
        return res.get("id")
    except Exception as e:
        print(f"画像アップロード失敗: {url} ({e})")
        return None


def create_wp_post(item, wp, category, aff_id):
    title = item["title"]

    # 既投稿チェック（タイトル一致）
    existing = wp.call(GetPosts({"post_status": "publish", "s": title}))
    if any(p.title == title for p in existing):
        print(f"→ 既投稿: {title}（スキップ）")
        return False

    # 画像
    images = []
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
    tags = set()
    ii = item.get("iteminfo", {}) or {}
    for key in ("label", "maker", "actress", "genre"):
        if key in ii and ii[key]:
            for v in ii[key]:
                if isinstance(v, dict) and "name" in v:
                    tags.add(v["name"])

    aff_link = make_affiliate_link(item["URL"], aff_id)
    desc = fetch_description_from_detail_page(item["URL"], item)

    parts = []
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


def main():
    jst_now = now_jst()
    print(f"[{jst_now.strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始（優先: 直近{RECENT_DAYS}日／不足分はバックログ／SCRAPE_DESC={'ON' if SCRAPE_DESC else 'OFF'}）")
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
