#!/usr/bin/env python3
"""
EventHub — イベント集約スクレイパー（案2 / GitHub Actions 日次実行）
────────────────────────────────────────────────────────────
複数ソースをサーバ側で集約し、Doorkeeper 互換の events.json を出力する。
ブラウザの CORS 制約を受けないので、connpass 等もここで取得できる。

出力: ../events.json  （フロントの EXT_SOURCES が ./events.json を読む）
依存: 標準ライブラリのみ（urllib）。pip install 不要。

ソース:
  1) Doorkeeper API   … 常に取得可（CORS無しでもOK）
  2) connpass API     … APIキーがあれば取得（環境変数 CONNPASS_API_KEY）
                        ※ connpass は 2024 以降 API キー必須化。未設定ならスキップ。

近畿圏を厚くしたい場合は KEYWORDS / PREFECTURES を調整する。
"""

import json
import os
import ssl
import sys
import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

OUT_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "events.json")

# 理系・学習系に寄せたキーワード
KEYWORDS = [
    "AI", "機械学習", "データ", "プログラミング", "セキュリティ",
    "科学", "宇宙", "ロボット", "バイオ", "量子",
    "IoT", "電子工作", "クラウド", "勉強会", "ハンズオン",
    "カンファレンス", "シンポジウム", "STEM", "展示会",
    # 近畿・地方を厚くするための地名キーワード
    "大阪", "京都", "神戸", "けいはんな", "インテックス", "つくば", "万博",
]

# Doorkeeper の prefecture は英語名。近畿圏を優先的に巡回。
DOORKEEPER_PREFS = [
    None,        # 全国
    "osaka", "kyoto", "hyogo", "nara", "shiga", "wakayama",
    "aichi", "fukuoka", "hokkaido",
]

UA = "EventHub-Scraper/1.0 (+https://rbelgblog.com/event-hub/)"


