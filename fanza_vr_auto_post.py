#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿（APIフロア横断・VR超厳密判定・1ファイル完結）
- DMM Affiliate API を floor=videoa / videoc で横断取得
- VR厳密判定：
    1) URLが media_type=vr または /vrvideo/ を含む → True
    2) CIDが *vrNN / dsvrNN などVRパターン → True
    3) 上記以外は「タイトルにVR」かつ「ジャンル名にVR系語彙（VR専用/8KVR等）」のときのみ True
  => タイトルだけの“なんちゃってVR”は除外（例：Avril／VR絶頂など）
"""

import os, re, time, html, json, pytz, requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# ====== collections.Iterable 互換パッチ（wordpress_xmlrpc 対策） ======
import collections as _col
import collections.abc as _abc
for _n in ("Iterable", "Mapping", "MutableMapping", "Sequence"):
    if not hasattr(_col, _n) and hasattr(_abc, _n):
        setattr(_col, _n, getattr(_abc, _n))

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

# ------------------ VR判定・発売済み判定（超厳密） ------------------
def _has_vr_token_in_title(title: str) -> bool:
    # 「VR」が前後英数字に接していない（Avril対策）
    return bool(re.search(r"(?<![A-Za-z0-9])VR(?![A-Za-z0-9])", title or "")) or any(
        kw in (title or "") for kw in ["【VR】", "VR専用", "8K VR", "8KVR", "ハイクオリティVR"]
    )

def _genre_has_vr_words(iteminfo: dict) -> bool:
    names = [x.get("name","") for x in (iteminfo or {}).get("genre",[]) if isinstance(x,dict)]
    joined = " ".join(names)
    vr_words = ["VR", "ＶＲ", "VR専用", "8KVR", "8K VR", "ハイクオリティVR", "VR動画", "VR作品"]
    return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", joined) for w in vr_words)

def contains_vr(item: dict) -> bool:
    """
    超厳密VR判定：
      A) URL: media_type=vr or /vrvideo/ → True
      B) CID: *vr + 数字（2桁以上） or dsvr + 数字 → True
      C) タイトルにVRトークン かつ ジャンルにVR語彙 → True
      それ以外は False
    """
    # A) URLで判定
    try:
        u = item.get("URL", "")
        pu = urlparse(u)
        q = dict(parse_qsl(pu.query))
        if q.get("media_type","").lower() == "vr":
            return True
        if "/vrvideo/" in pu.path:
            return True
    except Exception:
        pass

    # B) CIDで判定
    cid = (item.get("content_id") or item.get("product_id") or "").lower()
    if re.search(r"(?:^|[^a-z])(dsvr|idvr|[a-z]*vr)\d{2,}", cid):
        return True

    # C) タイトル + ジャンル
    title_ok = _has_vr_token_in_title(item.get("title",""))
    genre_ok = _genre_has_vr_words(item.get("iteminfo",{}) or {})
    if title_ok and genre_ok:
        return True

    # ここまで来たら非VR
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
        "floor": floor,      # videoa / videoc
        "sort": "date",
        "output": "json",
        "hits": HITS,
        "offset": offset,    # 1起点
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
    unreleased = [it for it in vr_items if not is_released(it)]
    released = [it for it in vr_items if is_released(it)]
    print(f"[API] 総取得: {len(raw)} / VR判定: {len(vr_items)} / 未発売: {len(unreleased)} / 発売OK: {len(released)}")
    released.sort(key=lambda x: x.get("date", ""), reverse=True)
    return released

# ------------------ 本文フォールバック ------------------
def fallback_description(item: dict) -> str:
    ii = item.get("iteminfo", {}) or {}
    for key in ("description","comment","story"):
        v = (item.get(key) or ii.get(key) or "").strip()
        if 20 <= len(v) <= 800:
            return html.unescape(v)
    cast  = "、".join([a.get("name","") for a in ii.get("actress",[]) if isinstance(a,dict)])
    label = "、".join([l.get("name","") for l in ii.get("label",[])   if isinstance(l,dict)])
    genres= "、".join([g.get("name","") for g in ii.get("genre",[])   if isinstance(g,dict)])
    series= "、".join([s.get("name","") for s in ii.get("series",[])  if isinstance(s,dict)])
    maker = "、".join([m.get("name","") for m in ii.get("maker",[])   if isinstance(m,dict)])
    title = item.get("title","")
    vol   = item.get("volume","")
    base  = f"{title}。ジャンル：{genres}。出演：{cast}。シリーズ：{series}。メーカー：{maker}。レーベル：{label}。収録時間：{vol}。"
    return base if len(base) > 10 else "FANZA（DMM）VR作品の自動紹介です。"

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

    try:
        exist = wp.call(GetPosts({"post_status": "publish", "s": title}))
        if any(p.title == title for p in exist):
            print(f"→ 既投稿: {title}")
            return False
    except Exception as e:
        print(f"[既投稿チェック失敗] {e}")

    siu = item.get("sampleImageURL") or {}
    images = siu.get("sample_l", {}).get("image") or siu.get("sample_s", {}).get("image") or []
    if not images:
        print(f"→ 画像なしスキップ: {title}")
        return False
    thumb_id = upload_image(wp, images[0])

    aff_link = make_affiliate_link(item["URL"], aff_id)
    desc = fallback_description(item)

    parts = [
        f'<p><a href="{aff_link}" target="_blank" rel="nofollow noopener"><img src="{images[0]}" alt="{title}"></a></p>',
        f'<p><a href="{aff_link}" target="_blank" rel="nofollow noopener">{title}</a></p>',
        f'<div>{desc}</div>',
    ] + [f'<p><img src="{img}" alt="{title}"></p>' for img in images[1:]] + [
        f'<p><a href="{aff_link}" target="_blank" rel="nofollow noopener">{title}</a></p>'
    ]

    post = WordPressPost()
    post.title = title
    post.content = "\n".join(parts)
    if thumb_id: post.thumbnail = thumb_id
    post.terms_names = {"category": [category]}
    post.post_status = "publish"
    wp.call(posts.NewPost(post))
    print(f"✔ 投稿完了: {title}")
    return True

# ------------------ メイン ------------------
def main():
    print(f"[{now_jst()}] VR新着投稿開始（VRタイトル+ジャンル連動/URL/CIDの超厳密判定）")
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
