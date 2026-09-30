"""One helpdesk ticket, from first screenshot to resolved or handed off.

The agent works until it resolves the ticket, asks a technician a question, or requests a
handoff. Asking and handing off end its turn. Waiting for the human happens here in code,
not in the model: watch Slack for replies and buttons, watch phaze_status for the
technician joining, move control, and resume the same agent conversation when they hand back.

    agent working ──ask_technician──▶ wait for reply ──▶ agent working
          │  ▲                                  (a tech can also say `takeover`)
          │  └──── handback (button, `back ...`, or control given back in Phaze)
          ▼                                                  │
    request_handoff / takeover ─▶ page ─▶ tech joins ─▶ tech has control
                                                             └─▶ resolved / close / timeout
"""
import asyncio
import json
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from phaze_mcp import PhazeMCP, PhazeMCPError, controller, find_connection, self_guest
from prompts import SYSTEM_PROMPT, tech_message, ticket_prompt
from ticketing import Signal

PHAZE_INPUT_TOOLS = [
    "phaze_left_click", "phaze_double_click", "phaze_triple_click", "phaze_right_click",
    "phaze_middle_click", "phaze_left_click_drag", "phaze_left_mouse_down",
    "phaze_left_mouse_up", "phaze_mouse_move", "phaze_scroll", "phaze_send_keys",
]
PHAZE_READ_TOOLS = ["phaze_list_hosts", "phaze_status", "phaze_screenshot"]
PHAZE_SESSION_TOOLS = ["phaze_connect", "phaze_set_control"]
HELPDESK_TOOLS = ["lookup_requester_machines", "ask_requester_machine", "ask_technician",
                  "request_handoff", "post_ticket_note", "mark_resolved"]
