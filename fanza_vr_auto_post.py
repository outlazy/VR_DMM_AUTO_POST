#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿（2025年対応）
- DMM側の動画URL仕様変更（/av/content/?id=xxxx）に対応
- DMM API floor=vrvideo を優先し、videoa/videocもフォールバック
- BeautifulSoupで説明文抽出
"""

import os, re, json, html, time, pytz, requests
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime, timedelta
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import media, posts
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client
from bs4 import BeautifulSoup

# ===== 可変設定 =====
MAX_PAGES   = int(os.environ.get("MAX_PAGES", "6"))
HITS        = int(os.environ.get("HITS", "30"))
POST_LIMIT  = int(os.environ.get("POST_LIMIT", "2"))
RECENT_DAYS = int(os.environ.get("RECENT_DAYS", "3"))
SCRAPE_DESC = os.environ.get("SCRAPE_DESC", "1") == "1"
AGE_GATE_COOKIE = os.environ.get("AGE_GATE_COOKIE", "").strip()
FORCE_DETAIL_DOMAIN = os.environ.get("FORCE_DETAIL_DOMAIN", "www").strip()
DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"

# ===== ユーティリティ =====
def now_jst():
    return datetime.now(pytz.timezone('Asia/Tokyo'))

def parse_jst_date(s):
    jst = pytz.timezone('Asia/Tokyo')
    return jst.localize(datetime.strptime(s, "%Y-%m-%d %H:%M:%S"))

def get_env(k, required=True, default=None):
    v = os.environ.get(k, default)
    if required and not v:
        raise RuntimeError(f"環境変数 {k} が未設定")
    return v

def make_affiliate_link(url, aff_id):
    p = urlparse(url)
    qs = dict(parse_qsl(p.query))
    qs["affiliate_id"] = aff_id
    new_q = urlencode(qs)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))

# ===== VR判定・日付 =====
def contains_vr(item):
    if (item.get("floor_code") or "").lower() == "vrvideo":
        return True
    if "VR" in (item.get("floor_name") or ""):
        return True
    ii = item.get("iteminfo", {}) or {}
    names = [g.get("name", "") for g in ii.get("genre", []) if isinstance(g, dict)]
    joined = " ".join(names)
    keys = ["VR", "ＶＲ", "バーチャル", "8K VR", "VR専用", "ハイクオリティVR"]
    return any(k in joined for k in keys)

def is_released(item):
    ds = item.get("date")
    if not ds: return False
    try: return parse_jst_date(ds) <= now_jst()
    except: return False

# ===== DMM API =====
def dmm_request(params):
    r = requests.get(DMM_API_URL, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    res = data.get("result", {})
    if isinstance(res, dict) and res.get("status") == "NG":
        raise RuntimeError(f"DMM API NG: {res.get('message')}")
    return res

def fetch_all_vr_released_sorted():
    floors = ["vrvideo", "videoa", "videoc"]
    API_ID = get_env("DMM_API_ID")
    AFF_ID = get_env("DMM_AFFILIATE_ID")
    all_items = []

    for floor in floors:
        print(f"[API] floor={floor}")
        for page in range(MAX_PAGES):
            offset = 1 + page * HITS
            print(f"[API] page {page+1} fetch (offset={offset})")
            params = {
                "api_id": API_ID, "affiliate_id": AFF_ID,
                "site": "FANZA", "service": "digital", "floor": floor,
                "sort": "date", "output": "json", "hits": HITS, "offset": offset, "keyword": "VR"
            }
            try:
                res = dmm_request(params)
                items = res.get("items", []) or []
            except Exception as e:
                print(f"[API] {floor} 失敗: {e}")
                items = []
            if not items:
                break
            all_items.extend(items)

    released = [it for it in all_items if contains_vr(it) and is_released(it)]
    released.sort(key=lambda x: x.get('date', ''), reverse=True)
    print(f"[API] VR発売済み件数: {len(released)}（floor複合／日付降順）")
    return released

# ===== 説明文抽出 =====
def extract_main_description_from_html_bytes(html_bytes):
    soup = BeautifulSoup(html_bytes, "lxml")
    raw = soup.get_text(" ", strip=True)
    if any(k in raw for k in ["18歳未満", "成人向け", "アダルトサイト"]):
        return None
    desc = None
    for sel in ["div.mg-b20.lh4", "div#introduction", ".vbox .txt"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if len(t) > 40:
                desc = t
                break
    if not desc:
        m = soup.select_one('meta[property="og:description"]')
        if m and m.get("content"):
            desc = html.unescape(m["content"])
    return desc

def _build_candidate_urls(item, original_url):
    cid = item.get("content_id") or ""
    urls = [
        f"https://video.dmm.co.jp/av/content/?id={cid}",
        f"https://www.dmm.co.jp/digital/vrvideo/-/detail/=/cid={cid}/",
        f"https://www.dmm.co.jp/vrvideo/-/detail/=/cid={cid}/",
        f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
        f"https://www.dmm.co.jp/av/-/detail/=/cid={cid}/"
    ]
    return urls

def fetch_description_from_detail_page(url, item):
    if not SCRAPE_DESC:
        return item.get("title", "")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ja,en-US;q=0.9",
        "Referer": "https://video.dmm.co.jp/"
    }
    if AGE_GATE_COOKIE:
        headers["Cookie"] = AGE_GATE_COOKIE
    for u in _build_candidate_urls(item, url):
        try:
            r = requests.get(u, headers=headers, timeout=10)
            if r.status_code == 404:
                continue
            desc = extract_main_description_from_html_bytes(r.content)
            if desc and len(desc) > 40:
                print(f"説明抽出成功: {u}")
                return desc
        except Exception as e:
            print(f"説明抽出失敗: {u} ({e})")
    return f"{item.get('title')} の紹介文です。"

# ===== 投稿 =====
def upload_image(wp, url):
    try:
        data = requests.get(url, timeout=10).content
        name = os.path.basename(url.split("?")[0])
        media_data = {"name": name, "type": "image/jpeg", "bits": xmlrpc_client.Binary(data)}
        res = wp.call(media.UploadFile(media_data))
        return res.get("id")
    except Exception as e:
        print(f"画像アップロード失敗: {url} ({e})")
        return None

def create_wp_post(item, wp, category, aff_id):
    title = item.get("title", "")
    existing = wp.call(GetPosts({"post_status": "publish", "s": title}))
    if any(p.title == title for p in existing):
        print(f"既投稿: {title}")
        return False

    images = []
    siu = item.get("sampleImageURL", {}) or {}
    for k in ["sample_l", "sample_s"]:
        if k in siu and "image" in siu[k]:
            images = siu[k]["image"]
            break
    if not images:
        print(f"画像なし: {title}")
        return False

    aff_link = make_affiliate_link(item["URL"], aff_id)
    desc = fetch_description_from_detail_page(item["URL"], item)

    content = [
        f'<p><a href="{aff_link}" target="_blank"><img src="{images[0]}" alt="{title}"></a></p>',
        f"<p>{desc}</p>"
    ]
    post = WordPressPost()
    post.title = title
    post.content = "\n".join(content)
    post.terms_names = {"category": [category]}
    post.post_status = "publish"
    thumb_id = upload_image(wp, images[0])
    if thumb_id:
        post.thumbnail = thumb_id
    wp.call(posts.NewPost(post))
    print(f"✔ 投稿完了: {title}")
    return True

# ===== メイン =====
def main():
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始")
    try:
        WP_URL = get_env('WP_URL').strip()
        WP_USER = get_env('WP_USER')
        WP_PASS = get_env('WP_PASS')
        CATEGORY = get_env('CATEGORY')
        AFF_ID = get_env('DMM_AFFILIATE_ID')
        wp = Client(WP_URL, WP_USER, WP_PASS)

        all_released = fetch_all_vr_released_sorted()
        boundary = now_jst() - timedelta(days=RECENT_DAYS)
        recent = [it for it in all_released if parse_jst_date(it["date"]) >= boundary]
        backlog = [it for it in all_released if it not in recent]

        posted = 0
        for item in recent + backlog:
            if create_wp_post(item, wp, CATEGORY, AFF_ID):
                posted += 1
                if posted >= POST_LIMIT:
                    break
        if posted == 0:
            print("新規投稿なし")
        else:
            print(f"合計投稿数: {posted}")
    except Exception as e:
        print(f"エラー: {e}")
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿終了")

if __name__ == "__main__":
    main()
