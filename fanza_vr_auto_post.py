#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿（APIフロア横断・VR厳密判定・1ファイル完結）
- DMM Affiliate API を floor=videoa / videoc で横断取得
- VR厳密判定（タイトルに「VR」が付く / URL media_type=vr / CIDにvrを含む）
- 通常動画を除外、VR作品のみ投稿
"""

import os, re, time, html, json, pytz, requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# WordPress XMLRPC
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import posts, media
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client

# ------------------ 環境設定 ------------------
DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"
POST_LIMIT = int(os.getenv("POST_LIMIT", "2"))
RECENT_DAYS = int(os.getenv("RECENT_DAYS", "3"))
HITS = int(os.getenv("HITS", "30"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "6"))
FLOORS = os.getenv("FLOORS", "videoa,videoc").split(",")
REQUIRE_RELEASED = int(os.getenv("REQUIRE_RELEASED", "1"))
RELEASE_GRACE_HOURS = int(os.getenv("RELEASE_GRACE_HOURS", "36"))

# ------------------ ユーティリティ ------------------
def now_jst():
    return datetime.now(pytz.timezone("Asia/Tokyo"))

def parse_jst_date(s: str):
    jst = pytz.timezone("Asia/Tokyo")
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return jst.localize(datetime.strptime(s, fmt))
        except ValueError:
            pass
    return jst.localize(datetime(1970,1,1))

def get_env(key, required=True):
    v = os.getenv(key)
    if required and not v:
        raise RuntimeError(f"環境変数 {key} が未設定です")
    return v

def make_affiliate_link(url: str, aff_id: str) -> str:
    pu = urlparse(url)
    q = dict(parse_qsl(pu.query))
    q["affiliate_id"] = aff_id
    return urlunparse((pu.scheme, pu.netloc, pu.path, pu.params, urlencode(q), pu.fragment))

# ------------------ VR判定・発売済み判定 ------------------
def contains_vr(item: dict) -> bool:
    """
    VR厳密判定（タイトルに「VR」が付く / URLにmedia_type=vr / VR系CID）
    """
    # URLチェック
    try:
        u = item.get("URL", "")
        pu = urlparse(u)
        q = dict(parse_qsl(pu.query))
        if q.get("media_type", "").lower() == "vr":
            return True
        if "/vrvideo/" in pu.path:
            return True
    except Exception:
        pass

    # content_id パターン（dsvr, idvr 等）
    cid = (item.get("content_id") or item.get("product_id") or "").lower()
    if re.search(r"(?:^|[^a-z])(dsvr|idvr|[a-z]*vr)\d{2,}", cid):
        return True

    # タイトルに「VR」がトークンとして出現（Avril除外）
    title = (item.get("title") or "")
    if re.search(r"(?<![A-Za-z0-9])VR(?![A-Za-z0-9])", title):
        return True
    if any(k in title for k in ["【VR】", "VR専用", "8K VR", "8KVR", "ハイクオリティVR"]):
        return True

    return False

def is_released(item: dict) -> bool:
    if not REQUIRE_RELEASED:
        return True
    ds = item.get("date")
    if not ds:
        return False
    try:
        d = parse_jst_date(ds)
        grace = d - timedelta(hours=RELEASE_GRACE_HOURS)
        return grace <= now_jst()
    except Exception:
        return False

# ------------------ DMM API ------------------
def dmm_request(params: dict) -> dict:
    r = requests.get(DMM_API_URL, params=params, timeout=15)
    if r.status_code != 200:
        print(f"[API] Error {r.status_code}: {r.text[:200]}")
        return {}
    data = r.json()
    return data.get("result", {}) or {}

def base_params(offset: int, floor: str, use_keyword=True) -> dict:
    base = {
        "api_id": get_env("DMM_API_ID"),
        "affiliate_id": get_env("DMM_AFFILIATE_ID"),
        "site": "FANZA",
        "service": "digital",
        "floor": floor,
        "sort": "date",
        "output": "json",
        "hits": HITS,
        "offset": offset,
    }
    if use_keyword:
        base["keyword"] = "VR"
    return base

def fetch_vr_items_from_floors() -> list[dict]:
    print("[API] フロア横断取得開始 →", ",".join(FLOORS))
    raw = []
    for floor in FLOORS:
        for page in range(MAX_PAGES):
            offset = 1 + page * HITS
            print(f"[API] floor={floor} page={page+1} offset={offset}")
            res = dmm_request(base_params(offset, floor, True))
            items = res.get("items", []) or []
            print(f"[API] 取得 {len(items)} 件")
            if not items:
                break
            raw.extend(items)
            time.sleep(0.2)
    vr_items = [it for it in raw if contains_vr(it)]
    released = [it for it in vr_items if is_released(it)]
    print(f"[API] 総取得: {len(raw)} / VR判定: {len(vr_items)} / 発売OK: {len(released)}")
    released.sort(key=lambda x: x.get("date", ""), reverse=True)
    return released

# ------------------ 本文生成 ------------------
def fallback_description(item: dict) -> str:
    ii = item.get("iteminfo", {}) or {}
    desc = item.get("description") or ""
    if len(desc) < 20:
        desc = html.unescape(
            "、".join([x.get("name", "") for x in ii.get("genre", []) if isinstance(x, dict)])
        )
    return desc or "FANZA（DMM）VR作品の紹介です。"

# ------------------ WordPress投稿 ------------------
def upload_image(wp: Client, url: str):
    try:
        data = requests.get(url, timeout=12).content
        name = os.path.basename(url.split("?")[0])
        return wp.call(media.UploadFile({
            "name": name,
            "type": "image/jpeg",
            "bits": xmlrpc_client.Binary(data)
        }))["id"]
    except Exception as e:
        print(f"[画像アップロード失敗] {e}")
        return None

def create_wp_post(item: dict, wp: Client, category: str, aff_id: str) -> bool:
    title = item.get("title", "").strip()
    if not contains_vr(item):
        print(f"→ 非VRスキップ: {title}")
        return False

    exist = wp.call(GetPosts({"post_status": "publish", "s": title}))
    if any(p.title == title for p in exist):
        print(f"→ 既投稿: {title}")
        return False

    siu = item.get("sampleImageURL") or {}
    images = siu.get("sample_l", {}).get("image") or siu.get("sample_s", {}).get("image") or []
    if not images:
        print(f"→ 画像なしスキップ: {title}")
        return False
    thumb_id = upload_image(wp, images[0])

    aff_link = make_affiliate_link(item["URL"], aff_id)
    desc = fallback_description(item)
    html_content = f"<p><a href='{aff_link}' target='_blank'><img src='{images[0]}'></a></p><p>{desc}</p>"
    post = WordPressPost()
    post.title = title
    post.content = html_content
    if thumb_id: post.thumbnail = thumb_id
    post.terms_names = {"category": [category]}
    post.post_status = "publish"
    wp.call(posts.NewPost(post))
    print(f"✔ 投稿完了: {title}")
    return True

# ------------------ メイン ------------------
def main():
    print(f"[{now_jst()}] VR新着投稿開始（VRタイトル限定）")
    wp = Client(get_env("WP_URL"), get_env("WP_USER"), get_env("WP_PASS"))
    cat = get_env("CATEGORY")
    aff = get_env("DMM_AFFILIATE_ID")

    items = fetch_vr_items_from_floors()
    boundary = now_jst() - timedelta(days=RECENT_DAYS)
    recent = [i for i in items if parse_jst_date(i.get("date", "")) >= boundary]
    backlog = [i for i in items if i not in recent]
    print(f"直近{RECENT_DAYS}日: {len(recent)} / バックログ: {len(backlog)}")

    posted = 0
    for it in recent + backlog:
        if create_wp_post(it, wp, cat, aff):
            posted += 1
            if posted >= POST_LIMIT:
                break
    print(f"投稿数: {posted}")
    print(f"[{now_jst()}] 終了")

if __name__ == "__main__":
    main()
