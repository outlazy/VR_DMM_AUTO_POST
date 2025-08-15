#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

MAX_PAGES = int(os.environ.get("MAX_PAGES", "6"))
HITS = int(os.environ.get("HITS", "30"))
POST_LIMIT = int(os.environ.get("POST_LIMIT", "1"))

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
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(qs), parsed.fragment))

def is_valid_description(desc: str) -> bool:
    if not desc or len(desc.strip()) < 30:
        return False
    for ng in NG_DESCRIPTIONS:
        if ng in desc:
            return False
    return True

def fetch_description_from_detail_page(url, item):
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        html = r.text
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if m and is_valid_description(m.group(1).strip()):
            return m.group(1).strip()
        m_script = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        if m_script:
            try:
                jd = json.loads(m_script.group(1))
                desc = jd.get("description") or (jd.get("subjectOf") or {}).get("description", "")
                if is_valid_description(desc):
                    return desc.strip()
            except:
                pass
    except:
        pass
    ii = item.get("iteminfo", {}) or {}
    for key in ("description", "comment", "story"):
        val = item.get(key) or ii.get(key)
        if is_valid_description(val or ""):
            return val
    cast = "、".join([a["name"] for a in ii.get("actress", []) if "name" in a])
    label = "、".join([l["name"] for l in ii.get("label", []) if "name" in l])
    genres = "、".join([g["name"] for g in ii.get("genre", []) if "name" in g])
    volume = item.get("volume", "")
    base = f"{item['title']}。ジャンル：{genres}。出演：{cast}。レーベル：{label}。収録時間：{volume}。"
    return base if len(base) > 10 else "FANZA（DMM）VR動画の自動投稿です。"

def contains_vr(item):
    ii = item.get("iteminfo", {}) or {}
    names = [g.get("name", "") for g in ii.get("genre", []) if isinstance(g, dict)]
    joined = " ".join(names)
    return ("VR" in joined) or ("ＶＲ" in joined) or ("バーチャル" in joined)

def is_released(item):
    ds = item.get("date")
    if not ds:
        return False
    try:
        jst = pytz.timezone('Asia/Tokyo')
        release_dt = jst.localize(datetime.strptime(ds, "%Y-%m-%d %H:%M:%S"))
        return release_dt <= now_jst()
    except:
        return False

def fetch_vr_released_items_sorted():
    API_ID = get_env("DMM_API_ID")
    AFF_ID = get_env("DMM_AFFILIATE_ID")
    all_items = []

    for page in range(MAX_PAGES):
        offset = page * HITS
        params = {
            "api_id": API_ID,
            "affiliate_id": AFF_ID,
            "site": "FANZA",
            "service": "digital",
            "floor": "videoa",
            "sort": "date",
            "output": "json",
            "hits": HITS,
            "offset": offset,
            "keyword": "VR",
        }
        r = requests.get(DMM_API_URL, params=params, timeout=12)
        r.raise_for_status()
        items = r.json().get("result", {}).get("items", []) or []
        if not items:
            break
        all_items.extend(items)

    # 発売済み＋VR判定でフィルタ
    released = [it for it in all_items if contains_vr(it) and is_released(it)]
    # 発売日降順ソート
    released.sort(key=lambda x: x['date'], reverse=True)
    print(f"VR発売済み件数: {len(released)}（日付降順）")
    return released

def upload_image(wp, url):
    try:
        data = requests.get(url, timeout=12).content
        name = os.path.basename(url.split("?")[0])
        media_data = {"name": name, "type": "image/jpeg", "bits": xmlrpc_client.Binary(data)}
        res = wp.call(media.UploadFile(media_data))
        return res.get("id")
    except:
        return None

def create_wp_post(item):
    WP_URL = get_env('WP_URL').strip()
    WP_USER = get_env('WP_USER')
    WP_PASS = get_env('WP_PASS')
    CATEGORY = get_env('CATEGORY')
    AFF_ID = get_env('DMM_AFFILIATE_ID')
    wp = Client(WP_URL, WP_USER, WP_PASS)

    title = item["title"]
    existing = wp.call(GetPosts({"post_status": "publish", "s": title}))
    if any(p.title == title for p in existing):
        print(f"既投稿: {title}")
        return False

    images = []
    siu = item.get("sampleImageURL", {}) or {}
    if "sample_l" in siu and "image" in siu["sample_l"]:
        images = siu["sample_l"]["image"]
    elif "sample_s" in siu and "image" in siu["sample_s"]:
        images = siu["sample_s"]["image"]
    if not images:
        return False
    thumb_id = upload_image(wp, images[0]) if images else None

    tags = set()
    ii = item.get("iteminfo", {}) or {}
    for key in ("label", "maker", "actress", "genre"):
        if key in ii:
            for v in ii[key]:
                if "name" in v:
                    tags.add(v["name"])

    aff_link = make_affiliate_link(item["URL"], AFF_ID)
    desc = fetch_description_from_detail_page(item["URL"], item)
    parts = [f'<p><a href="{aff_link}" target="_blank"><img src="{images[0]}" alt="{title}"></a></p>',
             f'<p><a href="{aff_link}" target="_blank">{title}</a></p>',
             f'<div>{desc}</div>']
    for img in images[1:]:
        parts.append(f'<p><img src="{img}" alt="{title}"></p>')

    post = WordPressPost()
    post.title = title
    post.content = "\n".join(parts)
    if thumb_id:
        post.thumbnail = thumb_id
    post.terms_names = {"category": [CATEGORY], "post_tag": list(tags)}
    post.post_status = "publish"
    wp.call(posts.NewPost(post))
    print(f"投稿完了: {title}")
    return True

def main():
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始（発売済・降順）")
    items = fetch_vr_released_items_sorted()
    posted = 0
    for item in items:
        if create_wp_post(item):
            posted += 1
            if posted >= POST_LIMIT:
                break
    if posted == 0:
        print("新規投稿なし")
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿終了")

if __name__ == "__main__":
    main()