def http_get_text(url, headers=None, timeout=20):
    """生テキスト(HTML/XML)を取得。失敗時 None。
    証明書チェーンが不完全なサイト（例: けいはんなプラザ）向けに、
    検証失敗時は検証なしで再取得する（公開情報の閲覧のみ）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or isinstance(
                getattr(e, "reason", None), ssl.SSLError):
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    return r.read().decode("utf-8", "replace")
            except Exception as e2:
                sys.stderr.write(f"[warn] GET(text,noverify) failed {url[:70]} : {e2}\n")
                return None
        sys.stderr.write(f"[warn] GET(text) failed {url[:70]} : {e}\n")
        return None
    except (urllib.error.HTTPError, TimeoutError) as e:
        sys.stderr.write(f"[warn] GET(text) failed {url[:70]} : {e}\n")
        return None


def http_get_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        # Doorkeeper はレート制限時に "Retry later" を平文で返す
        if body.lstrip().startswith("Retry later"):
            return None
        return json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
        sys.stderr.write(f"[warn] GET failed {url[:80]}... : {e}\n")
        return None


# ── Doorkeeper ──────────────────────────────────────────────
def fetch_doorkeeper():
    now = datetime.now(timezone.utc).isoformat()
    seen = {}
    for pref in DOORKEEPER_PREFS:
        for kw in KEYWORDS:
            params = {
                "locale": "ja", "sort": "starts_at",
                "per_page": 50, "starts_after": now,
            }
            if pref:
                params["prefecture"] = pref
            if kw:
                params["q"] = kw
            url = "https://api.doorkeeper.jp/events?" + urllib.parse.urlencode(params)
            data = http_get_json(url)
            time.sleep(0.3)  # レート制限対策
            if not isinstance(data, list):
                continue
            for wrap in data:
                ev = wrap.get("event") if isinstance(wrap, dict) else None
                if not ev:
                    continue
                eid = ev.get("id")
                if eid is None or eid in seen:
                    continue
                seen[eid] = normalize_doorkeeper(ev)
    return list(seen.values())


def normalize_doorkeeper(ev):
    return {
        "id": f"dk{ev.get('id')}",
        "title": ev.get("title") or "（タイトルなし）",
        "description": strip(ev.get("description") or ""),
        "starts_at": ev.get("starts_at"),
        "ends_at": ev.get("ends_at"),
        "address": ev.get("address") or "",
        "venue_name": ev.get("venue_name") or "",
        "lat": ev.get("lat"),
        "long": ev.get("long"),
        "ticket_limit": ev.get("ticket_limit") or 0,
        "participants": ev.get("participants") or 0,
        "banner": ev.get("banner"),
        "public_url": ev.get("public_url"),
        "source": "doorkeeper",
    }


# ── connpass（APIキーがあれば）─────────────────────────────
def fetch_connpass():
    api_key = os.environ.get("CONNPASS_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("[info] CONNPASS_API_KEY 未設定のため connpass はスキップ\n")
        return []

    ym = [datetime.now().strftime("%Y%m"),
          (datetime.now().replace(day=1) + timedelta(days=32)).strftime("%Y%m")]
    seen = {}
    for kw in KEYWORDS:
        params = [("keyword", kw), ("count", "50"), ("order", "2")]
        params += [("ym", y) for y in ym]
        url = "https://connpass.com/api/v1/event/?" + urllib.parse.urlencode(params)
        data = http_get_json(url, headers={"X-API-Key": api_key})
        time.sleep(0.3)
        if not isinstance(data, dict):
            continue
        for ev in data.get("events", []):
            eid = ev.get("event_id")
            if eid is None or eid in seen:
                continue
            seen[eid] = normalize_connpass(ev)
    return list(seen.values())


def normalize_connpass(ev):
    desc = ((ev.get("catch") or "") + " " + strip(ev.get("description") or "")).strip()
    return {
        "id": f"cp{ev.get('event_id')}",
        "title": ev.get("title") or "（タイトルなし）",
        "description": desc,
        "starts_at": ev.get("started_at"),
        "ends_at": ev.get("ended_at"),
        "address": ev.get("address") or "",
        "venue_name": ev.get("place") or "",
        "lat": ev.get("lat"),
        "long": ev.get("lon"),
        "ticket_limit": ev.get("limit") or 0,
        "participants": ev.get("accepted") or 0,
        "banner": None,
        "public_url": ev.get("event_url"),
        "source": "connpass",
    }


# ── utils ───────────────────────────────────────────────────
import re
_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"\s+")


def strip(html):
    return _WS.sub(" ", _TAG.sub("", html)).strip()


def is_future(ev):
    # 終了日があればそれで判定（複数日開催の開催中イベントを残す）
    s = ev.get("ends_at") or ev.get("starts_at")
    if not s:
        return True
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc)
    except ValueError:
        return True


# ── ATC（大阪南港ATC）公開イベント一覧ページの埋め込みデータから取得 ──
# 一覧ページ(/event/)のHTMLに、表示用イベントが "preload":[...] のJSONとして
# 埋め込まれている（＝ブラウザが受け取る公開ソースそのもの）。これを読む。
# ※ REST APIはデータセンターIPから弾かれることがあるため、確実なHTML経由に。
# 「開催中・開催予定」のみ／事実(名称/日時/会場/状態)だけ取得しATC公式へリンク。
import html as _htmlmod  # 他ソース(つくばエキスポ)の og:title 復号でも使用

ATC_LIST_URL = "https://www.atc-co.com/event/"
ATC_LAT, ATC_LON = 34.6155, 135.4280  # 大阪南港ATCの座標（近い順ソート用）


def _extract_preload(html):
    i = html.find('"preload":')
    if i < 0:
        return []
    start = html.find("[", i)
    if start < 0:
        return []
    try:
        arr, _ = json.JSONDecoder().raw_decode(html, start)
        return arr if isinstance(arr, list) else []
    except Exception as e:
        sys.stderr.write(f"[warn] ATC preload parse failed: {e}\n")
        return []


def fetch_atc():
    html = http_get_text(ATC_LIST_URL)
    if not html:
        return []
    out = {}
    for e in _extract_preload(html):
        ev = normalize_atc(e)
        if ev:
            out[ev["id"]] = ev
    sys.stderr.write(f"[info] ATC: {len(out)} events (preload)\n")
    return list(out.values())


def normalize_atc(e):
    eid = e.get("id")
    title = (e.get("title") or "").strip()
    d = e.get("date_ymd") or ""
    if eid is None or not title or not d:
        return None
    tm = _JP_TIME.search(e.get("time_text") or "")
    hh, mm = (tm.group(1).zfill(2), tm.group(2)) if tm else ("00", "00")
    starts_at = f"{d}T{hh}:{mm}:00+09:00"
    end = e.get("end_ymd") or d
    ends_at = f"{end}T23:59:00+09:00"
    place = (e.get("location") or "").strip()
    status = e.get("status") or ""
    parts = [p for p in (status, ("会場: " + place) if place else "", e.get("date_text") or "") if p]
    desc = "｜".join(parts) + "（大阪南港ATC）"
    return {
        "id": "atc" + str(eid),
        "title": title,
        "description": desc,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "address": (place + " / 大阪南港ATC") if place else "大阪南港ATC（大阪市住之江区南港北）",
        "venue_name": place or "大阪南港ATC",
        "lat": ATC_LAT,
        "long": ATC_LON,
        "ticket_limit": 0,
        "participants": 0,
        "banner": None,            # 画像は著作物のため転載しない
        "public_url": e.get("url") or e.get("official_url") or "https://www.atc-co.com/event/",
        "source": "atc",
    }


# ── 日本語日付パーサ（「YYYY年M月D日」系。範囲・カンマ列対応）──────
_Z2H = str.maketrans("０１２３４５６７８９", "0123456789")
_JP_FULL = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_JP_TOKEN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日|(\d{1,2})月(\d{1,2})日|(\d{1,2})日")
_JP_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def parse_jp_dates(date_str, time_str=""):
    """「2026年6月6日（土）、7日（日）、20日…」「…日(金) - …日(土)」等から
    開始ISO・終了ISOを推定。年月はトークン走査で引き継ぐ。"""
    if not date_str:
        return None, None
    s = date_str.translate(_Z2H)
    m0 = _JP_FULL.search(s)
    if not m0:
        return None, None
    y, mo, d = int(m0.group(1)), int(m0.group(2)), int(m0.group(3))
    hh = mm = 0
    tm = _JP_TIME.search((time_str or "").translate(_Z2H))
    if tm:
        hh, mm = int(tm.group(1)), int(tm.group(2))
    starts_at = f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:00+09:00"

    cy, cm, last = y, mo, (y, mo, d)
    for tok in _JP_TOKEN.finditer(s[m0.end():]):
        if tok.group(1):
            cy, cm, cd = int(tok.group(1)), int(tok.group(2)), int(tok.group(3))
        elif tok.group(4):
            cm, cd = int(tok.group(4)), int(tok.group(5))
        else:
            cd = int(tok.group(6))
        last = (cy, cm, cd)
    ends_at = None
    if last != (y, mo, d):
        ly, lm, ld = last
        ends_at = f"{ly:04d}-{lm:02d}-{ld:02d}T23:59:00+09:00"
    return starts_at, ends_at


def _hid(prefix, *parts):
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return prefix + h


# ── けいはんなプラザ（公式イベント一覧 1ページに数か月分）──────────
KEIHANNA_URL = "https://www.keihanna-plaza.co.jp/event/"
KEIHANNA_LAT, KEIHANNA_LON = 34.7402, 135.7790

_KH_ARTICLE = re.compile(r'<article class="event-article">(.*?)</article>', re.S)
_KH_HEADER = re.compile(r"<header>(.*?)</header>", re.S)
_KH_TITLE_A = re.compile(r'<h4 class="event-title">\s*<a href="(.*?)">(.*?)</a>', re.S)
_KH_TITLE = re.compile(r'<h4 class="event-title">(.*?)</h4>', re.S)
_KH_VENUE = re.compile(r"会場</th>\s*<td[^>]*>(.*?)</td>", re.S)


def fetch_keihanna():
    html = http_get_text(KEIHANNA_URL)
    if not html:
        return []
    out = {}
    for block in _KH_ARTICLE.findall(html):
        ma = _KH_TITLE_A.search(block)
        if ma:
            url, title = ma.group(1).strip(), strip(ma.group(2))
        else:
            mt = _KH_TITLE.search(block)
            if not mt:
                continue
            url, title = KEIHANNA_URL, strip(mt.group(1))
        if not title:
            continue
        mh = _KH_HEADER.search(block)
        starts_at, ends_at = parse_jp_dates(strip(mh.group(1)) if mh else "")
        if not starts_at:
            continue
        mv = _KH_VENUE.search(block)
        venue = strip(mv.group(1)) if mv else "けいはんなプラザ"
        eid = _hid("kh", title, starts_at)
        out[eid] = {
            "id": eid,
            "title": title,
            "description": f"会場: {venue}（けいはんなプラザ／関西文化学術研究都市）",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "address": f"{venue} / けいはんなプラザ（京都府精華町）",
            "venue_name": venue or "けいはんなプラザ",
            "lat": KEIHANNA_LAT,
            "long": KEIHANNA_LON,
            "ticket_limit": 0,
            "participants": 0,
            "banner": None,
            "public_url": url,
            "source": "keihanna",
        }
    sys.stderr.write(f"[info] けいはんな: {len(out)} events\n")
    return list(out.values())


# ── つくばエキスポセンター（一覧→各詳細ページで開催日を取得）──────
EXPO_LIST = "https://www.expocenter.or.jp/event/list/"
EXPO_LAT, EXPO_LON = 36.0865, 140.1081

_EXPO_LINK = re.compile(r"/event/detail/id=(\d+)")
_EXPO_OG_TITLE = re.compile(r'<meta property="og:title" content="(.*?)"', re.S)
_EXPO_CONTS = re.compile(r'<h3 class="ttl">\s*([^<]+?)\s*</h3>\s*<div class="txt">(.*?)</div>', re.S)


def fetch_expocenter(delay=0.4):
    listing = http_get_text(EXPO_LIST)
    if not listing:
        return []
    ids, seen = [], set()
    for m in _EXPO_LINK.finditer(listing):
        i = m.group(1)
        if i not in seen:
            seen.add(i)
            ids.append(i)
    out = {}
    for i in ids:
        url = f"https://www.expocenter.or.jp/event/detail/id={i}"
        html = http_get_text(url)
        time.sleep(delay)
        if not html:
            continue
        ev = parse_expo_event(html, url, i)
        if ev:
            out[ev["id"]] = ev
    sys.stderr.write(f"[info] つくばエキスポ: {len(out)} events ({len(ids)} pages)\n")
    return list(out.values())


def parse_expo_event(html, url, eid_num):
    mt = _EXPO_OG_TITLE.search(html)
    title = _htmlmod.unescape(mt.group(1)).strip() if mt else ""
    if not title:
        return None
    fields = {strip(k): strip(v) for k, v in _EXPO_CONTS.findall(html)}
    starts_at, ends_at = parse_jp_dates(fields.get("開催日", ""), fields.get("開催時間", ""))
    if not starts_at:
        return None
    place = fields.get("場所", "")
    return {
        "id": "expo" + eid_num,
        "title": title,
        "description": (("会場: " + place + "｜") if place else "")
                       + "つくばエキスポセンター（つくば研究学園都市）",
        "starts_at": starts_at,
        "ends_at": ends_at,
        "address": (place + " / つくばエキスポセンター") if place
                   else "つくばエキスポセンター（茨城県つくば市）",
        "venue_name": place or "つくばエキスポセンター",
        "lat": EXPO_LAT,
        "long": EXPO_LON,
        "ticket_limit": 0,
        "participants": 0,
        "banner": None,
        "public_url": url,
        "source": "expocenter",
    }


# ── キッズプラザ大阪（公式イベント一覧 1ページに全件）────────────
KIDS_URL = "https://www.kidsplaza.or.jp/event/list/"  # 一覧フラグメント（全件入り）
KIDS_BASE = "https://www.kidsplaza.or.jp"
KIDS_LAT, KIDS_LON = 34.7058, 135.5099

_KIDS_LI = re.compile(r'<li class="f5">(.*?)</li>', re.S)
_KIDS_A = re.compile(r'<a href="(/event/[^"]+)"[^>]*>(?:<i[^>]*></i>)?(.*?)</a>', re.S)
_KIDS_PLACE = re.compile(r'<span class="place">(?:<i[^>]*></i>)?(.*?)</span>', re.S)
_KIDS_DATE = re.compile(r'<span class="date">(.*?)(?:<p|</span>)', re.S)


def fetch_kidsplaza():
    html = http_get_text(KIDS_URL)
    if not html:
        return []
    out = {}
    for block in _KIDS_LI.findall(html):
        ma = _KIDS_A.search(block)
        if not ma:
            continue
        path, title = ma.group(1).strip(), strip(ma.group(2))
        if not title:
            continue
        md = _KIDS_DATE.search(block)
        starts_at, ends_at = parse_jp_dates(strip(md.group(1)) if md else "")
        if not starts_at:
            continue
        mp = _KIDS_PLACE.search(block)
        venue = strip(mp.group(1)) if mp else "キッズプラザ大阪"
        eid = "kids" + re.sub(r"\D", "", path)[-9:]
        out[eid] = {
            "id": eid,
            "title": title,
            "description": f"会場: {venue}（キッズプラザ大阪／こどものための博物館）",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "address": f"{venue} / キッズプラザ大阪（大阪市北区扇町）",
            "venue_name": venue or "キッズプラザ大阪",
            "lat": KIDS_LAT,
            "long": KIDS_LON,
            "ticket_limit": 0,
            "participants": 0,
            "banner": None,
            "public_url": KIDS_BASE + path,
            "source": "kidsplaza",
        }
    sys.stderr.write(f"[info] キッズプラザ大阪: {len(out)} events\n")
    return list(out.values())


# ── 大阪市立科学館（公式イベント一覧 tbl-basic、年は当月基準で補完）──
SCI_URL = "https://www.sci-museum.jp/event/"
SCI_LAT, SCI_LON = 34.6920, 135.4910

_SCI_TABLE = re.compile(r'<table class="tbl-basic">(.*?)</table>', re.S)
_SCI_ROW = re.compile(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", re.S)
_SCI_A = re.compile(r'<a href="(#?[^"]*)"[^>]*>(.*?)</a>', re.S)
_SCI_MD = re.compile(r"(\d{1,2})月(\d{1,2})日")


def _sci_ymd(md_text, base_year, base_month):
    """「M月D日」→ YYYY-MM-DD。月が基準月より小さければ翌年扱い。"""
    m = _SCI_MD.search(md_text.translate(_Z2H))
    if not m:
        return None, None, None
    mo, d = int(m.group(1)), int(m.group(2))
    y = base_year + 1 if mo < base_month else base_year
    return y, mo, d


def fetch_scimuseum():
    html = http_get_text(SCI_URL)
    if not html:
        return []
    mt = _SCI_TABLE.search(html)
    if not mt:
        sys.stderr.write("[info] 大阪市立科学館: table not found\n")
        return []
    now = datetime.now()
    by, bm = now.year, now.month
    out = {}
    for date_cell, title_cell in _SCI_ROW.findall(mt.group(1)):
        ma = _SCI_A.search(title_cell)
        if ma:
            href, title = ma.group(1).strip(), strip(ma.group(2))
        else:
            href, title = "", strip(title_cell)
        if not title:
            continue
        # 日付セルは「M月D日（曜）」、範囲は <br>～ で2つ目が終了
        parts = re.split(r"[～〜]", date_cell)
        sy, smo, sd = _sci_ymd(parts[0], by, bm)
        if not sy:
            continue
        starts_at = f"{sy:04d}-{smo:02d}-{sd:02d}T00:00:00+09:00"
        ends_at = None
        if len(parts) > 1:
            ey, emo, ed = _sci_ymd(parts[1], by, bm)
            if ey:
                # 終了が開始より前なら年跨ぎ
                if (ey, emo, ed) < (sy, smo, sd):
                    ey += 1
                ends_at = f"{ey:04d}-{emo:02d}-{ed:02d}T23:59:00+09:00"
        public_url = SCI_URL + href if href.startswith("#") else (href or SCI_URL)
        eid = _hid("sci", title, starts_at)
        out[eid] = {
            "id": eid,
            "title": title,
            "description": "大阪市立科学館（中之島）のイベント・サイエンスショー",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "address": "大阪市立科学館（大阪市北区中之島）",
            "venue_name": "大阪市立科学館",
            "lat": SCI_LAT,
            "long": SCI_LON,
            "ticket_limit": 0,
            "participants": 0,
            "banner": None,
            "public_url": public_url,
            "source": "scimuseum",
        }
    sys.stderr.write(f"[info] 大阪市立科学館: {len(out)} events\n")
    return list(out.values())


# ── インテックス大阪（公式ajax、is_holding=1で開催中・予定を全件）──
INTEX_API = "https://www.intex-osaka.com/jp/event/ajax_search/?is_holding=1&limit=100"
INTEX_PUBLIC = "https://www.intex-osaka.com/jp/event/"
INTEX_LAT, INTEX_LON = 34.6388, 135.4192

_INTEX_ITEM = re.compile(r"<div id='show_event-(\d+)'[^>]*class=\"([^\"]*)\".*?(?=<div id='show_event-|$)", re.S)
_INTEX_YMD = re.compile(r"\b(\d{8})\b")
_INTEX_TAG = re.compile(r'event-tag[^>]*>([^<]+)<')
_INTEX_TTL = re.compile(r'event-ttl">(.*?)</h3>', re.S)


def _intex_iso(ymd, end=False):
    y, mo, d = ymd[:4], ymd[4:6], ymd[6:8]
    return f"{y}-{mo}-{d}T{'23:59' if end else '00:00'}:00+09:00"


def fetch_intex():
    html = http_get_text(INTEX_API, headers={"X-Requested-With": "XMLHttpRequest"})
    if not html:
        return []
    out = {}
    for eid_num, cls in _INTEX_ITEM.findall(html):
        block_start = html.find(f"show_event-{eid_num}")
        block = html[block_start:block_start + 2000]
        mt = _INTEX_TTL.search(block)
        if not mt:
            continue
        title = strip(re.sub(r'<span class="tag-end">.*?</span>', "", mt.group(1)))
        if not title:
            continue
        ymds = _INTEX_YMD.findall(cls)
        if not ymds:
            continue
        starts_at = _intex_iso(ymds[0])
        ends_at = _intex_iso(ymds[-1], end=True) if len(ymds) > 1 and ymds[-1] != ymds[0] else None
        mtag = _INTEX_TAG.search(block)
        genre = strip(mtag.group(1)) if mtag else ""
        desc = (genre + "｜" if genre else "") + "インテックス大阪（大阪南港）"
        eid = "intex" + eid_num
        out[eid] = {
            "id": eid,
            "title": title,
            "description": desc,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "address": "インテックス大阪（大阪市住之江区南港北）",
            "venue_name": "インテックス大阪",
            "lat": INTEX_LAT,
            "long": INTEX_LON,
            "ticket_limit": 0,
            "participants": 0,
            "banner": None,
            "public_url": INTEX_PUBLIC + "#show_event-" + eid_num,
            "source": "intex",
        }
    sys.stderr.write(f"[info] インテックス大阪: {len(out)} events\n")
    return list(out.values())


# ── 大阪科学技術館 OSTEC（公開WP REST。開催日はタイトル内に埋没）──
# robots: /ostec_wpcore/wp-admin のみ禁止＝news取得は許可。
# 開催日が「【M月D日(曜)実施/開催】」等のタイトル表記に依存するため、
# 日付が抽出できたニュースのみ採用（休館案内・月次まとめ等はスキップ）。
OSTEC_API = "https://www.ostec.or.jp/pop/wp-json/wp/v2/news?per_page=50"
OSTEC_LAT, OSTEC_LON = 34.6847, 135.4889

# 「YYYY年M月D日」または「M月D日」（範囲は最大2つ拾う）
_OSTEC_DATE = re.compile(r"(?:(\d{4})年)?\s?(\d{1,2})月(\d{1,2})日")
# イベントでない事務連絡を除外（休館・天候対応・募集締切・新聞/号 等）
_OSTEC_SKIP = re.compile(r"休館|閉館|台風|地震|対応について|締め切り|締切|中止|最新号|新聞|カレンダー")


def fetch_ostec():
    data = http_get_json(OSTEC_API)
    if not isinstance(data, list):
        return []
    out = {}
    for p in data:
        try:
            title = strip(_htmlmod.unescape(p["title"]["rendered"]))
        except Exception:
            continue
        if not title or _OSTEC_SKIP.search(title):
            continue
        matches = _OSTEC_DATE.findall(title.translate(_Z2H))
        if not matches:
            continue   # 日付の無いニュースは載せない
        # 年はタイトルに無ければ「投稿日」を基準に推定（過去記事の誤未来化を防ぐ）
        try:
            base = datetime.fromisoformat(p["date"][:19])
        except Exception:
            base = datetime.now()
        sy, smo, sd = _ostec_ymd(matches[0], base)
        starts_at = f"{sy:04d}-{smo:02d}-{sd:02d}T00:00:00+09:00"
        ends_at = None
        if len(matches) > 1:
            ey, emo, ed = _ostec_ymd(matches[1], base)
            if (ey, emo, ed) < (sy, smo, sd):
                ey += 1
            ends_at = f"{ey:04d}-{emo:02d}-{ed:02d}T23:59:00+09:00"
        eid = "ostec" + str(p.get("id") or _hid("", title, starts_at))
        out[eid] = {
            "id": eid,
            "title": title,
            "description": "大阪科学技術館（OSTEC／靭本町）のイベント・実験教室",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "address": "大阪科学技術館（大阪市西区靭本町）",
            "venue_name": "大阪科学技術館",
            "lat": OSTEC_LAT,
            "long": OSTEC_LON,
            "ticket_limit": 0,
            "participants": 0,
            "banner": None,
            "public_url": p.get("link") or "https://www.ostec.or.jp/pop/",
            "source": "ostec",
        }
    sys.stderr.write(f"[info] 大阪科学技術館: {len(out)} events\n")
    return list(out.values())


def _ostec_ymd(match, base):
    """(year|'', month, day) → (Y,M,D)。年無しは投稿日(base)基準で、
    投稿月より小さい月なら翌年扱い（年末公開→翌年開催に対応）。"""
    y_s, mo_s, d_s = match
    mo, d = int(mo_s), int(d_s)
    if y_s:
        return int(y_s), mo, d
    y = base.year + 1 if mo < base.month else base.year
    return y, mo, d


def main():
    all_events = {}
    for fetch in (fetch_doorkeeper, fetch_connpass, fetch_atc,
                  fetch_keihanna, fetch_expocenter,
                  fetch_kidsplaza, fetch_scimuseum, fetch_intex,
                  fetch_ostec):
        try:
            for ev in fetch():
                if ev.get("id") and is_future(ev):
                    all_events[ev["id"]] = ev
        except Exception as e:  # 1ソースの失敗で全体を止めない
            sys.stderr.write(f"[warn] source failed: {e}\n")

    events = sorted(all_events.values(), key=lambda e: e.get("starts_at") or "")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(events),
        "events": events,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(events)} events -> {os.path.abspath(OUT_PATH)}")


if __name__ == "__main__":
    main()
