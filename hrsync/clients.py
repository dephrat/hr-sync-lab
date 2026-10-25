"""HTTP clients for Workday and Ethena, behind small Protocols the sync engine depends on."""
from typing import Protocol

import httpx


class HrSyncClientError(Exception):
    """Base for errors the clients raise deliberately."""


class RateLimited(HrSyncClientError):
    def __init__(self, retry_after: float | None = None):
        super().__init__(f"rate limited (retry after {retry_after})")
        self.retry_after = retry_after


class ClientError(HrSyncClientError):
    def __init__(self, status: int, title: str):
        super().__init__(f"{status} {title}")
        self.status = status
        self.title = title


class HrisClient(Protocol):
    def get_worker(self, worker_id: str) -> dict: ...
    def job_changes_since(self, completed_from: str) -> list[dict]: ...
    def get_job_change(self, change_id: str) -> dict: ...


class LearningClient(Protocol):
    def find_learners_by_email(self, email: str) -> list[dict]: ...
    def get_learner(self, learner_id: str) -> dict: ...
    def create_learner(self, body: dict) -> str: ...
    def patch_learner(self, learner_id: str, body: dict) -> None: ...
    def campaigns_for_learner(self, learner_id: str) -> list[dict]: ...
    def list_campaigns(self) -> list[dict]: ...


def _send(http: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    resp = http.request(method, url, **kw)
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        try:
            retry_after = float(ra) if ra is not None else None
        except ValueError:
            retry_after = None
        raise RateLimited(retry_after)
    if 400 <= resp.status_code < 500:
        try:
            title = resp.json().get("title", resp.reason_phrase)
        except (ValueError, AttributeError):
            title = resp.reason_phrase
        raise ClientError(resp.status_code, title)
    resp.raise_for_status()
    return resp


class WorkdayClient:
    PAGE = 100

    def __init__(self, http: httpx.Client):
        self.http = http

    def get_worker(self, worker_id: str) -> dict:
        return _send(self.http, "GET", f"/workers/{worker_id}").json()

    def get_job_change(self, change_id: str) -> dict:
        return _send(self.http, "GET", f"/jobChanges/{change_id}").json()

    def pending_changes(self, worker_id: str) -> list[dict]:
        """Job changes still waiting for approval for one worker.

        Reads the first 100 pending changes only: plenty for the demo, a real
        tenant would page."""
        body = _send(self.http, "GET", "/jobChanges",
                     params={"status": "pending", "limit": 100}).json()
        return [c for c in body["data"] if c["worker"]["id"] == worker_id]

    def list_workers(self) -> list[dict]:
        out, offset = [], 0
        while True:
            body = _send(self.http, "GET", "/workers",
                         params={"limit": self.PAGE, "offset": offset}).json()
            out += body["data"]
            offset += len(body["data"])
            if len(body["data"]) < self.PAGE or offset >= body["total"]:
                return out

    def job_changes_since(self, completed_from: str) -> list[dict]:
        out, offset = [], 0
        while True:
            body = _send(self.http, "GET", "/jobChanges", params={
                "completedFrom": completed_from, "limit": self.PAGE, "offset": offset}).json()
            out += body["data"]
            offset += len(body["data"])
            if len(body["data"]) < self.PAGE or offset >= body["total"]:
                return out


class EthenaClient:
    PAGE = 100

    def __init__(self, http: httpx.Client):
        self.http = http

    def _list(self, path: str, **params) -> list[dict]:
        out, cursor = [], None
        while True:
            q = {**params, "limit": self.PAGE}
            if cursor:
                q["cursor"] = cursor
            body = _send(self.http, "GET", f"/v1{path}", params=q).json()
            out += body["data"]
            if not body["hasMore"] or not body["data"]:
                return out
            cursor = body["data"][-1]["id"]

    def find_learners_by_email(self, email: str) -> list[dict]:
        return self._list("/learners", email=email)

    def list_learners(self) -> list[dict]:
        return self._list("/learners")

    def list_all_enrollments(self) -> list[dict]:
        return self._list("/learner-training-campaigns")

    def get_learner(self, learner_id: str) -> dict:
        return _send(self.http, "GET", f"/v1/learners/{learner_id}").json()

    def create_learner(self, body: dict) -> str:
        return _send(self.http, "POST", "/v1/learners", json=body).json()["id"]

    def patch_learner(self, learner_id: str, body: dict) -> None:
        _send(self.http, "PATCH", f"/v1/learners/{learner_id}", json={**body, "id": learner_id})

    def list_campaigns(self) -> list[dict]:
        return self._list("/training-campaigns")

    def campaigns_for_learner(self, learner_id: str, include_removed: bool = False) -> list[dict]:
        """The learner's courses, without REMOVED ones unless asked: a removed enrollment is a
        record of a course taken away, never a course the person holds. Displays that show the
        full record pass include_removed=True."""
        camps = {c["id"]: c for c in self.list_campaigns()}
        out = []
        for ltc in self._list("/learner-training-campaigns", learnerId=learner_id):
            if ltc.get("completionStatus") == "REMOVED" and not include_removed:
                continue
            c = camps.get(ltc["trainingCampaignId"], {})
            out.append({**ltc, "courseName": c.get("courseName"),
                        "campaignName": c.get("campaignName")})
        return out
