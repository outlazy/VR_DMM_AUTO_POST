#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FANZA（DMM）VR新着 → WordPress自動投稿（修正版・完全実行ファイル）
- 取得元: /av/list/?genre=6548&media_type=vr&release=latest&sort=date
- CID抽出パターン修正済み
- 年齢認証再試行対応
"""

import os, re, time, json, html, pytz, requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from wordpress_xmlrpc import Client, WordPressPost
from wordpress_xmlrpc.methods import posts, media
from wordpress_xmlrpc.methods.posts import GetPosts
from wordpress_xmlrpc.compat import xmlrpc_client

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# --- 共通設定 ---
DMM_API_URL = "https://api.dmm.com/affiliate/v3/ItemList"
POST_LIMIT = int(os.getenv("POST_LIMIT", "2"))
RECENT_DAYS = int(os.getenv("RECENT_DAYS", "3"))
VR_LIST_PAGES = int(os.getenv("VR_LIST_PAGES", "3"))
AGE_GATE_COOKIE = os.getenv("AGE_GATE_COOKIE", "").strip() or "ckcy=1; age_check_done=1"

# --- JST処理 ---
def now_jst(): return datetime.now(pytz.timezone('Asia/Tokyo'))
def parse_jst_date(s):
    jst = pytz.timezone('Asia/Tokyo')
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try: return jst.localize(datetime.strptime(s, fmt))
        except: continue
    return jst.localize(datetime(1970,1,1))

def get_env(key, req=True):
    v = os.getenv(key)
    if req and not v: raise RuntimeError(f"環境変数 {key} 未設定")
    return v

# --- CIDスクレイプ（修正版） ---
def scrape_vr_cids(max_pages=VR_LIST_PAGES):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.dmm.co.jp/",
        "Cookie": AGE_GATE_COOKIE
    }
    bases = [
        "https://video.dmm.co.jp/av/list/?genre=6548&media_type=vr&release=latest&sort=date",
        "https://video.dmm.co.jp/av/list/?media_type=vr&sort=date",
        "https://www.dmm.co.jp/digital/vrvideo/-/list/=/sort=date/",
    ]

    found=[]
    pat_href  = re.compile(r"(?:/detail/=/cid=|content/\?id=)([a-z0-9_]+)", re.I)
    pat_data  = re.compile(r'(?:data-cid="|"cid":"?)([a-z0-9_]+)', re.I)
    pat_prod  = re.compile(r'(?:data-product-id|data-content-id|data-gtm-list-product-id)=["\']([a-z0-9_]+)["\']', re.I)
    pat_json1 = re.compile(r'"cid"\s*:\s*"([a-z0-9_]+)"', re.I)
    pat_json2 = re.compile(r'"contentId"\s*:\s*"([a-z0-9_]+)"', re.I)

    def _norm(c): return re.sub(r"[^a-z0-9_]", "", c.lower())
    def collect(txt):
        ids=[]
        for p in (pat_href,pat_data,pat_prod,pat_json1,pat_json2):
            ids+=p.findall(txt)
        ids+=re.findall(r"cid=([a-z0-9_]+)", txt, re.I)
        seen,out=set(),[]
        for i in ids:
            i=_norm(i)
            if i and i not in seen:
                seen.add(i); out.append(i)
        return out

    for base in bases:
        for p in range(1,max_pages+1):
            url = f"{base}&page={p}" if "?" in base else f"{base}?page={p}"
            try:
                r = requests.get(url, headers=headers, timeout=14)
                if (r.url and "/age_check" in r.url) or ("From here on" in r.text):
                    hc=headers.copy(); hc["Cookie"]=AGE_GATE_COOKIE
                    r = requests.get(url, headers=hc, timeout=14)
                if r.status_code!=200: 
                    print(f"VR一覧失敗 {r.status_code} {url}"); break
                cids=collect(r.text)
                print(f"[VR一覧] {p}ページ: {len(cids)}件")
                found+=cids
            except Exception as e:
                print(f"エラー {e}"); break
            time.sleep(0.3)

    uniq=[]
    [uniq.append(x) for x in found if x not in uniq]
    return uniq

# --- API処理 ---
def dmm_request(params):
    r=requests.get(DMM_API_URL,params=params,timeout=10)
    if r.status_code!=200: print(r.text); r.raise_for_status()
    return r.json().get("result",{})

def fetch_item_by_cid(cid):
    pid=get_env("DMM_API_ID"); aff=get_env("DMM_AFFILIATE_ID")
    p={"api_id":pid,"affiliate_id":aff,"site":"FANZA","service":"digital","floor":"videoa","cid":cid,"hits":1,"output":"json"}
    try:
        res=dmm_request(p)
        it=res.get("items",[{}])[0]
        return it
    except: return None

def fetch_vr_items():
    print("VR一覧スクレイプ開始")
    cids=scrape_vr_cids()
    items=[]
    for i,c in enumerate(cids,1):
        it=fetch_item_by_cid(c)
        if it: items.append(it)
        if i%10==0: time.sleep(0.2)
    items.sort(key=lambda x:x.get("date",""),reverse=True)
    print(f"取得件数: {len(items)}")
    return items

# --- 投稿処理 ---
def upload_image(wp,url):
    try:
        data=requests.get(url,timeout=10).content
        name=os.path.basename(url.split("?")[0])
        res=wp.call(media.UploadFile({"name":name,"type":"image/jpeg","bits":xmlrpc_client.Binary(data)}))
        return res.get("id")
    except Exception as e:
        print(f"画像失敗 {url} {e}"); return None

def create_post(item,wp,cat,aff):
    title=item.get("title","")
    if any(p.title==title for p in wp.call(GetPosts({"post_status":"publish","s":title}))):
        print(f"→既投稿 {title}"); return False
    imgs=item.get("sampleImageURL",{}).get("sample_s",{}).get("image",[])
    if not imgs: print(f"→画像なし {title}"); return False
    afflink=f"{item['URL']}?affiliate_id={aff}"
    desc=item.get("iteminfo",{}).get("comment","FANZA VR動画紹介。")
    post=WordPressPost()
    post.title=title
    post.content=f'<a href="{afflink}" target="_blank"><img src="{imgs[0]}"></a><p>{desc}</p>'
    post.terms_names={"category":[cat]}
    post.post_status="publish"
    wp.call(posts.NewPost(post))
    print(f"✔投稿完了: {title}")
    return True

# --- メイン ---
def main():
    print(f"[{now_jst()}] VR新着投稿開始")
    wp=Client(get_env("WP_URL"),get_env("WP_USER"),get_env("WP_PASS"))
    aff=get_env("DMM_AFFILIATE_ID"); cat=get_env("CATEGORY")
    items=fetch_vr_items()
    posted=0
    for it in items[:POST_LIMIT]:
        if create_post(it,wp,cat,aff): posted+=1
    print(f"投稿数: {posted}")
    print(f"[{now_jst()}] 終了")

if __name__=="__main__":
    main()
