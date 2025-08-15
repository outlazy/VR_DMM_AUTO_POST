#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）アフィリエイトAPIで VR 単品の新着を取得 → WordPress 自動投稿

◎仕様まとめ
- 直近RECENT_DAYS日以内の「発売済VR」を優先投稿。足りなければバックログ（発売済の過去作/新しい順）で補完
- APIは新着(sort=date)に未来日が混ざるので、ローカルで「発売済のみ」を抽出して発売日降順に整列
- DMM APIの offset は 1 始まり（1, 1+HITS, ...）に対応
- keyword=VR で 400/NG の場合は keyword なしに自動フォールバック
- 商品ページから説明文を高精度で抽出（og:description / meta description / JSON-LD 全総当り）
  - UA/Referer付与・HTMLエンティティデコード・注意書き除去・文字数レンジで品質担保
  - 取れない場合は APIフィールド or 自動生成にフォールバック
- Python 3.10+ の collections.* 廃止対応モンキーパッチ（古いライブラリ対策）
- 既投稿はタイトル一致でスキップ（より強くしたいなら content_id 埋め込み検知も後で足せる）

◎必要な Secrets（GitHub Actions）
  WP_URL / WP_USER / WP_PASS / DMM_API_ID / DMM_AFFILIATE_ID / CATEGORY
◎オプション env（未設定なら右の既定値）
  MAX_PAGES=6, HITS=30, POST_LIMIT=2, RECENT_DAYS=3
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

DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"

# ===== 可変パラメータ（envで上書き可） =====
MAX_PAGES   = int(os.environ.get("MAX_PAGES", "6"))   # 探索最大ページ数（1ページ=HITS件）
HITS        = int(os.environ.get("HITS", "30"))       # 1ページ取得件数
POST_LIMIT  = int(os.environ.get("POST_LIMIT", "2"))  # 1回の実行で投稿する最大件数（デフォ2本）
RECENT_DAYS = int(os.environ.get("RECENT_DAYS", "3")) # 直近何日を“新作”とみなすか
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

def fetch_description_from_detail_page(url, item):
    """商品ページから説明文を高精度抽出。NGならAPI/自動生成へフォールバック。"""
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

        def clean(s: str) -> str:
            s = html.unescape(s).strip()
            BAD = [
                "アダルトサイト", "18歳未満", "成人向け", "From here on",
                "ご利用は18歳以上", "adult", "エログ", "無修正", "違法"
            ]
            for b in BAD:
                s = s.replace(b, "")
            # 余分な空白/改行を整形
            s = re.sub(r"\s{2,}", " ", s)
            return s.strip()

        def ok(s: str) -> bool:
            if not s:
                return False
            s = s.strip()
            # 短すぎ/長すぎ排除（サイトに合わせて調整可）
            return 30 <= len(s) <= 700 and is_valid_description(s)

        # 1) og:description
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html_txt, re.I)
        if m:
            desc = clean(m.group(1))
            if ok(desc): 
                return desc

        # 2) meta name=description
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html_txt, re.I)
        if m:
            desc = clean(m.group(1))
            if ok(desc): 
                return desc

        # 3) すべての JSON-LD を総当り
        for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html_txt, re.S | re.I
        ):
            raw = m.group(1).strip()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for jd in candidates:
                if isinstance(jd, dict):
                    # 直接
                    if "description" in jd:
                        desc = clean(str(jd["description"]))
                        if ok(desc): 
                            return desc
                    # ネスト（例: subjectOf.description）
                    sub = jd.get("subjectOf")
                    if isinstance(sub, dict) and "description" in sub:
                        desc = clean(str(sub["description"]))
                        if ok(desc): 
                            return desc

    except Exception as e:
        print(f"商品ページ説明抽出失敗: {e}")
        time.sleep(0.2)  # 軽いバックオフ

    # 4) API系フィールドでフォールバック
    ii = item.get("iteminfo", {}) or {}
    for key in ("description", "comment", "story"):
        val = (item.get(key) or ii.get(key) or "").strip()
        if 20 <= len(val) <= 800 and is_valid_description(val):
            return val

    # 5) 最終フォールバック（自動生成）
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

    # 発売済み＋VR判定 → 発売日降順
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
    # どちらも発売日降順のまま
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

    # タグ抽出
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
    post.title = title  # バッジ付けたいなら： post.title = "【VR】" + title
    post.content = "\n".join(parts)
    if thumb_id:
        post.thumbnail = thumb_id
    post.terms_names = {"category": [category], "post_tag": list(tags)}
    post.post_status = "publish"  # 下書き運用なら "draft"
    wp.call(posts.NewPost(post))
    print(f"✔ 投稿完了: {title}")
    return True

def main():
    jst_now = now_jst()
    print(f"[{jst_now.strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始（優先: 直近{RECENT_DAYS}日／不足分はバックログ）")
    try:
        # 準備
        WP_URL = get_env('WP_URL').strip()
        WP_USER = get_env('WP_USER')
        WP_PASS = get_env('WP_PASS')
        CATEGORY = get_env('CATEGORY')
        AFF_ID = get_env('DMM_AFFILIATE_ID')
        wp = Client(WP_URL, WP_USER, WP_PASS)

        # 取得・整形
        all_released = fetch_all_vr_released_sorted()
        recent, backlog = split_recent_and_backlog(all_released)
        print(f"直近{RECENT_DAYS}日: {len(recent)} / バックログ: {len(backlog)}")

        # 投稿
        posted = 0
        # 1) 直近分を優先
        for item in recent:
            if create_wp_post(item, wp, CATEGORY, AFF_ID):
                posted += 1
                if posted >= POST_LIMIT:
                    break

        # 2) まだ足りなければバックログ（発売日降順）
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
