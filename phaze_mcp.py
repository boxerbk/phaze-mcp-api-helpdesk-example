"""Direct client for the local Phaze MCP (the same server the agent uses).

The orchestrator uses this to watch sessions and move control deterministically:
detect a technician joining, hand them control, take it back on handback, and clean up.
The server speaks plain JSON-RPC over HTTP, so no MCP SDK is needed.
"""
import asyncio
import itertools
import json

import httpx


class PhazeMCPError(RuntimeError):
    pass


# The Phaze app answers this while its main loop is busy (e.g. a connection is still landing).
TRANSIENT = ("timed out",)


class PhazeMCP:
    def __init__(self, url: str):
        self.http = httpx.AsyncClient(
            timeout=30,
            headers={"Accept": "application/json, text/event-stream"},
        )
        self.url = url
        self._ids = itertools.count(1)

    async def call(self, name: str, retries: int = 4, **arguments) -> dict:
        """Call a tool, retrying transient failures with backoff."""
        for attempt in range(retries + 1):
            try:
                return await self._call_once(name, arguments)
            except (PhazeMCPError, httpx.TransportError) as e:
                transient = isinstance(e, httpx.TransportError) or any(t in str(e) for t in TRANSIENT)
                if not transient or attempt == retries:
                    raise
                await asyncio.sleep(2 * (attempt + 1))

    async def _call_once(self, name: str, arguments: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        r = await self.http.post(self.url, json=body)
        r.raise_for_status()
        msg = r.json()
        if "error" in msg:
            raise PhazeMCPError(f"{name}: {msg['error']}")
        result = msg.get("result", {})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise PhazeMCPError(f"{name}: {text}")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"text": text}

    async def status(self) -> dict:
        return await self.call("phaze_status")

    async def set_control(self, connection_id: str, guest_id: int) -> dict:
        return await self.call("phaze_set_control", connection_id=connection_id, guest_id=guest_id)

    async def disconnect(self, connection_id: str) -> dict:
        return await self.call("phaze_disconnect", connection_id=connection_id)

    async def aclose(self):
        await self.http.aclose()


# ---- helpers over a phaze_status payload ----

def find_connection(status: dict, connection_id: str) -> dict | None:
    return next((c for c in status.get("connections", []) if c.get("connection_id") == connection_id), None)


def self_guest(conn: dict) -> dict | None:
    return next((g for g in conn.get("guests", []) if g.get("is_self")), None)


def controller(conn: dict) -> dict | None:
    return next((g for g in conn.get("guests", []) if g.get("has_control")), None)
