"""Fallback channel: open a GitHub issue.

Needs no setup at all inside Actions -- ``GITHUB_TOKEN`` and
``GITHUB_REPOSITORY`` are already in the environment -- so a run with neither
LINE nor ntfy configured still leaves the alert somewhere visible instead of
discarding it. The GitHub mobile app turns it into a push notification too.
"""

from __future__ import annotations

import requests

from . import NotifyError

API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 20


class GitHubIssueNotifier:
    name = "github_issue"

    def __init__(self, token: str, repository: str, labels: tuple[str, ...] = ("flight-alert",)):
        if not token or not repository:
            raise NotifyError("GITHUB_TOKEN 或 GITHUB_REPOSITORY 是空的")
        self.token = token
        self.repository = repository
        self.labels = list(labels)

    def send(self, subject: str, body: str) -> None:
        url = f"{API_ROOT}/repos/{self.repository}/issues"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload: dict = {"title": subject, "body": f"```\n{body}\n```"}
        if self.labels:
            payload["labels"] = self.labels

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise NotifyError(f"GitHub issue 建立失敗: {exc}") from exc

        if response.status_code == 422 and self.labels:
            # Almost always a label that does not exist in the repo yet.
            # The alert matters more than the label, so retry without them.
            payload.pop("labels", None)
            response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)

        if response.status_code not in (200, 201):
            raise NotifyError(
                f"GitHub issue 建立失敗 (HTTP {response.status_code}): {response.text[:300]}"
            )
