#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）アフィリエイトAPIで VR 単品（floor=videoa, media_type=vr相当）新着を取得→WordPress投稿
・日本時間（JST）で動作
・一覧は新着順（sort=date）で取得し、発売日時 <= 現在（JST）のみ投稿
・VR判定は iteminfo->genre に "VR/ＶＲ/バーチャル" を含むかでフィルタ（念のため keyword=VR も併用）
・本文は商品ページの description を抽出（NG文は除外）、サンプル画像を貼付
・環境変数（Secrets）:
  WP_URL / WP_USER / WP_PASS / DMM_API_ID / DMM_AFFILIATE_ID / CATEGORY
"""

import os
import re
import json
import socket
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime

import pytz
import requests
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import media, posts
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client

DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"

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
        # 形式不明なら通す（APIが新形式のときに過剰スキップしないため）
        return True

def fetch_vr_items(max_hits=30):
    """videoa（アダルトビデオ）フロアから新着を取得し、VR系のみ返す"""
    API_ID = get_env("DMM_API_ID")
    AFF_ID = get_env("DMM_AFFILIATE_ID")
    params = {
        "api_id": API_ID,
        "affiliate_id": AFF_ID,
        "site": "FANZA",
        "service": "digital",
        "floor": "videoa",      # 単品AV。VR単品は基本ここに出る
        "sort": "date",         # 新着順
        "output": "json",
        "hits": max_hits,
        "keyword": "VR",        # 粗フィルタ（最終判定は contains_vr で）
    }
    r = requests.get(DMM_API_URL, params=params, timeout=12)
    try:
        r.raise_for_status()
    except Exception:
        print("---- DMM API Error ----")
        print(r.text)
        print("-----------------------")
        raise
    items = r.json().get("result", {}).get("items", [])
    print(f"API取得件数: {len(items)}")
    vr_items = []
    for it in items:
        # デバッグ：必要ならサンプル画像配列を出す
        siu = it.get("sampleImageURL", {}) or {}
        if "sample_l" in siu and "image" in siu["sample_l"]:
            print("sample_l images:", siu["sample_l"]["image"])
        if "sample_s" in siu and "image" in siu["sample_s"]:
            print("sample_s images:", siu["sample_s"]["image"])

        if not contains_vr(it):
            continue
        vr_items.append(it)
    print(f"VR判定後件数: {len(vr_items)}")
    return vr_items

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

    # --- Preflight: DNS & reachability ---
    host = urlparse(WP_URL).hostname
    try:
        ip = socket.gethostbyname(host)
        print(f"[preflight] DNS: {host} -> {ip}")
    except Exception as e:
        raise RuntimeError(f"WP_URLのホスト名が解決できません: {host} ({e})")
    try:
        requests.head(WP_URL, timeout=10, allow_redirects=True)
    except Exception as e:
        raise RuntimeError(f"WP_URLへ接続できません: {WP_URL} ({e})")

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
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始")
    try:
        items = fetch_vr_items(max_hits=30)
        posted = False
        for item in items:
            if not is_released(item):
                print(f"→ 未発売: {item.get('title')}")
                continue
            if create_wp_post(item):
                posted = True
                break  # 新着から1件投稿で終了（必要なら外してループ投稿に）
        if not posted:
            print("新規投稿なし（VR該当なし or 全て既投稿）")
    except Exception as e:
        print(f"エラー: {e}")
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿終了")

if __name__ == "__main__":
    main()
