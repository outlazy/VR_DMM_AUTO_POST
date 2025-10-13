#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿（genre=6548 / release=latest / sort=date 専用）
- 取得元： https://video.dmm.co.jp/av/list/?genre=6548&media_type=vr&release=latest&sort=date （ページネーション対応）
- リストから CID（例: 13dsvr01821 等）を robust に抽出
- CID から DMM API（cid 指定）で詳細補完 → 発売済みのみを降順整列
- 直近 RECENT_DAYS を優先して最大 POST_LIMIT 件だけ WordPress へ自動投稿
- 説明文抽出は www 優先（404 は即スキップ）/ video も候補

必須: WP_URL / WP_USER / WP_PASS / DMM_API_ID / DMM_AFFILIATE_ID / CATEGORY
任意: POST_LIMIT=2 / RECENT_DAYS=3 / VR_LIST_PAGES=3
      SCRAPE_DESC=1 / AGE_GATE_COOKIE="ckcy=1; age_check_done=1" / FORCE_DETAIL_DOMAIN=www
"""

import os, re, json, html, time
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# 互換パッチ（collections）
import collections as _collections
import collections.abc as _abc
for _name in ("Iterable", "Mapping", "MutableMapping", "Sequence"):
    if not hasattr(_collections, _name) and hasattr(_abc, _name):
        setattr(_collections, _name, getattr(_abc, _name))

import pytz
import requests
from bs4 import BeautifulSoup

# WordPress
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import media, posts
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client

DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"

# ===== 環境設定 =====
POST_LIMIT          = int(os.environ.get("POST_LIMIT", "2"))
RECENT_DAYS         = int(os.environ.get("RECENT_DAYS", "3"))
VR_LIST_PAGES       = int(os.environ.get("VR_LIST_PAGES", "3"))
SCRAPE_DESC         = os.environ.get("SCRAPE_DESC", "1") == "1"
AGE_GATE_COOKIE     = os.environ.get("AGE_GATE_COOKIE", "").strip()
FORCE_DETAIL_DOMAIN = os.environ.get("FORCE_DETAIL_DOMAIN", "www").strip()

# ===== 共通 =====

def now_jst() -> datetime:
    return datetime.now(pytz.timezone('Asia/Tokyo'))


def parse_jst_date(s: str) -> datetime:
    jst = pytz.timezone('Asia/Tokyo')
    s = (s or '').strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return jst.localize(datetime.strptime(s, fmt))
        except ValueError:
            continue
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", s)
    if m:
        return jst.localize(datetime.strptime(m.group(1), "%Y-%m-%d"))
    return jst.localize(datetime(1970, 1, 1))


def get_env(key: str, required: bool = True, default=None):
    v = os.environ.get(key, default)
    if required and not v:
        raise RuntimeError(f"環境変数 {key} が設定されていません")
    return v


def make_affiliate_link(url: str, aff_id: str) -> str:
    p = urlparse(url)
    qs = dict(parse_qsl(p.query))
    qs["affiliate_id"] = aff_id
    new_q = urlencode(qs)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))

# ===== VR判定・発売済み =====

def contains_vr(item: dict) -> bool:
    if (item.get("floor_code") or "").lower() == "vrvideo":
        return True
    ii = item.get("iteminfo", {}) or {}
    names = [g.get("name", "") for g in ii.get("genre", []) if isinstance(g, dict)]
    joined = " ".join(names)
    keys = ["VR", "ＶＲ", "バーチャル", "8K VR", "VR専用", "ハイクオリティVR"]
    return any(k in joined for k in keys)


def is_released(item: dict) -> bool:
    ds = item.get("date")
    if not ds:
        return False
    try:
        return parse_jst_date(ds) <= now_jst()
    except Exception:
        return False

# ===== DMM API =====

def dmm_request(params: dict) -> dict:
    r = requests.get(DMM_API_URL, params=params, timeout=14)
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


def fetch_item_by_cid(cid: str) -> dict | None:
    API_ID = get_env("DMM_API_ID")
    AFF_ID = get_env("DMM_AFFILIATE_ID")
    params = {
        "api_id": API_ID,
        "affiliate_id": AFF_ID,
        "site": "FANZA",
        "service": "digital",
        "floor": "videoa",
        "output": "json",
        "hits": 1,
        "offset": 1,
        "cid": cid,
    }
    try:
        res = dmm_request(params)
        items = res.get("items", []) or []
        return items[0] if items else None
    except Exception as e:
        print(f"CID補完失敗: {cid} ({e})")
        return None

# ===== 説明文抽出 =====

NG_DESCRIPTIONS = [
    "From here on, it will be an adult site",
    "18歳未満", "未成年", "18才未満",
    "アダルト商品を取り扱う", "成人向け", "アダルトサイト", "ご利用は18歳以上",
]

AGE_MARKERS = [
    "18歳未満", "未満の方のアクセス", "成人向け", "アダルトサイト",
    "under the age of 18", "age verification"
]

SCRAPE_DESC         = os.environ.get("SCRAPE_DESC", "1") == "1"
AGE_GATE_COOKIE     = os.environ.get("AGE_GATE_COOKIE", "").strip()
FORCE_DETAIL_DOMAIN = os.environ.get("FORCE_DETAIL_DOMAIN", "www").strip()


def is_valid_description(desc: str) -> bool:
    if not desc or len(desc.strip()) < 30:
        return False
    for ng in NG_DESCRIPTIONS:
        if ng in desc:
            return False
    return True


def _clean_text(s: str) -> str:
    s = html.unescape(s or "").strip()
    s = re.sub(r"\s{2,}", " ", s)
    for b in ["18歳未満", "成人向け", "アダルトサイト", "ご利用は18歳以上", "年齢認証", "無修正"]:
        s = s.replace(b, "")
    return s.strip()


def extract_main_description_from_html_bytes(html_bytes: bytes) -> str | None:
    if not SCRAPE_DESC or not html_bytes:
        return None
    try:
        soup = BeautifulSoup(html_bytes, "lxml")
    except Exception:
        soup = BeautifulSoup(html_bytes, "html.parser")

    raw_text = soup.get_text(" ", strip=True)
    if any(k in raw_text for k in AGE_MARKERS):
        return None

    candidates: list[str] = []
    for sel in [
        "div.mg-b20.lh4",
        "div#introduction", "section#introduction", "div.introduction", "section.introduction",
        "[data-contents='introduction']",
        ".vbox .txt", ".d-item__intro", "#performer + div",
        ".txt",
    ]:
        for n in soup.select(sel):
            t = n.get_text("\n", strip=True)
            if t:
                candidates.append(t)

    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        ht = (h.get_text(strip=True) or "")
        if any(k in ht for k in ["作品紹介", "作品内容", "ストーリー", "あらすじ", "解説"]):
            parts: list[str] = []
            sib = h.find_next_sibling()
            while sib and sib.name not in ["h1", "h2", "h3", "h4"]:
                if sib.name in ["p", "div", "section"]:
                    t = sib.get_text(" ", strip=True)
                    if t:
                        parts.append(t)
                sib = sib.find_next_sibling()
            if parts:
                candidates.append("\n".join(parts))

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

    best, best_score = None, -1
    for c in candidates:
        c2 = _clean_text(c)
        if not ok(c2):
            continue
        score = len(c2) + 20 * (c2.count("。") + c2.count("！") + c2.count("？"))
        if score > best_score:
            best, best_score = c2, score
    if best:
        return best

    m = soup.select_one('meta[property="og:description"]')
    if m and m.get("content"):
        d = _clean_text(m["content"]) 
        if 30 <= len(d) <= 700 and is_valid_description(d):
            return d

    m = soup.select_one('meta[name="description"]')
    if m and m.get("content"):
        d = _clean_text(m["content"]) 
        if 30 <= len(d) <= 700 and is_valid_description(d):
            return d

    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.get_text(strip=True))
        except Exception:
            continue
        arr = data if isinstance(data, list) else [data]
        for jd in arr:
            if isinstance(jd, dict) and "description" in jd:
                d = _clean_text(str(jd["description"]))
                if 30 <= len(d) <= 700 and is_valid_description(d):
                    return d
    return None

# ===== URL候補生成 =====

def _extract_cid_from_url(u: str) -> str:
    m = re.search(r"(?:cid|id)=([a-z0-9_]+)", u)
    return m.group(1) if m else ""


def _build_candidate_urls(item: dict, original_url: str) -> list[str]:
    cid = (item.get("content_id") or item.get("product_id") or _extract_cid_from_url(original_url) or "").lower()
    urls: list[str] = []
    if cid:
        urls = [
            f"https://video.dmm.co.jp/av/content/?id={cid}",
            f"https://www.dmm.co.jp/digital/vrvideo/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/vrvideo/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
            f"https://www.dmm.co.jp/av/-/detail/=/cid={cid}/",
        ]
    else:
        urls = [original_url]

    # www優先 or video優先を反映
    if FORCE_DETAIL_DOMAIN in ("video", "www"):
        pref = "video." if FORCE_DETAIL_DOMAIN == "video" else "www."
        urls.sort(key=lambda x: 0 if urlparse(x).netloc.startswith(pref) else 1)

    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_description_from_detail_page(url: str, item: dict) -> str:
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

    last_err = None
    for u in _build_candidate_urls(item, url):
        try:
            resp = requests.get(u, headers=headers, timeout=12, allow_redirects=True)
            if resp.status_code == 404:
                print(f"説明抽出: 404 / {u}")
                continue
            desc = extract_main_description_from_html_bytes(resp.content)
            if desc and is_valid_description(desc):
                print(f"説明抽出: OK / {u}")
                return desc
        except Exception as e:
            last_err = e
            print(f"説明抽出失敗: {u} ({e})")
            time.sleep(0.2)
    if last_err:
        print(f"説明抽出最終エラー: {last_err}")
    return fallback_description(item)

# ===== VR一覧スクレイプ（genre=6548 / release=latest / sort=date） =====

def scrape_vr_cids(max_pages: int = VR_LIST_PAGES) -> list[str]:
    base = "https://video.dmm.co.jp/av/list/?genre=6548&media_type=vr&release=latest&sort=date"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://video.dmm.co.jp/",
    }
    if AGE_GATE_COOKIE:
        headers["Cookie"] = AGE_GATE_COOKIE

    found: list[str] = []

    # robust 正規表現群
    pat_href   = re.compile(r"/av/content/\?id=([a-z0-9_]+)", re.I)
    pat_href2  = re.compile(r"/detail/=/(?:cid|content_id)=([a-z0-9_]+)/?", re.I)
    pat_cidq   = re.compile(r"cid=([a-z0-9_]+)", re.I)
    pat_data   = re.compile(r'(?:data-cid=|data-product-id=|data-content-id=|data-gtm-list-product-id=)["\']([a-z0-9_]+)["\']', re.I)
    pat_json   = re.compile(r'\"(?:contentId|productId|cid)\"\s*:\s*\"([a-z0-9_]+)\"', re.I)

    def _norm(c: str) -> str:
        return re.sub(r"[^a-z0-9_]", "", c.strip().lower())

    for p in range(1, max_pages + 1):
        url = base + (f"&page={p}" if p > 1 else "")
        try:
            r = requests.get(url, headers=headers, timeout=14)
            if r.status_code != 200:
                print(f"VR一覧取得失敗: {r.status_code} {url}")
                break
            txt = r.text
            cids = []
            cids += pat_href.findall(txt)
            cids += pat_href2.findall(txt)
            cids += pat_cidq.findall(txt)
            cids += pat_data.findall(txt)
            cids += pat_json.findall(txt)
            if not cids:
                print(f"CIDが見つかりません: {url}")
            else:
                print(f"[VR一覧] page {p}: CID {len(cids)}件")
            for c in cids:
                nc = _norm(c)
                if nc and nc not in found:
                    found.append(nc)
        except Exception as e:
            print(f"VR一覧取得エラー: {e}")
            break
        time.sleep(0.25)

    return found

# ===== 一覧→API補完 → 発売済み =====

def fetch_released_from_vr_list() -> list[dict]:
    print(f"VR一覧スクレイプ開始（pages={VR_LIST_PAGES}）")
    cids = scrape_vr_cids(VR_LIST_PAGES)
    if not cids:
        print("一覧からCIDが取得できませんでした（終了）")
        return []
    items: list[dict] = []
    for i, cid in enumerate(cids, 1):
        it = fetch_item_by_cid(cid)
        if it:
            items.append(it)
        if i % 10 == 0:
            time.sleep(0.3)
    released = [it for it in items if contains_vr(it) and is_released(it)]
    released.sort(key=lambda x: x.get('date', ''), reverse=True)
    print(f"VR発売済み件数: {len(released)}（一覧→API補完／日付降順）")
    return released

# ===== 投稿処理 =====

def upload_image(wp: Client, url: str):
    try:
        data = requests.get(url, timeout=12).content
        name = os.path.basename(url.split("?")[0])
        media_data = {"name": name, "type": "image/jpeg", "bits": xmlrpc_client.Binary(data)}
        res = wp.call(media.UploadFile(media_data))
        return res.get("id")
    except Exception as e:
        print(f"画像アップロード失敗: {url} ({e})")
        return None


def create_wp_post(item: dict, wp: Client, category: str, aff_id: str) -> bool:
    title = item.get("title", "")

    # 既投稿チェック（タイトル一致）
    existing = wp.call(GetPosts({"post_status": "publish", "s": title}))
    if any(p.title == title for p in existing):
        print(f"→ 既投稿: {title}（スキップ）")
        return False

    # 画像
    images: list[str] = []
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
    tags: set[str] = set()
    ii = item.get("iteminfo", {}) or {}
    for key in ("label", "maker", "actress", "genre"):
        if key in ii and ii[key]:
            for v in ii[key]:
                if isinstance(v, dict) and "name" in v:
                    tags.add(v["name"])

    aff_link = make_affiliate_link(item["URL"], get_env("DMM_AFFILIATE_ID"))
    desc = fetch_description_from_detail_page(item["URL"], item)

    parts: list[str] = []
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

# ===== メイン =====

def main():
    print(f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')}] VR新着投稿開始（genre=6548/latest/date）")
    try:
        WP_URL = get_env('WP_URL').strip()
        WP_USER = get_env('WP_USER')
        WP_PASS = get_env('WP_PASS')
        CATEGORY = get_env('CATEGORY')
        wp = Client(WP_URL, WP_USER, WP_PASS)

        items = fetch_released_from_vr_list()
        # 直近優先
        boundary = now_jst() - timedelta(days=RECENT_DAYS)
        recent = []
        backlog = []
        for it in items:
            try:
                dt = parse_jst_date(it.get('date',''))
                (recent if dt >= boundary else backlog).append(it)
            except Exception:
                backlog.append(it)
        print(f"直近{RECENT_DAYS}日: {len(recent)} / バックログ: {len(backlog)}")

        posted = 0
        for it in recent + backlog:
            if create_wp_post(it, wp, CATEGORY, get_env('DMM_AFFILIATE_ID')):
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
