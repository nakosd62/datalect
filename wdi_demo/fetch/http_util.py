"""Small shared HTTP helper: retries with backoff on transient failures."""

import time

import requests

import config


def get(url, params=None, **kwargs):
    last_exc = None
    for attempt in range(1, config.HTTP_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT_SECONDS, **kwargs)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                last_exc = RuntimeError(f"HTTP {resp.status_code} from {resp.url}")
            else:
                resp.raise_for_status()
        except (requests.ConnectionError, requests.Timeout, RuntimeError) as exc:
            last_exc = exc
        wait = config.HTTP_RETRY_BACKOFF_SECONDS * attempt
        print(f"    retrying ({attempt}/{config.HTTP_MAX_RETRIES}) after {last_exc}; sleeping {wait:.0f}s", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"Giving up on {url} after {config.HTTP_MAX_RETRIES} attempts: {last_exc}")
