"""Offline tests for the orchestration: no Slack, Phaze, or Claude calls.

    python tests/test_flow.py

Fakes stand in for the Phaze MCP (guests, control), Slack, and the Enterprise API, so this
checks the state machine: guardrails, machine picker, ask / handoff / takeover / handback.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent
from agent import HelpdeskRun
from ticketing import Ticket, Signal, parse_command

agent.POLL_SECONDS = 0.02
agent.CAPTURE_POLL_SECONDS = 0.01
agent.CAPTURE_WARMUP_SECONDS = 0.05
agent.SIGNIN_POLL_SECONDS = 0.01

# ---- parse_command ----
cases = {"back try restarting the spooler": ("handback", "try restarting the spooler"),
         "<@U1> takeover": ("takeover", ""), "Hand back: go ahead": ("handback", "go ahead"),
         "resolved, it was the cable": ("resolved", "it was the cable"), "close": ("close", ""),
         "the HP one on floor 2": ("reply", "the HP one on floor 2"), "backup drive?": ("reply", "backup drive?")}
for t, exp in cases.items():
    s = parse_command(t, "U1"); assert (s.kind, s.text) == exp, (t, s)
print("parse_command ok")

class Cfg:
    human_join_timeout_min = 1; human_reply_timeout_min = 1; human_session_timeout_min = 1
    max_handoffs = 3; max_turns = 5; model = "x"; phaze_mcp_url = "http://x"

SELF, TECH, REQ = 11, 22, "u_req"

class FakePhaze:
    def __init__(self):
        self.guests = [{"guest_id": SELF, "user": "u_agent", "has_control": True, "is_self": True}]
        self.calls = []; self.connected = True
    def conn(self):
        return {"connection_id": "c1", "owner": "agent", "host_machine_id": "m1", "guests": self.guests}
    async def status(self):
        conns = [{"connection_id": "old", "owner": "agent", "host_machine_id": "m1", "guests": []}]
        if self.connected: conns.append(self.conn())
        conns.append({"connection_id": "u1", "owner": "user", "host_machine_id": "m1", "guests": []})
        return {"connections": conns}
    async def set_control(self, cid, gid):
        self.calls.append(("set_control", cid, gid))
        for g in self.guests: g["has_control"] = (g["guest_id"] == gid)
    async def disconnect(self, cid):
        self.calls.append(("disconnect", cid)); self.connected = False
    async def call(self, name, **a):
        return {}   # screenshots succeed immediately in the basic fake

class FakeAPI:
    def find_member(self, e): return {"id": REQ}
    def machines_for_member(self, m): return [{"id": "m1", "name": "demo"}]
    def member_label(self, uid): return f"Tech<{uid}>"

class FakeSlack:
    def __init__(self): self.posts = []
    def __getattr__(self, name):
        if name in ("add_internal_note", "ask", "escalate", "ask_requester_machine", "requester_hint", "machine_chosen", "ask_requester_sign_in", "requester_signed_in", "announce"):
            return lambda t, text=None, *rest: self.posts.append((name, text, *rest))
        raise AttributeError(name)
    def user_label(self, u): return f"Slack<{u}>"

def mk(observe=False):
    t = Ticket("C:1", "C", "1", "a@b", "Ann", "printer", "it's broken", "http://t")
    ph = FakePhaze()
    r = HelpdeskRun(Cfg, t, FakeSlack(), FakeAPI(), ph, observe_only=observe)
    return r, ph

def pre(tool, **args): return {"tool_name": f"mcp__phaze__{tool}", "tool_input": args}
denied = lambda res: res.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

async def test_guards():
    r, ph = mk()
    assert denied(await r._guard(pre("phaze_connect", machine_id="m1"), None, None))  # before lookup
    r.allowed_machines = {"m1": "demo"}
    assert denied(await r._guard(pre("phaze_connect", machine_id="m2"), None, None))
    assert not denied(await r._guard(pre("phaze_connect", machine_id="m1"), None, None))
    assert denied(await r._guard(pre("phaze_screenshot", connection_id="u1"), None, None))  # user session
    assert not denied(await r._guard(pre("phaze_screenshot", connection_id="c1"), None, None))
    assert denied(await r._guard(pre("phaze_set_control", connection_id="c1", guest_id=TECH), None, None))
    ph.guests.append({"guest_id": TECH, "user": "u_tech", "has_control": False, "is_self": False})
    await ph.set_control("c1", TECH)
    assert denied(await r._guard(pre("phaze_set_control", connection_id="c1", guest_id=SELF), None, None))  # human holds it
    r.pending = ("ask", "q")
    assert denied(await r._guard({"tool_name": "mcp__helpdesk__post_ticket_note", "tool_input": {}}, None, None))
    r2, _ = mk(observe=True); r2.allowed_machines = {"m1": "demo"}
    assert denied(await r2._guard(pre("phaze_set_control", connection_id="c1", guest_id=SELF), None, None))
    assert not denied(await r2._guard(pre("phaze_set_control", connection_id="c1", guest_id=0), None, None))
    print("guards ok")

async def test_handoff_then_phaze_handback():
    r, ph = mk(); r.allowed_machines = {"m1": "demo"}; r.machine_id = "m1"; r.requester_member_id = REQ
    r.preexisting = {"old", "u1"}
    # the agent account's OTHER (leftover) connection to this host is already a guest at page time
    ph.guests.append({"guest_id": 44, "user": "u_agent", "has_control": False, "is_self": False})
    task = asyncio.create_task(r._handoff("stuck on UAC", allow_handback=True))
    await asyncio.sleep(0.1)
    assert ("set_control", "c1", 0) in ph.calls, ph.calls           # released before paging
    assert r.slack.posts[-1][0] == "escalate" and r.slack.posts[-1][2] == "m1"   # connect link target
    assert "Connect in Phaze" in r.slack.posts[-1][1]
    # the requester joining as a guest must NOT be treated as the technician
    ph.guests.append({"guest_id": 33, "user": REQ, "has_control": False, "is_self": False})
    await asyncio.sleep(0.1); assert not any(c[0]=="set_control" and c[2] in (33, 44) for c in ph.calls)
    ph.guests.append({"guest_id": TECH, "user": "u_tech", "has_control": False, "is_self": False})
    await asyncio.sleep(0.1)
    assert ("set_control", "c1", TECH) in ph.calls
    r.inbox.put_nowait(Signal("reply", "hmm", "U9")); await asyncio.sleep(0.1)
    assert not task.done()
    await ph.set_control("c1", SELF)   # tech gives control to the agent inside Phaze
    sig = await asyncio.wait_for(task, 2)
    assert sig.kind == "handback" and sig.user == "u_tech", sig
    prompt = await r._take_back(sig)
    assert "You have control again" in prompt, prompt
    print("handoff -> tech joins -> phaze handback ok")

async def test_slack_handback_and_resolve():
    r, ph = mk(); r.allowed_machines = {"m1": "demo"}; r.machine_id = "m1"; r.requester_member_id = REQ
    task = asyncio.create_task(r._handoff("x", allow_handback=True))
    await asyncio.sleep(0.05)
    ph.guests.append({"guest_id": TECH, "user": "u_tech", "has_control": False, "is_self": False})
    await asyncio.sleep(0.1)
    r.inbox.put_nowait(Signal("handback", "clear the print queue then retry", "U7"))
    sig = await asyncio.wait_for(task, 2); assert sig.kind == "handback"
    prompt = await r._take_back(sig)
    assert ("set_control", "c1", SELF) in ph.calls and "clear the print queue" in prompt
    # second handoff ends with resolved
    task = asyncio.create_task(r._handoff("y", allow_handback=True))
    await asyncio.sleep(0.05); r.inbox.put_nowait(Signal("resolved", "done", "U7"))
    assert await asyncio.wait_for(task, 2) is None and r.outcome == "resolved"
    await r._cleanup()
    assert ph.calls[-1] == ("disconnect", "c1")
    print("slack handback + resolved + cleanup ok")

async def test_solo_demo_same_account():
    """Requester, agent and technician are all one Phaze account (a sales demo)."""
    r, ph = mk(); r.allowed_machines = {"m1": "demo"}; r.machine_id = "m1"; r.requester_member_id = "u_agent"
    ph.guests.append({"guest_id": 44, "user": "u_agent", "has_control": False, "is_self": False})  # leftover
    task = asyncio.create_task(r._handoff("x", allow_handback=True))
    await asyncio.sleep(0.1); assert not any(c[0] == "set_control" and c[2] == 44 for c in ph.calls)
    ph.guests.append({"guest_id": 55, "user": "u_agent", "has_control": False, "is_self": False})  # you, clicking Connect
    await asyncio.sleep(0.1); assert ("set_control", "c1", 55) in ph.calls, ph.calls
    await ph.set_control("c1", SELF)   # hand back inside Phaze
    sig = await asyncio.wait_for(task, 2); assert sig.kind == "handback"
    print("solo demo (same account) ok")

class CapPhaze(FakePhaze):
    """Screenshots fail with a given error for the first N calls per connection."""
    def __init__(self, fails: dict):
        super().__init__(); self.fails = dict(fails)
    async def call(self, name, **a):
        self.calls.append((name, a.get("connection_id")))
        if name == "phaze_screenshot":
            n, err = self.fails.get(a["connection_id"], (0, ""))
            if n > 0:
                self.fails[a["connection_id"]] = (n - 1, err)
                from phaze_mcp import PhazeMCPError
                raise PhazeMCPError(f"phaze_screenshot: {err}")
        return {}

async def test_screen_capture():
    shot = pre("phaze_screenshot", connection_id="c1")
    reason = lambda res: res["hookSpecificOutput"]["permissionDecisionReason"]
    # warm-up: "output is not ready" twice, then frames arrive; the agent's screenshot goes through
    r, _ = mk(); r.phaze = ph = CapPhaze({"c1": (2, "output is not ready")})
    r.allowed_machines = {"m1": "demo"}; r.machine_id = "m1"
    assert not denied(await r._guard(shot, None, None)) and "c1" in r.capturing
    assert not denied(await r._guard(shot, None, None))              # checked once per connection
    assert sum(1 for c in ph.calls if c[0] == "phaze_screenshot") == 3
    # sign-in screen: requester is asked to sign in, agent is told to end its turn, no reconnect
    r, _ = mk(); r.phaze = ph = CapPhaze({"c1": (10**6, "connection is not capturing: c1")})
    r.allowed_machines = {"m1": "demo"}; r.machine_id = "m1"
    res = await r._guard(shot, None, None)
    assert denied(res) and "sign-in" in reason(res) and r.pending == ("signin", "c1")
    assert r.slack.posts[0] == ("ask_requester_sign_in", "demo")
    notice = next(p[1] for p in r.slack.posts if p[0] == "announce")   # visible in the escalation channel
    assert ":lock:" in notice and "password is needed" in notice and "sign-in" in notice
    # if nobody signs in, the handoff page leads with the password line
    task = asyncio.create_task(r._handoff("stayed locked", allow_handback=True)); await asyncio.sleep(0.05)
    page = [p for p in r.slack.posts if p[0] == "escalate"][-1][1]
    assert page.splitlines()[1].startswith(":lock: *Needs a password:*") and "sign-in / lock screen" in page, page
    r.inbox.put_nowait(Signal("close", "", "U7")); await asyncio.wait_for(task, 2); r.pending = ("signin", "c1")
    assert not any(c[0] in ("phaze_connect", "disconnect") for c in ph.calls)
    assert denied(await r._guard(pre("phaze_status"), None, None))   # everything blocked until resumed
    # requester signs in after a few polls -> screen visible -> resumed
    ph.fails["c1"] = (2, "connection is not capturing")
    ok, sig = await asyncio.wait_for(r._wait_signed_in("c1"), 2)
    assert ok and sig is None and ("requester_signed_in", True) in r.slack.posts
    # a technician can step in while waiting
    ph.fails["c1"] = (10**6, "connection is not capturing")
    t = asyncio.create_task(r._wait_signed_in("c1")); r.inbox.put_nowait(Signal("takeover", "", "U7"))
    ok, sig = await asyncio.wait_for(t, 2); assert not ok and sig.kind == "takeover"
    # some other screenshot error: hand off rather than wait
    r, _ = mk(); r.phaze = CapPhaze({"c1": (10**6, "boom")}); r.allowed_machines = {"m1": "demo"}
    assert "request_handoff" in reason(await r._guard(shot, None, None))
    # agent stops at a UAC / password prompt mid-task -> page says a password is needed
    r, _ = mk(); tools = {tl.name: tl for tl in r._tools()}
    await tools["request_handoff"].handler({"summary": "UAC prompt for driver install", "password_required": True})
    task = asyncio.create_task(r._handoff(r.pending[1], allow_handback=True)); await asyncio.sleep(0.05)
    page = [p for p in r.slack.posts if p[0] == "escalate"][-1][1]
    assert ":lock: *Needs a password:*" in page and "UAC" in page, page
    r.inbox.put_nowait(Signal("close", "", "U7")); await asyncio.wait_for(task, 2)
    # without the flag, no password line
    r, _ = mk(); tools = {tl.name: tl for tl in r._tools()}
    await tools["request_handoff"].handler({"summary": "stuck", "password_required": False})
    task = asyncio.create_task(r._handoff(r.pending[1], allow_handback=True)); await asyncio.sleep(0.05)
    assert ":lock:" not in [p for p in r.slack.posts if p[0] == "escalate"][-1][1]
    r.inbox.put_nowait(Signal("close", "", "U7")); await asyncio.wait_for(task, 2)
    print("screen capture: warm-up, sign-in screen, resume, password notices ok")

async def test_run_with_signin_pause():
    class DummyClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    agent.ClaudeSDKClient = DummyClient
    r, ph = mk(); ph.connected = False; r.options = lambda: None
    r.phaze = cp = CapPhaze({}); cp.connected = False
    r.allowed_machines = {"m1": "demo"}; r.machine_id = "m1"
    steps = iter(["locked", "resolve"]); prompts = []
    async def fake_converse(client, prompt, first):
        prompts.append(prompt); r.pending = None; cp.connected = True
        if next(steps) == "locked":
            cp.fails["c1"] = (10**6, "connection is not capturing")   # locked...
            await r._guard(pre("phaze_screenshot", connection_id="c1"), None, None)
            assert r.pending == ("signin", "c1")
            cp.fails["c1"] = (3, "connection is not capturing")       # ...signs in after ~3 polls
        else:
            r.outcome = "resolved"
    r._converse = fake_converse
    assert await asyncio.wait_for(r.run(), 5) == "resolved"
    assert "signed in" in prompts[1], prompts
    print("run with sign-in pause ok")

async def test_join_timeout():
    r, ph = mk(); r.machine_id = "m1"; r.allowed_machines = {"m1": "demo"}
    Cfg.human_join_timeout_min = 0.002  # ~0.1s
    sig = await asyncio.wait_for(r._handoff("x", True), 2)
    assert sig is None and "Nobody joined" in r.slack.posts[-1][1]
    Cfg.human_join_timeout_min = 1
    print("join timeout ok")

async def test_run_orchestration():
    """Full run(): agent asks -> tech answers -> agent hands off -> tech hands back -> agent resolves."""
    class DummyClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    agent.ClaudeSDKClient = DummyClient
    r, ph = mk(); r.machine_id = "m1"; r.allowed_machines = {"m1": "demo"}; r.requester_member_id = REQ
    r.options = lambda: None
    ph.connected = False   # the agent opens c1 during its first turn
    script = iter(["ask", "handoff", "resolve"]); prompts = []
    async def fake_converse(client, prompt, first):
        prompts.append(prompt); r.pending = None; step = next(script)
        ph.connected = True
        if step == "ask":
            r.pending = ("ask", "which printer?")
            asyncio.get_running_loop().call_later(0.05, r.inbox.put_nowait, Signal("reply", "HP floor 2", "U7"))
        elif step == "handoff":
            r.pending = ("handoff", "needs admin")
            async def tech():
                await asyncio.sleep(0.1)
                ph.guests.append({"guest_id": TECH, "user": "u_tech", "has_control": False, "is_self": False})
                await asyncio.sleep(0.1); r.inbox.put_nowait(Signal("handback", "driver installed", "U7"))
            asyncio.create_task(tech())
        else:
            r.outcome = "resolved"
    r._converse = fake_converse
    out = await asyncio.wait_for(r.run(), 5)
    assert out == "resolved", out
    assert "HP floor 2" in prompts[1] and "driver installed" in prompts[2], prompts
    assert ("disconnect", "c1") in ph.calls and ("disconnect", "old") not in ph.calls
    print("full run orchestration ok")

async def test_machine_picker():
    r, ph = mk()
    r.allowed_machines = {"a": "Office Tower", "b": "Office Tower", "c": "Studio-PC", "d": "Laptop"}
    assert r._match_machine("3") == "c" and r._match_machine("studio-pc") == "c"
    assert r._match_machine("lap") == "d" and r._match_machine("office") is None   # ambiguous name
    assert r._match_machine("9") is None and r._match_machine("") is None
    # requester signals are ignored while waiting on a technician
    r.inbox.put_nowait(Signal("requester", "close", "UREQ")); r.inbox.put_nowait(Signal("reply", "HP", "U7"))
    assert (await r._wait_signal(1)).kind == "reply"
    # bad reply -> hint, then a number picks and narrows the allowed machines
    t = asyncio.create_task(r._wait_machine_pick())
    r.inbox.put_nowait(Signal("requester", "the tower", "UREQ")); r.inbox.put_nowait(Signal("requester", "2", "UREQ"))
    mid, sig = await asyncio.wait_for(t, 2)
    assert mid == "b" and sig is None and r.allowed_machines == {"b": "Office Tower"}
    assert any(p[0] == "requester_hint" for p in r.slack.posts) and ("machine_chosen", "Office Tower") in r.slack.posts
    # a button click with a machine id that isn't theirs is ignored; a tech takeover ends the wait
    r.allowed_machines = {"a": "A", "c": "C"}
    t = asyncio.create_task(r._wait_machine_pick())
    r.inbox.put_nowait(Signal("machine", "zzz", "UREQ")); r.inbox.put_nowait(Signal("takeover", "", "U7"))
    mid, sig = await asyncio.wait_for(t, 2); assert mid is None and sig.kind == "takeover"
    # the tool refuses with one machine, posts the list with several
    tools = {tl.name: tl for tl in r._tools()}
    r.allowed_machines = {"a": "A"}; r.pending = None
    assert "only one" in (await tools["ask_requester_machine"].handler({}))["content"][0]["text"]
    r.allowed_machines = {"a": "A", "c": "C"}; r.machine_online = {"a": True}
    await tools["ask_requester_machine"].handler({})
    assert r.pending == ("machine", "") and r.slack.posts[-1] == ("ask_requester_machine", [("a", "A", True), ("c", "C", False)])
    print("machine picker ok")

async def test_run_with_picker():
    class DummyClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    agent.ClaudeSDKClient = DummyClient
    r, ph = mk(); ph.connected = False; r.options = lambda: None
    r.allowed_machines = {"m0": "Old laptop", "m1": "demo"}
    steps = iter(["pick", "resolve"]); prompts = []
    async def fake_converse(client, prompt, first):
        prompts.append(prompt); r.pending = None
        if next(steps) == "pick":
            r.pending = ("machine", "")
            asyncio.get_running_loop().call_later(0.05, r.inbox.put_nowait, Signal("machine", "m1", "UREQ"))
        else:
            r.machine_id = "m1"; ph.connected = True; r.outcome = "resolved"
    r._converse = fake_converse
    assert await asyncio.wait_for(r.run(), 5) == "resolved"
    assert "machine_id m1" in prompts[1] and "only machine" in prompts[1], prompts[1]
    print("run with machine picker ok")

async def main():
    await test_machine_picker(); await test_run_with_picker()
    await test_guards(); await test_handoff_then_phaze_handback()
    await test_slack_handback_and_resolve(); await test_solo_demo_same_account(); await test_screen_capture(); await test_run_with_signin_pause(); await test_join_timeout(); await test_run_orchestration()
asyncio.run(main())
