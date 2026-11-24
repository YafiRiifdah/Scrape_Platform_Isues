"""Server Apify tiruan untuk test. Setiap token adalah akun terpisah dengan kuota dan run sendiri."""

from __future__ import annotations

import re
from typing import Any, Optional


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: Optional[str] = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("bukan JSON")
        return self._payload


def _error(status: int, kind: str, message: str) -> FakeResponse:
    return FakeResponse(status, {"error": {"type": kind, "message": message}}, text=f'{{"error":{{"type":"{kind}","message":"{message}"}}}}')


class FakeApify:
    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}  # token -> akun
        self.items: dict[str, list[dict[str, Any]]] = {}  # actor -> isi dataset
        self.status_sequence: list[str] = ["RUNNING", "SUCCEEDED"]  # status run, maju satu tiap dipantau
        self.runs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Optional[str]]] = []  # (metode, url, token)
        self.started: list[tuple[str, str, dict[str, Any]]] = []  # (token, actor, payload)
        self.aborted: list[str] = []

    def add_account(self, token: str, username: str, used: float = 0.0, limit: float = 5.0, **flags: Any) -> None:
        """flags: no_credits, quota_exceeded, forbidden, rate_limited."""
        self.accounts[token] = {"id": f"id-{username}", "username": username, "used": used, "limit": limit, **flags}

    # ---- pembantu untuk asersi -----------------------------------------------------
    def tokens_used(self) -> set[Optional[str]]:
        return {token for _, _, token in self.calls}

    def actor_runs_by(self, token: str) -> list[str]:
        return [actor for t, actor, _ in self.started if t == token]

    # ---- HTTP ------------------------------------------------------------------------
    def _auth(self, method: str, url: str, headers: Optional[dict[str, str]]):
        value = (headers or {}).get("Authorization", "")
        token = value[len("Bearer "):] if value.startswith("Bearer ") else None
        self.calls.append((method, url, token))
        account = self.accounts.get(token or "")
        if account is None:
            return token, None, _error(401, "user-or-token-not-found", "User was not found or authentication token is not valid")
        return token, account, None

    def post(self, url: str, headers=None, json=None, params=None, timeout=None):
        token, account, denied = self._auth("POST", url, headers)
        if denied:
            return denied
        m = re.search(r"/acts/([^/]+)/runs$", url)
        if m:
            if account.get("rate_limited"):
                return _error(429, "rate-limit-exceeded", "You have exceeded the rate limit")
            if account.get("no_credits"):
                return _error(402, "insufficient-funds", "Insufficient funds")
            if account.get("quota_exceeded"):
                return _error(403, "platform-feature-disabled", "Monthly usage hard limit exceeded")
            if account.get("forbidden"):
                return _error(403, "insufficient-permissions", "Insufficient permissions for the Actor")
            run_id = f"run{len(self.runs) + 1}"
            self.runs[run_id] = {"token": token, "actor": m.group(1), "step": 0}
            self.started.append((token, m.group(1), json or {}))
            return FakeResponse(201, {"data": self._run_view(run_id)})
        m = re.search(r"/actor-runs/([^/]+)/abort$", url)
        if m:
            self.aborted.append(m.group(1))
            return FakeResponse(200, {"data": {"id": m.group(1), "status": "ABORTING"}})
        return _error(404, "not-found", "unknown endpoint")

    def get(self, url: str, headers=None, params=None, timeout=None):
        token, account, denied = self._auth("GET", url, headers)
        if denied:
            return denied
        if url.endswith("/users/me"):
            return FakeResponse(200, {"data": {"id": account["id"], "username": account["username"]}})
        if url.endswith("/users/me/limits"):
            return FakeResponse(
                200,
                {
                    "data": {
                        "current": {"monthlyUsageUsd": account["used"]},
                        "limits": {"maxMonthlyUsageUsd": account["limit"]},
                        "monthlyUsageCycle": {"endAt": "2026-11-30T23:59:59.999Z"},
                    }
                },
            )
        m = re.search(r"/actor-runs/([^/]+)$", url)
        if m:
            run = self.runs.get(m.group(1))
            if run is None or run["token"] != token:  # run milik akun lain tidak terlihat
                return _error(404, "record-not-found", "Actor run was not found")
            run["step"] = min(run["step"] + 1, len(self.status_sequence) - 1)
            return FakeResponse(200, {"data": self._run_view(m.group(1))})
        m = re.search(r"/datasets/ds-([^/]+)/items$", url)
        if m:
            run = self.runs.get(m.group(1))
            if run is None or run["token"] != token:
                return _error(404, "record-not-found", "Dataset was not found")
            return FakeResponse(200, list(self.items.get(run["actor"], [])))
        return _error(404, "not-found", "unknown endpoint")

    def _run_view(self, run_id: str) -> dict[str, Any]:
        run = self.runs[run_id]
        status = self.status_sequence[min(run["step"], len(self.status_sequence) - 1)]
        return {
            "id": run_id, "actId": run["actor"], "status": status, "defaultDatasetId": f"ds-{run_id}",
            "usageTotalUsd": 0.0125 if status == "SUCCEEDED" else None, "statusMessage": f"status {status}",
        }
