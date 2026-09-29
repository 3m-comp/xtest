"""X / RSS(Bluesky・Mastodon・YouTubeなど)の新着を検知してDiscordに投稿する。
GitHub Actionsから5分おきに実行される想定。外部ライブラリ不要(Python標準のみ)。"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

SOURCES_FILE = "sources.json"
STATE_FILE = "state.json"
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
INCLUDE_REPOSTS = False   # Xのリポストも通知するなら True
INCLUDE_REPLIES = False   # Xの返信も通知するなら True
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def warn(msg):
    print(f"::warning::{msg}")


def http_get(url, retries=3):
    """GET。429や5xxのときは待って再試行する。"""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise
        except urllib.error.URLError:
            if i < retries - 1:
                time.sleep(5)
                continue
            raise


def post_discord(channel_id, content):
    for _ in range(3):
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=json.dumps({"content": content}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bot {BOT_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com, 1.0)",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait)
                continue
            raise RuntimeError(f"Discord HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    raise RuntimeError("Discord rate limited")


def fetch_x(handle):
    """古い順の [{id, url}]"""
    html = http_get(f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError(f"__NEXT_DATA__ not found (html={len(html)} bytes)")
    entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"].get("entries") or []
    ids = set()
    for e in entries:
        t = (e.get("content") or {}).get("tweet")
        if not t:
            continue
        if t.get("retweeted_status") is not None and not INCLUDE_REPOSTS:
            continue
        if t.get("in_reply_to_status_id_str") and not INCLUDE_REPLIES:
            continue
        ids.add(int(t["id_str"]))
    print(f"  raw entries={len(entries)} kept={len(ids)}")
    return [{"id": str(i), "url": f"https://x.com/{handle}/status/{i}"} for i in sorted(ids)]


def _local(tag):
    return tag.split("}", 1)[-1]


def fetch_rss(url):
    """RSS 2.0 / Atom 両対応。古い順の [{id, url}]"""
    root = ET.fromstring(http_get(url))
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = gid = None
        for c in el:
            name = _local(c.tag)
            if name == "link":
                if c.get("href"):  # Atom
                    if c.get("rel", "alternate") == "alternate":
                        link = c.get("href")
                elif (c.text or "").strip():  # RSS
                    link = c.text.strip()
            elif name in ("guid", "id"):
                gid = (c.text or "").strip()
        if link or gid:
            items.append({"id": gid or link, "url": link or gid})
    items.reverse()  # フィードは新しい順が多いので古い順にする
    print(f"  items={len(items)}")
    return items


def main():
    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}

    for s in sources:
        kind, target, channel = s["type"], s["target"], str(s["channel_id"])
        key = f"{kind}:{target}@{channel}"
        print(f"[{key}]")
        try:
            items = fetch_x(target.lstrip("@")) if kind == "x" else fetch_rss(target)
        except Exception as e:
            warn(f"{key} 取得失敗: {e}")
            continue
        if kind == "x":
            time.sleep(2)  # Xへのアクセス間隔をあける

        st = state.get(key)
        if kind == "x":
            if not st:
                if items:
                    state[key] = {"last": items[-1]["id"]}
                    print(f"  initialized at {items[-1]['id']}")
                else:
                    warn(f"{key} 取得結果が空のため基準を記録できませんでした")
                continue
            new = [i for i in items if int(i["id"]) > int(st["last"])]
            for i in new:
                try:
                    post_discord(channel, i["url"])
                except Exception as e:
                    warn(f"{key} Discord投稿失敗: {e}")
                    break
                st["last"] = i["id"]
                print(f"  posted {i['url']}")
        else:
            if not st:
                state[key] = {"seen": [i["id"] for i in items][-200:]}
                print(f"  initialized ({len(items)} items)")
                continue
            seen = st["seen"]
            for i in [i for i in items if i["id"] not in seen]:
                try:
                    post_discord(channel, i["url"])
                except Exception as e:
                    warn(f"{key} Discord投稿失敗: {e}")
                    break
                seen.append(i["id"])
                print(f"  posted {i['url']}")
            st["seen"] = seen[-200:]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
