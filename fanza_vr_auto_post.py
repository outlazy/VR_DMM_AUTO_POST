#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）アフィリエイトAPIで VR 単品の新着を取得 → 発売済のみを日付降順で WordPress 投稿
・offset は 1 始まり（必須）に修正
・keyword=VR が 400/NG の時は keyword なしで自動フォールバック
・発売済のみ抽出して発売日降順に整列
・Secrets: WP_URL / WP_USER / WP_PASS / DMM_API_ID / DMM_AFFILIATE_ID / CATEGORY
・オプション環境変数: MAX_PAGES(既定6), HITS(既定30), POST_LIMIT(既定1)
"""

import os
import re
import json
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime

import pytz
import requests
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import media, posts
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client

DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"

# ===== 可変パラメータ（envで上書き可） =====
MAX_PAGES = int(os.environ.get("MAX_PAGES", "6"))   # 探索最大ページ数（1ページ=HITS件）
HITS      = int(os.environ.get("HITS", "30"))       # 1ページ取得件数
POST_LIMIT = int(os.environ.get("POST_LIMIT", "1")) # 1回の実行で投稿する最大件数
# =========================================

NG_DESCRIPTIONS = [
    "From here on, it will be an adult site",
    "18歳未満", "未成年", "18才未満",
    "アダルト商品を取り扱う", "成人向け", "アダルトサイト", "ご利用は18歳以上",
]

def now_jst():
    return datetime.now(pytz.timezone('Asia/Tokyo'))

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

def fetch_description_from_detail_page(url, item):
    """商品ページ <meta name=description> / JSON-LD の description を抽出。NGならAPI/自動生成にフォールバック。"""
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        html = r.text

        # 1) <meta name="description" content="...">
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if m:
            desc = m.group(1).strip()
            if is_valid_description(desc):
                return desc

        # 2) JSON-LD の "description"
        m_script = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        if m_script:
            try:
                jd = json.loads(m_script.group(1))
                desc = jd.get("description") or (jd.get("subjectOf") or {}).get("description", "")
                if is_valid_description(desc):
                    return desc.strip()
            except Exception:
                pass
    except Exception as e:
        print(f"商品ページ説明抽出失敗: {e}")

    # 3) APIデータでフォールバック
    ii = item.get("iteminfo", {}) or {}
    for key in ("description", "comment", "story"):
        val = item.get(key) or ii.get(key)
        if is_valid_description(val or ""):
            return val

    # 4) 自動生成
    cast = "、".join([a["name"] for a in ii.get("actress", []) if "name" in a])
    label = "、".join([l["name"] for l in ii.get("label", []) if "name" in l])
    genres = "、".join([g["name"] for g in ii.get("genre", []) if "name" in g])
    volume = item.get("volume", "")
    base = f"{item['title']}。ジャンル：{genres}。出演：{cast}。レーベル：{label}。収録時間：{volume}。"
    return base if len(base) > 10 else "FANZA（DMM）VR動画の自動投稿です。"

def contains_vr(item) -> bool:
    """ iteminfo->genre に VR 系タグが含まれるかを判定 """
    ii = item.get("iteminfo", {}) or {}
    names = [g.get("name", "") for g in ii.get("genre", []) if isinstance(g, dict)]
    joined = " ".join(names)
    return ("VR" in joined) or ("ＶＲ" in joined) or ("バーチャル" in joined)

def is_released(item) -> bool:
    """item['date'] が JST 現在時刻以下か判定（例: '2025-08-15 00:00:00'）"""
    ds = item.get("date")
    if not ds:
        return False
    try:
        jst = pytz.timezone('Asia/Tokyo')
        release_dt = jst.localize(datetime.strptime(ds, "%Y-%m-%d %H:%M:%S"))
        return release_dt <= now_jst()
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

def fetch_vr_released_items_sorted():
    """新着順ページを連結し、発売済みVRのみを日付降順で返す（keyword=VRがNGなら自動フォールバック）"""
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
            "offset": offset,    # ★ 1 始まりに注意
        }
        if use_keyword:
            p["keyword"] = "VR"   # 粗フィルタ（ダメなら外す）
        return p

    for page in range(MAX_PAGES):
        # ★ offset は 1, 1+HITS, 1+2*HITS, ...
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

    # 発売済み＋VR判定でフィルタ → 発売日降順（最新→古い）
    released = [it for it in all_items if contains_vr(it) and is_released(it)]
    released.sort(key=lambda x: x.get('date', ''), reverse=True)
    print(f"VR発売済み件数: {len(released)}（日付降順）")
    return released

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

def create_wp_post(item):
    WP_URL = get_env('WP_URL').strip()
    WP_USER = get_env('WP_USER')
    WP_PASS = get_env('WP_PASS')
    CATEGORY = get_env('CATEGORY')
    AFF_ID = get_env('DMM_AFFILIATE_ID')

    wp = Client(WP_URL, WP_USER, WP_PASS)

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

    # タグ抽出（レーベル・メーカー・女優・ジャンル）
    tags = set()
    ii = item.get("iteminfo", {}) or {}
    for key in ("label", "maker", "actress", "genre"):
        if key in ii and ii[key]:
            for v in ii[key]:
                if isinstance(v, dict) and "name" in v:
                    tags.add(v["name"])

    aff_link = make_affiliate_link(item["URL"], AFF_ID)

    # 本文
    desc = fetch_description_from_detail_page(item["URL"], item) or "FANZA（DMM）VR動画の自動投稿です。"
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
    post.terms_names = {"category": [CATEGORY], "post_tag": list(tags)}
    post.post_status = "publish"
    wp.call(posts.NewPost(post))
    print(f"✔ 投稿完了: {title}")
    return True

def main():
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始（発売済・降順）")
    try:
        items = fetch_vr_released_items_sorted()
        posted = 0
        for item in items:
            if create_wp_post(item):
                posted += 1
                if posted >= POST_LIMIT:
                    break
        if posted == 0:
            print("新規投稿なし（発売済が見つからない・VR該当なし・既投稿のみ等）")
    except Exception as e:
        print(f"エラー: {e}")
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿終了")

if __name__ == "__main__":
    main()
