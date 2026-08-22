"""Minimal GitHub REST API client (stdlib only)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import Config
from .util import SetupError

API_ROOT = "https://api.github.com"


def _request(method: str, path: str, pat: str | None):
    url = f"{API_ROOT}/{path}"
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ci-runner-setup",
            },
        )
        if pat:
            request.add_header("Authorization", f"Bearer {pat}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            if e.code >= 500 and attempt < 2:
                last_error = e
                time.sleep(2**attempt)
                continue
            hint = ""
            if e.code in (401, 403):
                hint = " (is the PAT valid and does it have admin rights on the runner scope?)"
            if e.code == 404:
                hint = " (does the owner/repo exist and can the PAT see it?)"
            raise SetupError(f"GitHub API {method} {path}: HTTP {e.code}{hint} {body}") from e
        except urllib.error.URLError as e:
            last_error = e
            time.sleep(2**attempt)
    raise SetupError(f"GitHub API {method} {path} failed: {last_error}")


def get(path: str, pat: str | None = None):
    return _request("GET", path, pat)


def post(path: str, pat: str | None = None):
    return _request("POST", path, pat)


def latest_runner_release(pat: str | None = None) -> dict:
    return get("repos/actions/runner/releases/latest", pat)


def registration_token(cfg: Config) -> str:
    data = post(f"{cfg.api_base}/actions/runners/registration-token", cfg.pat)
    token = data.get("token")
    if not token:
        raise SetupError("GitHub API returned no registration token")
    return token


def removal_token(cfg: Config) -> str:
    data = post(f"{cfg.api_base}/actions/runners/remove-token", cfg.pat)
    token = data.get("token")
    if not token:
        raise SetupError("GitHub API returned no removal token")
    return token
