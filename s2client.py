"""
Shared Semantic Scholar API client helper. Works WITH & WITHOUT an API key against the public Academic Graph API.

Key facts about keyless access:
  * Base URL: https://api.semanticscholar.org/graph/v1
  * No key required. Unauthenticated callers share one global pool, so you
    will hit HTTP 429 (Too Many Requests) under load. The practical keyless
    budget is roughly 100 requests / 5 minutes.
  * The fix for 429s is polite pacing + exponential backoff, both built in
    here. For heavy work, request a free key and set S2_API_KEY.

If you DO have a key, export it and this client will send it automatically:
    export S2_API_KEY="your-key"
"""

from __future__ import annotations

import json
import os
import time
import dotenv
dotenv.load_dotenv()

import requests

GRAPH_BASE = "https://api.semanticscholar.org/graph/v1"
REC_BASE = "https://api.semanticscholar.org/recommendations/v1"

USER_AGENT = "s2-citation-graph-samples/1.1 (educational)"
# The keyless pool is shared globally and can be very tight. Pace politely and
# back off patiently. Override via env if you have a key / want to go faster.
DEFAULT_PAUSE = float(os.environ.get("S2_PAUSE", "1.0"))  # seconds between calls
MAX_BACKOFF = 16  # cap for a single backoff sleep

# One shared Session -> connection pooling + persistent headers.
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
if os.environ.get("S2_API_KEY"):
    _session.headers["x-api-key"] = os.environ["S2_API_KEY"]  # header is case-sensitive


def _pause() -> None:
    time.sleep(DEFAULT_PAUSE)


def _request(method: str, url: str, params: dict | None = None,
             body: dict | None = None, max_retries: int = 8) -> dict | list:
    """Perform an HTTP request with exponential backoff on 429/5xx."""
    attempt = 0
    while True:
        try:
            resp = _session.request(method, url, params=params, json=body, timeout=30)
        except requests.RequestException as e:
            if attempt < max_retries:
                wait = min(2 ** attempt, MAX_BACKOFF)
                print(f"  [network error {e}] retrying in {wait}s")
                time.sleep(wait)
                attempt += 1
                continue
            raise

        if resp.status_code == 200:
            return resp.json()

        # 429 = rate limited, 5xx = transient server error -> retry
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            # Exponential backoff
            wait = min(2 ** attempt, MAX_BACKOFF)
            print(f"  [{resp.status_code}]: backing off for {wait}s "
                  f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            attempt += 1
            continue

        raise RuntimeError(f"HTTP {resp.status_code} for {resp.url}\n{resp.text}")


def _clean(params: dict | None) -> dict | None:
    """Drop None/False; turn True into '' for valueless flags (openAccessPdf)."""
    if not params:
        return None
    return {k: ("" if v is True else v)
            for k, v in params.items() if v is not None and v is not False}


def get(path: str, params: dict | None = None, base: str = GRAPH_BASE):
    """GET a graph endpoint. `path` starts with '/'."""
    _pause()
    return _request("GET", f"{base}{path}", params=_clean(params))


def post(path: str, ids: list[str], params: dict | None = None,
         base: str = GRAPH_BASE):
    """POST to a batch endpoint. `fields` go in the query string, ids in body."""
    _pause()
    return _request("POST", f"{base}{path}", params=_clean(params),
                    body={"ids": ids})


def pretty(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def show(title: str, obj) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    print(pretty(obj))
