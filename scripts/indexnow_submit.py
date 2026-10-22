#!/usr/bin/env python3
"""Tell IndexNow search engines (Bing, Yandex, Seznam, Naver) about our public URLs.

Run after shipping a new public page:
    .venv/bin/python scripts/indexnow_submit.py            # all sitemap pages
    .venv/bin/python scripts/indexnow_submit.py /vs/better-stack

Google does not use IndexNow; submit the sitemap in Search Console for Google.
Stdlib only, so it runs from any Python on any host.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.api.routes.seo import BASE, INDEXNOW_KEY, SITEMAP_PAGES  # noqa: E402

ENDPOINT = "https://api.indexnow.org/IndexNow"


def main(paths: list[str]) -> int:
    paths = paths or [p for p, _, _ in SITEMAP_PAGES]
    body = {
        "host": BASE.removeprefix("https://"),
        "key": INDEXNOW_KEY,
        "keyLocation": f"{BASE}/{INDEXNOW_KEY}.txt",
        "urlList": [BASE + p for p in paths],
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "checkpulse-indexnow/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(r.status, "accepted", len(body["urlList"]), "urls")
            return 0 if r.status in (200, 202) else 1
    except urllib.error.HTTPError as e:
        # 403 key not valid, 422 URL not on host, 429 too many requests
        print(e.code, e.read()[:300].decode("utf-8", "replace"))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
