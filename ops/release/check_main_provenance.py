"""Fail main CI when the pushed commit did not come from a merged PR to main."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def has_merged_main_pr(pulls: list[dict[str, Any]]) -> bool:
    return any(
        pr.get("merged_at")
        and isinstance(pr.get("base"), dict)
        and pr["base"].get("ref") == "main"
        for pr in pulls
    )


def fetch_associated_pulls(repository: str, sha: str, token: str) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repository}/commits/{sha}/pulls"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "neironych-main-provenance-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("github_provenance_lookup_failed") from exc

    if not isinstance(payload, list):
        raise RuntimeError("github_provenance_response_invalid")
    return [item for item in payload if isinstance(item, dict)]


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    ref = os.environ.get("GITHUB_REF", "")
    event = os.environ.get("GITHUB_EVENT_NAME", "")

    if event != "push" or ref != "refs/heads/main":
        return 0

    if not repository or not sha or not token:
        print("ERROR: missing GitHub context for main provenance check", file=sys.stderr)
        return 2

    try:
        pulls = fetch_associated_pulls(repository, sha, token)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if has_merged_main_pr(pulls):
        print(f"main provenance verified for {sha}: associated merged PR to main")
        return 0

    print(
        f"ERROR: main commit {sha} is not associated with a merged pull request to main; "
        "automated release is blocked",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
