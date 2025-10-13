#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿（APIフロア横断版・1ファイル完結）
- スクレイピング不使用。DMM Affiliate APIを floor=videoa / videoc で横断取得
- VR判定を多面的に強化（content_id/URL/ジャンル/タイトル）
- 発売済みのみ（日付<=現在JST）を日付降順で整列、直近優先で最大POST_LIMIT件投稿
"""

import os, re, time, json, html, pytz, requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# 古い依存が collections.Iterable を参照する対策
import collections as _collections, collections.abc as _abc
for _n in ("Iterable","Mapping","MutableMapping","Sequence"):
    if not hasattr(_collections,_n) and hasattr(_abc,_n):
        setattr(_collections,_n,getattr(_abc,_n))

from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import posts, media
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client

# ------------- 環境変数 -------------
DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"
POST_LIMIT  = int(os.getenv("POST_LIMIT", "2"))
RECENT_DAYS = int(os.getenv("RECENT_DAYS", "3"))
HITS        = int(os.getenv("HITS", "30"))      # API 1ページ件数（最大30）
MAX_PAGES   = int(os.getenv("MAX_PAGES", "6"))  # 取得ページ数
FLOORS      = os.getenv("FLOORS", "videoa,videoc").split(",")

# ------------- ユーティリティ -------------
def now_jst():
    return datetime.now(pytz.timezone("Asia/Tokyo"))

def parse_jst_date(s: str):
    jst = pytz.timezone("Asia/Tokyo")
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S","%Y-%m-%d %H:%M","%Y-%m-%d"):
        try:
            return jst.localize(datetime.strptime(s, fmt))
        except ValueError:
            pass
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", s)
    if m:
        return jst.localize(datetime.strptime(m.group(1), "%Y-%m-%d"))
    return jst.localize(datetime(1970,1,1))

def get_env(key, required=True):
    v = os.getenv(key)
    if required and not v:
        raise RuntimeError(f"環境変数 {key} が未設定です")
    return v

def make_affiliate_link(url: str, aff_id: str) -> str:
    pu = urlparse(url)
    q  = dict(parse_qsl(pu.query))
    q["affiliate_id"] = aff_id
    return urlunparse((pu.scheme, pu.netloc, pu.path, pu.params, urlencode(q), pu.fragment))

# ------------- VR判定＆発売済み -------------
def contains_vr(item: dict) -> bool:
    # 1) content_id に 'vr' っぽいパターン（例: 13dsvr01821）
    cid = (item.get("content_id") or item.get("product_id") or "").lower()
    if re.search(r"vr", cid):
        return True
    # 2) URL に /vr/ や media_type=vr が含まれる
    u = (item.get("URL") or "").lower()
    if "/vr/" in u or "media_type=vr" in u:
        return True
    # 3) ジャンルに VR 系ワード
    ii = item.get("iteminfo", {}) or {}
    names = [g.get("name","") for g in ii.get("genre",[]) if isinstance(g,dict)]
    joined = " ".join(names)
    for k in ("VR","ＶＲ","バーチャル","8K VR","VR専用","ハイクオリティVR"):
        if k in joined:
            return True
    # 4) タイトルに VR
    if "VR" in (item.get("title","") or ""):
        return True
    return False

def is_released(item: dict) -> bool:
    ds = item.get("date")
    if not ds: return False
    try:
        return parse_jst_date(ds) <= now_jst()
    except Exception:
        return False

# ------------- DMM API -------------
def dmm_request(params: dict) -> dict:
    r = requests.get(DMM_API_URL, params=params, timeout=12)
    if r.status_code != 200:
        print("---- DMM API Error ----")
        print(r.text[:1200])
        print("-----------------------")
        r.raise_for_status()
    data = r.json()
    return data.get("result", {}) or {}

def base_params(offset: int, floor: str, use_keyword=True) -> dict:
    return {
        "api_id":       get_env("DMM_API_ID"),
        "affiliate_id": get_env("DMM_AFFILIATE_ID"),
        "site":   "FANZA",
        "service":"digital",
        "floor":  floor,     # videoa / videoc
        "sort":   "date",
        "output": "json",
        "hits":   HITS,
        "offset": offset,    # ※ DMMは 1 起点
        **({"keyword":"VR"} if use_keyword else {}),
    }

def fetch_vr_items_from_floors() -> list[dict]:
    print("[API] フロア横断取得開始 →", ",".join(FLOORS))
    got = []
    for floor in FLOORS:
        for page in range(MAX_PAGES):
            offset = 1 + page*HITS
            print(f"[API] floor={floor} page={page+1} offset={offset}")
            try:
                res   = dmm_request(base_params(offset,floor,use_keyword=True))
                items = res.get("items",[]) or []
            except Exception as e:
                print(f"[API] keyword=VR 失敗 ({floor} p{page+1}): {e} → keywordなし再試行")
                try:
                    res   = dmm_request(base_params(offset,floor,use_keyword=False))
                    items = res.get("items",[]) or []
                except Exception as e2:
                    print(f"[API] keywordなしも失敗 ({floor} p{page+1}): {e2} → 次フロアへ")
                    break
            print(f"[API] 取得 {len(items)} 件")
            if not items:
                break
            got.extend(items)
            time.sleep(0.2)

    # VRだけ抽出＋発売済み
    filtered = [it for it in got if contains_vr(it) and is_released(it)]
    filtered.sort(key=lambda x: x.get("date",""), reverse=True)
    print(f"[API] VR発売済み件数: {len(filtered)}（日付降順）")
    return filtered

# ------------- 本文（説明）フォールバック -------------
def fallback_description(item: dict) -> str:
    ii = item.get("iteminfo", {}) or {}
    for key in ("description","comment","story"):
        v = (item.get(key) or ii.get(key) or "").strip()
        if 20 <= len(v) <= 800: return html.unescape(v)
    cast  = "、".join([a.get("name","") for a in ii.get("actress",[]) if isinstance(a,dict)])
    label = "、".join([l.get("name","") for l in ii.get("label",[])   if isinstance(l,dict)])
    genres= "、".join([g.get("name","") for g in ii.get("genre",[])   if isinstance(g,dict)])
    title = item.get("title","")
    vol   = item.get("volume","")
    base  = f"{title}。ジャンル：{genres}。出演：{cast}。レーベル：{label}。収録時間：{vol}。"
    return base if len(base) > 10 else "FANZA（DMM）VR作品の自動紹介です。"

# ------------- WordPress 投稿 -------------
def upload_image(wp: Client, url: str):
    try:
        data = requests.get(url, timeout=12).content
        name = os.path.basename(url.split("?")[0])
        return wp.call(media.UploadFile({"name":name,"type":"image/jpeg","bits":xmlrpc_client.Binary(data)})).get("id")
    except Exception as e:
        print(f"画像アップロード失敗: {url} ({e})")
        return None

def create_wp_post(item: dict, wp: Client, category: str, aff_id: str) -> bool:
    title = item.get("title","").strip()
    # 既投稿チェック（タイトル検索＋完全一致）
    try:
        exist = wp.call(GetPosts({"post_status":"publish","s":title}))
        if any(p.title == title for p in exist):
            print(f"→ 既投稿: {title}（スキップ）")
            return False
    except Exception:
        pass

    # 画像
    images = []
    siu = item.get("sampleImageURL") or {}
    if siu.get("sample_l",{}).get("image"): images = siu["sample_l"]["image"]
    elif siu.get("sample_s",{}).get("image"): images = siu["sample_s"]["image"]
    if not images:
        print(f"→ サンプル画像なし: {title}（スキップ）")
        return False
    thumb_id = upload_image(wp, images[0])

    # タグ
    tags = set()
    ii = item.get("iteminfo",{}) or {}
    for key in ("label","maker","actress","genre","series"):
        for v in ii.get(key,[]) or []:
            if isinstance(v,dict) and v.get("name"):
                tags.add(v["name"])

    aff_link = make_affiliate_link(item["URL"], aff_id)
    desc = fallback_description(item)

    parts = []
    parts.append(f'<p><a href="{aff_link}" target="_blank" rel="nofollow noopener"><img src="{images[0]}" alt="{title}"></a></p>')
    parts.append(f'<p><a href="{aff_link}" target="_blank" rel="nofollow noopener">{title}</a></p>')
    parts.append(f'<div>{desc}</div>')
    for img in images[1:]:
        parts.append(f'<p><img src="{img}" alt="{title}"></p>')
    parts.append(f'<p><a href="{aff_link}" target="_blank" rel="nofollow noopener">{title}</a></p>')

    post = WordPressPost()
    post.title = title
    post.content = "\n".join(parts)
    if thumb_id: post.thumbnail = thumb_id
    post.terms_names = {"category":[category], "post_tag": list(tags)}
    post.post_status = "publish"
    wp.call(posts.NewPost(post))
    print(f"✔ 投稿完了: {title}")
    return True

# ------------- メイン -------------
def main():
    print(f"[{now_jst()}] VR新着投稿開始（APIフロア横断）")
    wp  = Client(get_env("WP_URL"), get_env("WP_USER"), get_env("WP_PASS"))
    cat = get_env("CATEGORY")
    aff = get_env("DMM_AFFILIATE_ID")

    items = fetch_vr_items_from_floors()
    # 直近優先
    boundary = now_jst() - timedelta(days=RECENT_DAYS)
    recent, backlog = [], []
    for it in items:
        try:
            (recent if parse_jst_date(it.get("date","")) >= boundary else backlog).append(it)
        except Exception:
            backlog.append(it)
    print(f"直近{RECENT_DAYS}日: {len(recent)} / バックログ: {len(backlog)}")

    posted = 0
    for it in recent + backlog:
        if create_wp_post(it, wp, cat, aff):
            posted += 1
            if posted >= POST_LIMIT: break

    print(f"投稿数: {posted}")
    print(f"[{now_jst()}] 終了")

if __name__ == "__main__":
    main()
