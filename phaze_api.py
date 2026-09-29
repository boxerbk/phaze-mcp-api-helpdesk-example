"""Thin client for the Phaze Enterprise API (control plane).

Docs: https://apidocs.phaze.app/
"""
import time
from typing import Iterator

import httpx

BASE_URL = "https://public-api.phaze.app/enterprise/v1"


class PhazeAPI:
    def __init__(self, api_key: str):
        self.http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )

    def _get(self, path: str, **params) -> dict:
        for attempt in range(3):
            r = self.http.get(path, params=params)
            if r.status_code == 429 and attempt < 2:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Phaze API {r.status_code} on {path}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"Phaze API rate limited on {path}")

    def _paginate(self, path: str, **params) -> Iterator[dict]:
        offset = 0
        while True:
            page = self._get(path, limit=200, offset=offset, **params)
            items = page.get("data", [])
            yield from items
            offset += len(items)
            if not items or offset >= page.get("count", 0):
                return

    def list_orgs(self) -> list[dict]:
        return list(self._paginate("/orgs"))

    def find_member(self, email: str) -> dict | None:
        email = email.lower()
        for m in self._paginate("/members", q=email):
            if str(m.get("email", "")).lower() == email:
                return m
        return None

    def machines_for_member(self, member: dict) -> list[dict]:
        """Machines directly assigned to this member, across the orgs they belong to.

        A machine has either an assignee_id or a group_id, never both. Group-assigned
        machines are deliberately excluded: the API doesn't expose group membership,
        and shared machines shouldn't be touched on one person's ticket anyway.
        """
        org_ids = [o["id"] for o in member.get("orgs", []) if o.get("id")]
        results = []
        for org_id in org_ids:
            for mach in self._paginate(f"/orgs/{org_id}/machines", user_id=member["id"]):
                if mach.get("assignee_id") == member["id"]:
                    results.append(mach)
        return results

    def member_label(self, user_id: str) -> str:
        """'Name <email>' for a Phaze user id (e.g. a guest's `user` in phaze_status)."""
        if not hasattr(self, "_members_by_id"):
            self._members_by_id = {m["id"]: m for m in self._paginate("/members") if m.get("id")}
        m = self._members_by_id.get(user_id)
        return f"{m.get('name') or m.get('email')} <{m.get('email')}>" if m else user_id

    def recent_connections(self, org_id: str, limit: int = 20) -> list[dict]:
        return self._get(f"/orgs/{org_id}/connections", limit=limit).get("data", [])