REQUESTER_SIGNALS = ("requester", "machine")  # the requester's thread replies and machine-button clicks
CLAUDE_CODE_BUILTINS = [
    "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch",
    "WebSearch", "Task", "NotebookEdit", "TodoWrite", "BashOutput", "KillShell",
]
POLL_SECONDS = 5
CAPTURE_WARMUP_SECONDS = 8    # "output is not ready" right after connecting clears within seconds
CAPTURE_POLL_SECONDS = 2
SIGNIN_POLL_SECONDS = 5
SIGNIN_SCREEN_ERROR = "not capturing"  # what phaze_screenshot says on the Windows sign-in/lock screen
# Shown at the top of a handoff page when a password is what's blocking the agent.
SIGNIN_BLOCKER = "the computer is at the Windows sign-in / lock screen, and someone has to sign in."
PASSWORD_PROMPT_BLOCKER = "the agent stopped at a password, PIN, MFA, or admin (UAC) prompt."


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class HelpdeskRun:
    def __init__(self, cfg, ticket, slack, phaze_api, phaze: PhazeMCP, observe_only=False,
                 machine_locks: dict | None = None):
        self.cfg = cfg
        self.ticket = ticket
        self.slack = slack
        self.api = phaze_api
        self.phaze = phaze
        self.observe_only = observe_only
        self.machine_locks = machine_locks if machine_locks is not None else {}
        self.inbox: asyncio.Queue[Signal] = asyncio.Queue()  # technician signals, from Slack/console
        self.tag = f"[{ticket.ts}]"

        self.allowed_machines: dict[str, str] = {}  # machine id -> name
        self.machine_online: dict[str, bool] = {}
        self.requester_member_id = ""
        self.machine_id: str | None = None
        self.connection_id: str | None = None
        self.preexisting: set[str] = set()           # connections open before this run; never closed by it
        self.capturing: set[str] = set()             # connections known to return screenshots
        self.password_blocker: str | None = None     # set while a password is what's blocking the agent
        self.outcome = "unfinished"
        self.pending: tuple[str, str] | None = None  # ("ask" | "handoff", text) from the agent
        self.interrupted_by: Signal | None = None    # a tech signal that stopped the agent mid-turn
        self.stash: list[Signal] = []                # replies that arrived while the agent worked
        self.aborted = False

    def log(self, msg: str):
        print(f"{self.tag} {msg}")

    async def _post(self, method: str, text: str):
        await asyncio.to_thread(getattr(self.slack, method), self.ticket, text)

    async def _note(self, text: str):
        await self._post("add_internal_note", text)

    async def _who(self, sig: Signal) -> str:
        if sig.user.startswith("u_"):  # Phaze user id (control handed back inside Phaze)
            return await asyncio.to_thread(self.api.member_label, sig.user)
        return await asyncio.to_thread(self.slack.user_label, sig.user)

    # ---------- custom tools ----------
    def _tools(self):
        run = self

        @tool("lookup_requester_machines",
              "Find the Phaze machines assigned to the ticket's requester via the Phaze "
              "Enterprise API. Only these machines may be connected to.", {})
        async def lookup_requester_machines(_args):
            member = await asyncio.to_thread(run.api.find_member, run.ticket.requester_email)
            if not member:
                return _text(f"No Phaze member found for {run.ticket.requester_email}.")
            machines = await asyncio.to_thread(run.api.machines_for_member, member)
            run.requester_member_id = member.get("id", "")
            run.allowed_machines = {str(m["id"]): m.get("name", "") for m in machines if m.get("id")}
            run.machine_online = {str(m["id"]): bool(m.get("is_online")) for m in machines if m.get("id")}
            return _text(json.dumps({"member": member, "machines": machines}, indent=2, default=str))

        @tool("ask_technician",
              "Ask a human technician a question in the private escalation thread. You keep the "
              "session. End your turn right after calling this; the answer arrives as a new message.",
              {"question": str})
        async def ask_technician(args):
            if run.pending:
                return _text("Already waiting on a technician. End your turn now.")
            run.pending = ("ask", args["question"])
            await run._post("ask", f"<!here> :question: *The helpdesk agent has a question* "
                                   f"(it keeps the session while it waits)\n{args['question']}\n\n"
                                   f"Reply in this thread, or reply `takeover` to take the session.")
            return _text("Question posted. End your turn now with a one-line summary.")

        @tool("ask_requester_machine",
              "The requester has several machines and the ticket doesn't say which one has the "
              "problem. Posts the list in the requester's ticket thread so they can pick one. "
              "End your turn right after; their pick arrives as a new message.", {})
        async def ask_requester_machine(_args):
            if run.pending:
                return _text("Already waiting on someone. End your turn now.")
            if not run.allowed_machines:
                return _text("Call lookup_requester_machines first.")
            if len(run.allowed_machines) == 1:
                return _text("The requester has only one machine; use it.")
            run.pending = ("machine", "")
            machines = [(mid, name, run.machine_online.get(mid, False))
                        for mid, name in run.allowed_machines.items()]
            await asyncio.to_thread(run.slack.ask_requester_machine, run.ticket, machines)
            return _text("Machine list posted to the requester. End your turn now with a one-line summary.")

        @tool("request_handoff",
              "Hand the live session to a human technician. The system pages them, gives them "
              "control when they join, and resumes you if they hand back. Set password_required "
              "when you stopped because a password, PIN, MFA code, admin (UAC) prompt, or lock "
              "screen is in the way. End your turn right after.",
              {"summary": str, "password_required": bool})
        async def request_handoff(args):
            if run.pending:
                return _text("Already waiting on a technician. End your turn now.")
            if args.get("password_required"):
                run.password_blocker = PASSWORD_PROMPT_BLOCKER
            run.pending = ("handoff", args["summary"])
            return _text("Handoff requested. Stop sending input and end your turn now.")

        @tool("post_ticket_note",
              "Post a tech-only note to the ticket's thread in the escalation channel. "
              "The requester does not see it.", {"note": str})
        async def post_ticket_note(args):
            await run._note(f"[Helpdesk agent]\n{args['note']}")
            return _text("Note posted.")

        @tool("mark_resolved",
              "Call ONLY after you verified the fix with a screenshot and posted your note.",
              {"summary": str})
        async def mark_resolved(args):
            run.outcome = "resolved"
            return _text("Marked resolved. The system will release control and disconnect. End your turn.")

        return [lookup_requester_machines, ask_requester_machine, ask_technician, request_handoff,
                post_ticket_note, mark_resolved]

    # ---------- deterministic guardrails (every tool call passes through here) ----------
    async def _guard(self, input_data, tool_use_id, context):
        if self.pending or self.interrupted_by:
            return _deny("Waiting for a technician. Don't call more tools; end your turn now "
                         "with a one-line summary.")
        name = input_data.get("tool_name", "")
        args = input_data.get("tool_input") or {}
        if not name.startswith("mcp__phaze__"):
            return {}
        t = name.removeprefix("mcp__phaze__")

        if t == "phaze_connect":
            mid = str(args.get("machine_id", ""))
            if not self.allowed_machines:
                return _deny("Call lookup_requester_machines before connecting.")
            if mid not in self.allowed_machines:
                return _deny(f"Machine {mid} is not assigned to the requester. Use request_handoff.")
            holder = self.machine_locks.get(mid)
            if holder and holder != self.ticket.id:
                return _deny("Another ticket is being worked on this machine right now. Use request_handoff.")
            self.machine_locks[mid] = self.ticket.id
            self.machine_id = mid
            return {}

        if "connection_id" not in args:
            return {}
        conn = find_connection(await self.phaze.status(), str(args["connection_id"]))
        if not conn:
            return {}  # let the tool itself report the bad id
        if conn.get("owner") != "agent":
            return _deny("That is the user's own live session. Never touch it; use your own connection.")
        if conn.get("host_machine_id") not in self.allowed_machines:
            return _deny("That connection is to a machine not assigned to the requester.")
        self.connection_id = conn["connection_id"]

        if t == "phaze_screenshot":
            problem = await self._ensure_capturing(conn)
            if problem:
                return _deny(problem)

        if t == "phaze_set_control":
            target = int(args.get("guest_id", 0))
            me, holder = self_guest(conn), controller(conn)
            if me and target == me.get("guest_id"):
                if self.observe_only:
                    return _deny("Observe-only mode: you may not take control. Use request_handoff.")
                if holder and not holder.get("is_self"):
                    return _deny(f"Guest {holder.get('guest_id')} (a person or another session) has "
                                 f"control. Don't take it; use ask_technician or request_handoff.")
            elif target != 0:
                return _deny("Only the system hands control to technicians. Use request_handoff.")
        return {}

    # ---------- screen capture ----------
    async def _capture_error(self, connection_id: str) -> str | None:
        """Try one real screenshot; None if it worked, else the error text."""
        try:
            await self.phaze.call("phaze_screenshot", connection_id=connection_id)
            self.capturing.add(connection_id)
            return None
        except PhazeMCPError as e:
            return str(e)

    async def _ensure_capturing(self, conn: dict) -> str | None:
        """Before the agent's first screenshot on a connection, check the screen can actually
        be captured. Returns a message for the agent when it shouldn't proceed, else None.

        - "output is not ready": the first frame hasn't arrived; wait a few seconds.
        - "connection is not capturing": the machine is on the Windows sign-in / lock screen
          (a secure desktop Phaze doesn't capture). Only a person can sign in, so ask the
          requester and let the run loop resume the agent once the screen is visible.
        """
        cid = conn["connection_id"]
        if cid in self.capturing:
            return None
        error, deadline = None, time.monotonic() + CAPTURE_WARMUP_SECONDS
        while (error := await self._capture_error(cid)) and time.monotonic() < deadline:
            await asyncio.sleep(CAPTURE_POLL_SECONDS)
        if not error:
            return None
        if SIGNIN_SCREEN_ERROR in error:
            self.pending = ("signin", cid)
            self.log("[phaze] screen not capturing: machine is likely at the sign-in screen")
            machine = self.allowed_machines.get(self.machine_id or "", "your computer")
            self.password_blocker = SIGNIN_BLOCKER
            await asyncio.to_thread(self.slack.ask_requester_sign_in, self.ticket, machine)
            await self._post("announce",
                             f":lock: *Stopped at the Windows sign-in / lock screen: a password is "
                             f"needed.* {machine} can't be seen or used until someone signs in. The "
                             f"agent asked the requester to sign in and will continue automatically "
                             f"once the screen is visible.")
            return ("The computer is at the Windows sign-in or lock screen, which Phaze can't capture, "
                    "and you must never type a password. The system has asked the requester to sign "
                    "in and will message you when the screen is visible. End your turn now.")
        return (f"Screenshots on this connection fail ({error}). Use request_handoff and say so.")

    async def _wait_signed_in(self, connection_id: str) -> tuple[bool, Signal | None]:
        """Wait for the requester to sign in: poll a screenshot until it works. Returns
        (True, None) once the screen is visible, (False, tech signal) if a tech stepped in,
        or (False, None) on timeout."""
        deadline = time.monotonic() + self.cfg.human_reply_timeout_min * 60
        while time.monotonic() < deadline:
            sig = await self._wait_signal(SIGNIN_POLL_SECONDS)
            if sig and sig.kind in ("takeover", "resolved", "close"):
                return False, sig
            if sig:
                await self._note("Waiting for the requester to sign in. Reply `takeover` to step in.")
            if not await self._capture_error(connection_id):
                self.password_blocker = None
                await asyncio.to_thread(self.slack.requester_signed_in, self.ticket, True)
                await self._note(":unlock: Signed in; the screen is visible again and the agent is continuing.")
                self.log("[phaze] screen visible again; requester signed in")
                return True, None
        await asyncio.to_thread(self.slack.requester_signed_in, self.ticket, False)
        return False, None

    def options(self) -> ClaudeAgentOptions:
        helpdesk = create_sdk_mcp_server(name="helpdesk", version="0.2.0", tools=self._tools())
        phaze_tools = PHAZE_READ_TOOLS + PHAZE_SESSION_TOOLS + ([] if self.observe_only else PHAZE_INPUT_TOOLS)
        allowed = [f"mcp__phaze__{t}" for t in phaze_tools] + [f"mcp__helpdesk__{t}" for t in HELPDESK_TOOLS]
        disallowed = CLAUDE_CODE_BUILTINS + ["mcp__phaze__phaze_disconnect"] + (
            [f"mcp__phaze__{t}" for t in PHAZE_INPUT_TOOLS] if self.observe_only else []
        )
        return ClaudeAgentOptions(
            model=self.cfg.model,
            system_prompt=SYSTEM_PROMPT,
            mcp_servers={
                "helpdesk": helpdesk,
                "phaze": {"type": "http", "url": self.cfg.phaze_mcp_url},
            },
            strict_mcp_config=True,  # only these servers, never the host's own Claude Code MCP config
            setting_sources=[],
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            max_turns=self.cfg.max_turns,
            max_buffer_size=32 * 1024 * 1024,  # screenshots exceed the SDK's 1 MB default
            # Headroom for the guard's screen-capture check before a first screenshot.
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._guard], timeout=120)]},
        )

    # ---------- the agent's turn ----------
    async def _converse(self, client: ClaudeSDKClient, prompt: str, first: bool):
        self.pending = None
        self.interrupted_by = None
        if self.stash:
            prompt += "\n\nWhile you were working, technicians also said:\n" + "\n".join(
                tech_message(await self._who(s), s.text) for s in self.stash)
            self.stash.clear()
        await client.query(prompt)
        watcher = asyncio.create_task(self._watch_while_working(client))
        try:
            async for msg in client.receive_response():
                if first and isinstance(msg, SystemMessage) and msg.subtype == "init":
                    servers = {s.get("name"): s.get("status") for s in msg.data.get("mcp_servers", [])}
                    if servers.get("phaze") != "connected":
                        self.aborted = True
                        self.outcome = "failed"
                        self.log(f"[abort] Phaze MCP not connected (status={servers.get('phaze')}). "
                                 f"Is the Phaze app running? Check PHAZE_MCP_URL in .env.")
                        await client.interrupt()
                        break
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            self.log(f"[agent] {block.text}")
                        elif isinstance(block, ToolUseBlock):
                            self.log(f"[tool]  {block.name} {json.dumps(block.input)[:160]}")
                elif isinstance(msg, ResultMessage):
                    self.log(f"[turn]  outcome={self.outcome} turns={msg.num_turns} "
                             f"cost=${(msg.total_cost_usd or 0):.3f}")
        finally:
            watcher.cancel()

    async def _watch_while_working(self, client: ClaudeSDKClient):
        """While the agent works, a technician can stop it (takeover / resolved / close)."""
        while True:
            sig = await self.inbox.get()
            if sig.kind in ("takeover", "resolved", "close"):
                self.interrupted_by = sig
                self.log(f"[human] {sig.kind} while agent working; interrupting")
                await client.interrupt()
                return
            if sig.kind == "reply":
                self.stash.append(sig)
                await self._note("Noted. The agent is mid-task and will see this at its next pause. "
                                 "Reply `takeover` to stop it now.")
            elif sig.kind == "handback":
                await self._note("The agent already has the session.")

    # ---------- orchestration ----------
    async def run(self) -> str:
        prompt = ticket_prompt(self.ticket)
        if self.observe_only:
            prompt += ("\n\nOBSERVE-ONLY MODE: connect and screenshot to diagnose, but you cannot "
                       "send input. Post your diagnosis as a note, then request_handoff.")
        handoffs = 0
        status = await self.phaze.status()
        self.preexisting = {c["connection_id"] for c in status.get("connections", [])}
        try:
            async with ClaudeSDKClient(options=self.options()) as client:
                first = True
                while True:
                    await self._converse(client, prompt, first)
                    first = False
                    if self.aborted:
                        break
                    request = self.pending
                    if self.interrupted_by:
                        sig = self.interrupted_by
                        if sig.kind in ("resolved", "close"):
                            await self._finish_by_tech(sig)
                            break
                        request = ("handoff", f"{await self._who(sig)} took over the session. {sig.text}")
                    elif request is None:
                        if self.outcome == "resolved":
                            break
                        request = ("handoff", "The agent stopped without resolving the ticket or "
                                              "asking for help (it may have run out of turns).")

                    if request[0] == "machine":
                        mid, sig = await self._wait_machine_pick()
                        if mid:
                            name = self.allowed_machines[mid]
                            prompt = (f"The requester picked *{name}* (machine_id {mid}). It is now the "
                                      f"only machine you may connect to. Connect to it and continue the ticket.")
                            continue
                        if sig and sig.kind in ("resolved", "close"):
                            await self._finish_by_tech(sig)
                            break
                        request = ("handoff",
                                   f"{await self._who(sig)} took over while the requester was picking a machine."
                                   if sig else
                                   f"The requester didn't pick a machine within {self.cfg.human_reply_timeout_min} "
                                   f"min. Their machines: {', '.join(self.allowed_machines.values())}.")

                    if request[0] == "signin":
                        ok, sig = await self._wait_signed_in(request[1])
                        if ok:
                            prompt = ("The requester signed in and the screen is visible now. Take a "
                                      "screenshot and continue the ticket.")
                            continue
                        if sig and sig.kind in ("resolved", "close"):
                            await self._finish_by_tech(sig)
                            break
                        request = ("handoff",
                                   f"{await self._who(sig)} took over while the machine was at the sign-in screen."
                                   if sig else
                                   f"The machine stayed at the Windows sign-in screen for "
                                   f"{self.cfg.human_reply_timeout_min} min; the requester didn't sign in.")

                    if request[0] == "ask":
                        sig = await self._wait_signal(self.cfg.human_reply_timeout_min * 60)
                        if sig is None:
                            request = ("handoff", f"Nobody answered the agent's question within "
                                                  f"{self.cfg.human_reply_timeout_min} min: {request[1]}")
                        elif sig.kind in ("reply", "handback"):
                            prompt = (f"A technician answered your question.\n"
                                      f"{tech_message(await self._who(sig), sig.text)}\nContinue the ticket.")
                            continue
                        elif sig.kind == "takeover":
                            request = ("handoff", f"{await self._who(sig)} took over after the "
                                                  f"agent asked: {request[1]}")
                        else:
                            await self._finish_by_tech(sig)
                            break

                    handoffs += 1
                    sig = await self._handoff(request[1], allow_handback=handoffs <= self.cfg.max_handoffs)
                    if not sig:
                        break
                    prompt = await self._take_back(sig)
        finally:
            await self._cleanup()
        return self.outcome

    async def _wait_signal(self, timeout: float, ignore=REQUESTER_SIGNALS) -> Signal | None:
        """Next signal within `timeout`, dropping kinds in `ignore` (by default the requester's,
        which only matter while they're picking a machine)."""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                sig = await asyncio.wait_for(self.inbox.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if sig.kind not in ignore:
                return sig
        return None

    def _match_machine(self, text: str) -> str | None:
        """Machine id for a requester's reply: a list number or (part of) a machine name."""
        text = text.strip().strip(".").lower()
        ids = list(self.allowed_machines)
        if text.isdigit() and 1 <= int(text) <= len(ids):
            return ids[int(text) - 1]
        hits = [mid for mid, name in self.allowed_machines.items() if text and text in name.lower()]
        exact = [mid for mid in hits if self.allowed_machines[mid].lower() == text]
        return (exact or hits)[0] if len(exact or hits) == 1 else None

    async def _wait_machine_pick(self) -> tuple[str | None, Signal | None]:
        """Wait for the requester to pick a machine from the list the agent posted.
        Returns (machine_id, None) on a pick, (None, tech signal) if a tech stepped in,
        or (None, None) on timeout."""
        deadline = time.monotonic() + self.cfg.human_reply_timeout_min * 60
        while True:
            sig = await self._wait_signal(deadline - time.monotonic(), ignore=())
            if sig is None:
                await asyncio.to_thread(self.slack.machine_chosen, self.ticket, None)
                return None, None
            if sig.kind in ("takeover", "resolved", "close"):
                await asyncio.to_thread(self.slack.machine_chosen, self.ticket, None)
                return None, sig
            if sig.kind == "machine":
                mid = sig.text if sig.text in self.allowed_machines else None
            elif sig.kind == "requester" or (sig.kind == "reply" and sig.user == "console"):
                mid = self._match_machine(sig.text)
                if not mid:
                    await asyncio.to_thread(self.slack.requester_hint, self.ticket, len(self.allowed_machines))
                    continue
            else:
                await self._note("Waiting for the requester to pick a machine. Reply `takeover` to step in.")
                continue
            if mid:
                name = self.allowed_machines[mid]
                self.allowed_machines = {mid: name}  # the agent may now only connect to this one
                await asyncio.to_thread(self.slack.machine_chosen, self.ticket, name)
                await self._note(f"The requester picked *{name}*. The agent is connecting.")
                self.log(f"[human] requester picked {name}")
                return mid, None

    async def _finish_by_tech(self, sig: Signal):
        who = await self._who(sig)
        if sig.kind == "resolved":
            self.outcome = "resolved"
            await self._note(f"Marked resolved by {who}. {sig.text}".strip())
        else:
            self.outcome = "closed"
            await self._note(f"Closed by {who}. {sig.text}".strip())

    async def _my_connection(self) -> dict | None:
        status = await self.phaze.status()
        conn = find_connection(status, self.connection_id) if self.connection_id else None
        if not conn and self.machine_id:
            mine = [c for c in status.get("connections", [])
                    if c.get("owner") == "agent" and c.get("host_machine_id") == self.machine_id
                    and c["connection_id"] not in self.preexisting]
            conn = mine[-1] if mine else None
        self.connection_id = conn["connection_id"] if conn else None
        return conn

    def _technicians(self, conn: dict, present_at_page: set[int]) -> list[dict]:
        """Guests who count as the technician. Guests are shared by every connection to the
        host, so the agent account's other sessions show up here too. A guest from the agent's
        own Phaze account counts only if it joined after the page: that is someone clicking
        Connect in Phaze with the same account (e.g. a solo demo), not a leftover session."""
        me = self_guest(conn)
        agent_user = me.get("user") if me else None
        techs = []
        for g in conn.get("guests", []):
            if g.get("is_self"):
                continue
            if g.get("user") == agent_user:
                if g.get("guest_id") in present_at_page:
                    continue
            elif g.get("user") == self.requester_member_id:
                continue  # the requester watching their own machine is never the technician
            techs.append(g)
        return techs

    async def _handoff(self, summary: str, allow_handback: bool) -> Signal | None:
        """Page a tech, give them control when they join, and wait until they hand back
        (returns the handback Signal) or the ticket ends (returns None)."""
        self.outcome = "escalated"
        conn = await self._my_connection()
        # Where to send the tech: the machine the agent connected to, else the requester's only one.
        machine_id = self.machine_id or (
            next(iter(self.allowed_machines)) if len(self.allowed_machines) == 1 else None)
        machine = self.allowed_machines.get(machine_id or "", "") or machine_id or "unknown"
        present_at_page = {g.get("guest_id") for g in conn.get("guests", [])} if conn else set()
        if conn:
            me = self_guest(conn)
            if me and me.get("has_control"):
                await self.phaze.set_control(conn["connection_id"], 0)
            where = (f"*Machine:* {machine}. Click *Connect in Phaze*; you'll get control "
                     f"automatically once you join.")
        elif machine_id:
            where = f"*Machine:* {machine}. The agent has no open session on it; *Connect in Phaze* to take a look."
        else:
            where = f"*Machine:* {machine}. The agent has no open session on it."
        back = ("When you're done, click *Hand back to agent* (or reply `back <instructions>`, or "
                "give control to the agent's guest in Phaze), *Mark resolved*, or *Close*."
                if allow_handback else
                "Handback limit reached for this ticket: please finish it, then *Mark resolved* or *Close*.")
        needs_password = (f":lock: *Needs a password:* {self.password_blocker}\n"
                          if self.password_blocker else "")
        await asyncio.to_thread(self.slack.escalate, self.ticket,
                                f"<!here> :rotating_light: *Helpdesk agent needs a human*\n"
                                f"{needs_password}"
                                f"*Ticket:* <{self.ticket.url}|{self.ticket.subject or self.ticket.id}>\n"
                                f"*Requester:* {self.ticket.requester_name} ({self.ticket.requester_email})\n"
                                f"{where}\n\n{summary}\n\n{back}", machine_id)
        self.log(f"[human] paged: {summary[:120]}")

        tech: dict | None = None
        join_by = time.monotonic() + self.cfg.human_join_timeout_min * 60
        end_by = time.monotonic() + self.cfg.human_session_timeout_min * 60
        while True:
            sig = await self._wait_signal(POLL_SECONDS)
            if sig:
                if sig.kind == "handback":
                    if allow_handback:
                        return sig
                    await self._note("Handback limit reached. Please finish, then Mark resolved or Close.")
                elif sig.kind in ("resolved", "close"):
                    await self._finish_by_tech(sig)
                    return None
                elif sig.kind == "reply":
                    await self._note("The agent is paused. Reply `back <instructions>` to hand the "
                                     "session back, or `resolved` / `close` to finish.")
                continue

            now = time.monotonic()
            try:
                conn = await self._my_connection() if self.connection_id else None
            except PhazeMCPError as e:
                self.log(f"[phaze] status failed while waiting on a human: {e}")
                continue
            if conn:
                techs = self._technicians(conn, present_at_page)
                me = self_guest(conn)
                if tech and not any(g["guest_id"] == tech["guest_id"] for g in techs):
                    await self._note("The technician left the session without handing back. Waiting "
                                     "for them to rejoin, or for `back` / `resolved` / `close`.")
                    tech, join_by = None, now + self.cfg.human_join_timeout_min * 60
                elif not tech and techs:
                    tech = techs[0]
                    await self.phaze.set_control(conn["connection_id"], tech["guest_id"])
                    label = await asyncio.to_thread(self.api.member_label, tech.get("user", ""))
                    await self._note(f"{label} joined the session and now has control.")
                    self.log(f"[human] {label} joined; control handed over")
                    end_by = now + self.cfg.human_session_timeout_min * 60
                elif tech and me and me.get("has_control") and allow_handback:
                    return Signal("handback", "", tech.get("user", ""))  # tech gave control back in Phaze
                if not tech and now > join_by:
                    await self._note(f"Nobody joined within {self.cfg.human_join_timeout_min} min. "
                                     f"The agent is disconnecting; this ticket still needs a human.")
                    return None
            if now > end_by:
                await self._note(f"No handback or close after {self.cfg.human_session_timeout_min} "
                                 f"min. The agent is disconnecting and leaving this ticket to you.")
                return None

    async def _take_back(self, sig: Signal) -> str:
        """Retake control after a handback and build the agent's next prompt."""
        who = await self._who(sig)
        self.outcome = "unfinished"
        self.password_blocker = None  # the technician dealt with it (or will say otherwise)
        conn = await self._my_connection()
        state = "You have no open session; reconnect as in your workflow."
        if conn and self.observe_only:
            state = f"Observe-only mode: you still cannot send input. Connection: {conn['connection_id']}."
        elif conn:
            me = self_guest(conn)
            if me and not me.get("has_control"):
                await self.phaze.set_control(conn["connection_id"], me["guest_id"])
            for _ in range(10):
                conn = await self._my_connection()
                me = self_guest(conn) if conn else None
                if me and me.get("has_control"):
                    break
                await asyncio.sleep(1)
            state = (f"You have control again on connection {conn['connection_id']}." if me and me.get("has_control")
                     else f"Take control with set_control (your own guest) on connection "
                          f"{self.connection_id} and confirm with phaze_status before sending input.")
        await self._note(f"{who} handed the session back to the agent.")
        self.log(f"[human] handback from {who}")
        return (f"A technician handed the session back. {state}\n"
                f"{tech_message(who, sig.text)}\n"
                f"Take a fresh screenshot first, then continue the ticket.")

    async def _cleanup(self):
        """Release control (unless a human holds it) and close the agent's own connection."""
        try:
            conn = await self._my_connection()
            if conn and conn["connection_id"] in self.preexisting:
                self.log(f"[phaze] left {conn['connection_id'][:24]}… open (it predates this run)")
            elif conn and conn.get("owner") == "agent":
                me = self_guest(conn)
                if me and me.get("has_control"):
                    await self.phaze.set_control(conn["connection_id"], 0)
                try:
                    await self.phaze.disconnect(conn["connection_id"])
                except PhazeMCPError as e:
                    if "no connection" not in str(e):  # a timed-out first attempt can still land
                        raise
                self.log(f"[phaze] disconnected {conn['connection_id'][:24]}…")
        except Exception as e:
            self.log(f"[error] cleanup failed: {e}. Check the Phaze app for an open session.")
        finally:
            if self.machine_id and self.machine_locks.get(self.machine_id) == self.ticket.id:
                del self.machine_locks[self.machine_id]
