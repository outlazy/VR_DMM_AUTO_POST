#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）アフィリエイトAPIで VR 単品の新着を取得 → WordPress 自動投稿（本文抽出つきフル版）

主な仕様
- 直近 RECENT_DAYS 日の "発売済" VR を優先し、不足分はバックログ（発売日降順）で補完
- DMM API: sort=date は未来が混ざるため、ローカルで発売日<=現在のものだけにフィルタ
- offset は 1 始まり（1, 1+HITS, ...）に対応
- keyword=VR 失敗時は keyword なしで自動フォールバック
- 説明文は商品ページの本文（紹介/ストーリー等）を最優先で抽出（SCRAPE_DESC=1 で有効）
  * 年齢認証文面を検知したら即フォールバック（回避はしない）
  * Cookie を環境変数（AGE_GATE_COOKIE）から付与可能（先生自身のクッキーのみ想定）
  * 本文セレクタ強化（.mg-b20.lh4 / .txt / #introduction など）
  * 取れない場合は og:description → meta description → JSON-LD → API説明 → 自動生成
- 既投稿はタイトル一致でスキップ
- Python3.10+ の collections.Iterable 問題に互換パッチ

必要 Secrets/Env（GitHub Actions 例）
  WP_URL / WP_USER / WP_PASS / DMM_API_ID / DMM_AFFILIATE_ID / CATEGORY
  POST_LIMIT=2 / RECENT_DAYS=3 / MAX_PAGES=6 / HITS=30 / SCRAPE_DESC=1
  AGE_GATE_COOKIE="ckcy=1; age_check_done=1"（任意・先生自身のクッキー）
  FORCE_DETAIL_DOMAIN=www（任意: www または video を優先）
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
AGE_GATE_COOKIE = os.environ.get("AGE_GATE_COOKIE", "").strip()  # 例: "ckcy=1; age_check_done=1"
FORCE_DETAIL_DOMAIN = os.environ.get("FORCE_DETAIL_DOMAIN", "").strip()  # "video" / "www"（任意）
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

# ------------------ フォールバック（API/自動生成） ------------------

def fallback_description(item):
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

# ------------------ 本文抽出（セレクタ強化） ------------------

def _clean_text(s: str) -> str:
    s = html.unescape(s or "").strip()
    s = re.sub(r"\s{2,}", " ", s)
    for b in ["18歳未満", "成人向け", "アダルトサイト", "ご利用は18歳以上", "年齢認証", "無修正"]:
        s = s.replace(b, "")
    return s.strip()


def extract_main_description(html_txt: str):
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

    # 1) DMMでよく見る本文ブロック（www側）
    # 例: <div class="mg-b20 lh4"> ... 説明 ... </div>
    for sel in [
        "div.mg-b20.lh4",          # 代表的な本文
        "div#introduction",        # ID版
        "section#introduction",
        "div.introduction",
        "section.introduction",
        '[data-contents="introduction"]',
        ".vbox .txt",               # VR系のテキストブロックで見かけることがある
        ".d-item__intro",          # 新デザイン仮
        "#performer + div",        # 出演者ブロック直後
    ]:
        for n in soup.select(sel):
            t = n.get_text(" ", strip=True)
            if t:
                candidates.append(t)

    # 2) 見出しが「作品紹介/内容/ストーリー/あらすじ/解説」
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        ht = (h.get_text(strip=True) or "")
        if any(k in ht for k in ["作品紹介", "作品内容", "ストーリー", "あらすじ", "解説"]):
            parts = []
            sib = h.find_next_sibling()
            while sib and sib.name not in ["h1", "h2", "h3", "h4"]:
                if sib.name in ["p", "div", "section"]:
                    t = sib.get_text(" ", strip=True)
                    if t:
                        parts.append(t)
                sib = sib.find_next_sibling()
            if parts:
                candidates.append("\n".join(parts))

    # 3) やや長めの段落を保険で
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
    return best

# ------------------ URL候補生成（www/video 両面） ------------------

