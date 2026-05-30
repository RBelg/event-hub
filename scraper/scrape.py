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

UA = "EventHub-Scraper/1.0 (+https://github.com/)"


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
    s = ev.get("starts_at")
    if not s:
        return True
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc)
    except ValueError:
        return True


def main():
    all_events = {}
    for fetch in (fetch_doorkeeper, fetch_connpass):
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
