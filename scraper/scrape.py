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
import sys
import time
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
    """生テキスト(HTML/XML)を取得。失敗時 None。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        sys.stderr.write(f"[warn] GET(text) failed {url[:80]}... : {e}\n")
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


# ── ATC（大阪南港ATC）公式サイトマップ＋公開ページから事実のみ取得 ──
# robots.txt で /event/ は許可。検索エンジン向けに公開された公式サイトマップを使い、
# 各イベント公式ページから「名称・日時・会場・ジャンル・公式URL」だけを取得し、
# 詳細はATC公式へリンクする（説明文・画像は転載しない）= Googleニュース型の集約。
import html as _htmlmod

ATC_SITEMAPS = [
    "https://www.atc-co.com/event-sitemap.xml",
    "https://www.atc-co.com/event-sitemap2.xml",
]
ATC_LAT, ATC_LON = 34.6155, 135.4280  # 大阪南港ATCの座標（近い順ソート用）

_ATC_LOC = re.compile(r"<loc>\s*(https://www\.atc-co\.com/event/event-\d+/?)\s*</loc>")
_ATC_OG_TITLE = re.compile(r'<meta property="og:title" content="(.*?)"', re.S)
_ATC_OG_URL = re.compile(r'<meta property="og:url" content="(.*?)"')
_ATC_GENRE = re.compile(r'genre-badge">(.*?)</span>', re.S)
_ATC_DATE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")
_ATC_DATE_MD = re.compile(r"(\d{2})\.(\d{2})")
_ATC_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def _atc_dd(label, html):
    m = re.search(r"<dt>\s*" + label + r"\s*</dt>\s*<dd>(.*?)</dd>", html, re.S)
    return strip(m.group(1)) if m else ""


def fetch_atc(max_pages=400, delay=0.4, stop_after_past=60):
    # 1) 公式サイトマップからイベントURLを収集
    urls, seen_u = [], set()
    for sm in ATC_SITEMAPS:
        xml = http_get_text(sm)
        if not xml:
            continue
        for m in _ATC_LOC.finditer(xml):
            u = m.group(1)
            if u not in seen_u:
                seen_u.add(u)
                urls.append(u)
    if not urls:
        return []

    # 2) サイトマップは古い順なので末尾(新しい=開催予定)から処理。
    #    終了済みが stop_after_past 件連続したら打ち切り、毎日の取得を最小化。
    urls.reverse()
    out, fetched, consec_past = {}, 0, 0
    for url in urls:
        if fetched >= max_pages:
            break
        html = http_get_text(url)
        fetched += 1
        time.sleep(delay)          # 礼儀としてアクセス間隔を空ける
        if not html:
            continue
        ev = parse_atc_event(html, url)
        if not ev:
            continue
        if is_future(ev):
            out[ev["id"]] = ev
            consec_past = 0
        else:
            consec_past += 1
            if consec_past >= stop_after_past:
                break
    sys.stderr.write(f"[info] ATC: {len(out)} upcoming events ({fetched} pages fetched)\n")
    return list(out.values())


def parse_atc_event(html, url):
    m = _ATC_OG_TITLE.search(html)
    title = _htmlmod.unescape(m.group(1)) if m else ""
    title = re.sub(r"\s*\|\s*大阪ベイエリア.*$", "", title).strip()
    if not title:
        return None

    mu = _ATC_OG_URL.search(html)
    public_url = mu.group(1) if mu else url
    date_str = _atc_dd("開催日", html)
    time_str = _atc_dd("開催時間", html)
    place = _atc_dd("開催場所", html)
    genres = [strip(g) for g in _ATC_GENRE.findall(html)]

    starts_at, ends_at = parse_atc_dates(date_str, time_str)
    if not starts_at:
        return None

    mid = re.search(r"event-(\d+)", url)
    eid = "atc" + (mid.group(1) if mid else str(abs(hash(url)) % 100000))

    desc = "｜".join([g for g in genres if g])
    if place:
        desc = (desc + "｜会場: " + place) if desc else ("会場: " + place)
    desc = (desc + "（大阪南港ATC）").strip("｜ ")

    return {
        "id": eid,
        "title": title,
        "description": desc,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "address": (place + " / 大阪南港ATC") if place else "大阪南港ATC",
        "venue_name": place or "大阪南港ATC",
        "lat": ATC_LAT,
        "long": ATC_LON,
        "ticket_limit": 0,
        "participants": 0,
        "banner": None,            # 画像は著作物のため転載しない
        "public_url": public_url,
        "source": "atc",
    }


def parse_atc_dates(date_str, time_str):
    if not date_str:
        return None, None
    full = _ATC_DATE.findall(date_str)   # [(Y,M,D), ...]
    if not full:
        return None, None
    y, mo, d = full[0]
    hh, mm = "00", "00"
    tm = _ATC_TIME.search(time_str or "")
    if tm:
        hh, mm = tm.group(1).zfill(2), tm.group(2)
    starts_at = f"{y}-{mo}-{d}T{hh}:{mm}:00+09:00"

    ends_at = None
    if any(sep in date_str for sep in ("～", "〜", "-", "ー")):
        if len(full) >= 2:                       # YYYY.MM.DD～YYYY.MM.DD
            ey, emo, ed = full[1]
            ends_at = f"{ey}-{emo}-{ed}T23:59:00+09:00"
        else:                                    # YYYY.MM.DD～MM.DD（同年）
            tail = re.split(r"[～〜\-ー]", date_str, 1)
            if len(tail) == 2:
                md = _ATC_DATE_MD.search(tail[1])
                if md:
                    ends_at = f"{y}-{md.group(1)}-{md.group(2)}T23:59:00+09:00"
    return starts_at, ends_at


def main():
    all_events = {}
    for fetch in (fetch_doorkeeper, fetch_connpass, fetch_atc):
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