def _build_candidate_urls(item, original_url: str):
    urls = []
    # 1) 元URLから affiliate 系を除去
    try:
        pu = urlparse(original_url)
        q = dict(parse_qsl(pu.query))
        for k in list(q.keys()):
            if k.lower() in {"affiliate_id", "affi_id", "uid", "af_id"}:
                q.pop(k, None)
        urls.append(urlunparse((pu.scheme, pu.netloc, pu.path, pu.params, urlencode(q), pu.fragment)))
    except Exception:
        urls.append(original_url)

    # 2) content_id / product_id から www 側 detail を推測
    cid = (item.get("content_id") or item.get("product_id") or "").strip()
    if cid:
        urls.extend([
            f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/digital/vrvideo/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/vrvideo/-/detail/=/cid={cid}/",
        ])

    # 3) www/video スワップ
    extra = []
    for u in list(urls):
        try:
            pu = urlparse(u)
            if pu.netloc.startswith("video."):
                extra.append(urlunparse((pu.scheme, "www." + pu.netloc.split(".",1)[1], pu.path, pu.params, pu.query, pu.fragment)))
            elif pu.netloc.startswith("www."):
                # www→video 側のパス差分を一部ケア
                extra.append(urlunparse((pu.scheme, "video." + pu.netloc.split(".",1)[1], pu.path.replace("/digital/", "/av/"), pu.params, pu.query, pu.fragment)))
        except Exception:
            pass
    urls.extend(extra)

    # 4) 優先ドメイン指定
    if FORCE_DETAIL_DOMAIN in ("video", "www"):
        pref = "video." if FORCE_DETAIL_DOMAIN == "video" else "www."
        urls.sort(key=lambda x: 0 if urlparse(x).netloc.startswith(pref) else 1)

    # 重複除去
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# ------------------ 説明文抽出（本文→メタ→JSONLD→フォールバック） ------------------

def fetch_description_from_detail_page(url, item):
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

    def pick_from_html(html_txt: str):
        age_gate_markers = [
            "18歳未満", "未満の方のアクセス", "成人向け", "アダルトサイト",
            "under the age of 18", "age verification"
        ]
        if any(k in html_txt for k in age_gate_markers):
            return None, "age-gate"

        # 1) 本文ブロック
        main_desc = extract_main_description(html_txt)
        if main_desc and is_valid_description(main_desc):
            return main_desc, "main"

        # 2) og:description
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html_txt, re.I)
        if m:
            d = _clean_text(m.group(1))
            if 30 <= len(d) <= 700 and is_valid_description(d):
                return d, "og"

        # 3) meta name=description
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html_txt, re.I)
        if m:
            d = _clean_text(m.group(1))
            if 30 <= len(d) <= 700 and is_valid_description(d):
                return d, "meta"

        # 4) JSON-LD
        for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html_txt, re.S | re.I):
            raw = m.group(1).strip()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            arr = data if isinstance(data, list) else [data]
            for jd in arr:
                if isinstance(jd, dict):
                    if "description" in jd:
                        d = _clean_text(str(jd["description"]))
                        if 30 <= len(d) <= 700 and is_valid_description(d):
                            return d, "jsonld"
                    sub = jd.get("subjectOf")
                    if isinstance(sub, dict) and "description" in sub:
                        d = _clean_text(str(sub["description"]))
                        if 30 <= len(d) <= 700 and is_valid_description(d):
                            return d, "jsonld.subject"
        return None, None

    last_err = None
    for i, u in enumerate(_build_candidate_urls(item, url), 1):
        try:
            resp = requests.get(u, headers=headers, timeout=12, allow_redirects=True)
            html_bytes = resp.content  # ← 重要: bytesで受ける（文字化け回避）
         try:
    soup = BeautifulSoup(html_bytes, "lxml")
except Exception:
    soup = BeautifulSoup(html_bytes, "html.parser")
html_txt = str(soup)  # 正規化したHTML文字列にして既存ロジックへ
            desc, src = pick_from_html(html_txt)
            if desc:
                print(f"説明抽出: {src} / {u}")
                return desc
            if src == "age-gate":
                print(f"年齢認証検知: {u} → 他候補 or フォールバック")
        except Exception as e:
            last_err = e
            print(f"説明抽出失敗({i}): {u} ({e})")
            time.sleep(0.2)

    if last_err:
        print(f"説明抽出最終エラー: {last_err}")
    return fallback_description(item)

# ------------------ VR判定・発売済み判定 ------------------

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

# ------------------ DMM API 呼び出し ------------------

def dmm_request(params):
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
    API_ID = get_env("DMM_API_ID")
    AFF_ID = get_env("DMM_AFFILIATE_ID")
    all_items = []

    def base_params(offset, use_keyword=True):
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

def split_recent_and_backlog(items):
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

# ------------------ メイン ------------------

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
