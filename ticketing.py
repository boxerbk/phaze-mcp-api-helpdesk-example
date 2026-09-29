"""Slack as the ticket source and the technician's control panel.

A ticket is a top-level message (plus its thread) in the help channel, e.g. #it-help.
- The requester only gets emoji status reactions on their message.
- Each ticket gets a thread in the private escalation channel. The agent posts notes and
  questions there, and technicians steer the agent from it with buttons or thread replies
  (see parse_command).
"""
import re
from dataclasses import dataclass

import httpx


@dataclass
class Ticket:
    id: str            # "<channel>:<ts>"
    channel: str
    ts: str
    requester_email: str
    requester_name: str
    subject: str
    body: str
    url: str
    requester_slack_id: str = ""


@dataclass
class Signal:
    """Something a technician did in the ticket's escalation thread."""
    kind: str          # reply | takeover | handback | resolved | close
    text: str = ""
    user: str = ""     # Slack user id


# Button action ids. The button value is the ticket id, which routes the click to its run.
ACTIONS = {"hd_takeover": "takeover", "hd_handback": "handback",
           "hd_resolved": "resolved", "hd_close": "close"}

CONNECT_ACTION = "phaze_connect_link"  # link button; run.py only acks it
MACHINE_ACTION_PREFIX = "req_machine_"  # requester's machine picker; value is "<ticket id>|<machine id>"

_COMMANDS = [
    (re.compile(r"^(?:hand\s*back|back|agent)\b[\s:,-]*", re.I), "handback"),
    (re.compile(r"^(?:take\s*over|takeover|stop)\b[\s:,-]*", re.I), "takeover"),
    (re.compile(r"^(?:resolved|fixed)\b[\s:,-]*", re.I), "resolved"),
    (re.compile(r"^close\b[\s:,-]*", re.I), "close"),
]

HELP_TEXT = ("Reply in this thread to steer the agent: `takeover` (agent stops and hands you "
             "the session), `back <instructions>` (agent takes control again), `resolved`, "
             "`close`. Any other reply is passed to the agent as an answer.")


def parse_command(text: str, user: str = "") -> Signal:
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text or "").strip()
    for pattern, kind in _COMMANDS:
        if pattern.match(text):
            return Signal(kind, pattern.sub("", text, count=1).strip(), user)
    return Signal("reply", text, user)


def parse_permalink(url: str) -> tuple[str, str]:
    """https://x.slack.com/archives/C0123/p1727550000123456 -> ("C0123", "1727550000.123456")"""
    m = re.search(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})", url)
    if not m:
        raise SystemExit(f"Not a Slack message permalink: {url}")
    return m.group(1), f"{m.group(2)}.{m.group(3)}"


def _button(text: str, action_id: str, ticket: Ticket, style: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text},
         "action_id": action_id, "value": ticket.id}
    if style:
        b["style"] = style
    return b


def phaze_connect_url(machine_id: str) -> str:
    """Phaze protocol handler link: opens the Phaze app and connects to the machine."""
    return f"phaze://connect?id={machine_id}"


def _blocks(text: str, buttons: list[dict]) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": buttons}]


class Slack:
    """One instance per ticket: it remembers that ticket's escalation thread."""

    def __init__(self, bot_token: str, escalation_channel: str):
        self.escalation_channel = escalation_channel
        self.http = httpx.Client(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=15,
        )
        self.thread_ts: str | None = None

    def _call(self, method: str, **payload) -> dict:
        r = self.http.post(f"/{method}", json=payload).json()
        if not r.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {r.get('error')}")
        return r

    def _get(self, method: str, **params) -> dict:
        r = self.http.get(f"/{method}", params=params).json()
        if not r.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {r.get('error')}")
        return r

    # ---- intake ----
    def get(self, permalink: str) -> Ticket:
        channel, ts = parse_permalink(permalink)
        return self.get_message(channel, ts, permalink)

    def get_message(self, channel: str, ts: str, permalink: str | None = None) -> Ticket:
        msgs = self._get("conversations.replies", channel=channel, ts=ts, limit=50)["messages"]
        root = msgs[0]
        user_id = root.get("user", "")
        user = self._get("users.info", user=user_id)["user"] if user_id else {}
        profile = user.get("profile", {})
        body = "\n\n".join(m.get("text", "") for m in msgs if m.get("user") == user_id)
        if not permalink:
            permalink = self._get("chat.getPermalink", channel=channel, message_ts=ts)["permalink"]
        return Ticket(
            id=f"{channel}:{ts}",
            channel=channel,
            ts=ts,
            requester_email=profile.get("email", ""),
            requester_name=profile.get("real_name") or user.get("name", ""),
            subject=root.get("text", "")[:80],
            body=body,
            url=permalink,
            requester_slack_id=user_id,
        )

    def user_label(self, user_id: str) -> str:
        try:
            u = self._get("users.info", user=user_id)["user"]
            return u.get("profile", {}).get("real_name") or u.get("name") or user_id
        except RuntimeError:
            return user_id or "a technician"

    # ---- requester-visible status (emoji only) ----
    def react(self, ticket: Ticket, emoji: str) -> None:
        try:
            self._call("reactions.add", channel=ticket.channel, timestamp=ticket.ts, name=emoji)
        except RuntimeError as e:
            if "already_reacted" not in str(e):
                raise

    # ---- requester's machine picker (the only message the requester gets besides emoji) ----
    def ask_requester_machine(self, ticket: Ticket, machines: list[tuple[str, str, bool]]) -> None:
        """Reply in the requester's thread with their machines as buttons: (id, name, online)."""
        lines = [f"{i}. *{name}*" + ("" if online else " _(offline)_")
                 for i, (_, name, online) in enumerate(machines, 1)]
        text = (f"<@{ticket.requester_slack_id}> I can help with that. Which computer is it on?\n"
                + "\n".join(lines) + "\nClick one below, or reply here with its number.")
        buttons = [{"type": "button", "action_id": f"{MACHINE_ACTION_PREFIX}{i}",
                    "text": {"type": "plain_text", "text": f"{i}. {name}"[:75]},  # names can repeat
                    "value": f"{ticket.id}|{mid}"}
                   for i, (mid, name, _) in enumerate(machines[:25], 1)]
        r = self._call("chat.postMessage", channel=ticket.channel, thread_ts=ticket.ts,
                       text=text, blocks=_blocks(text, buttons))
        self.picker_ts = r["ts"]

    def requester_hint(self, ticket: Ticket, count: int) -> None:
        self._call("chat.postMessage", channel=ticket.channel, thread_ts=ticket.ts,
                   text=f"Sorry, I didn't catch that. Reply with a number from 1 to {count}, "
                        f"or click one of the buttons above.")

    def machine_chosen(self, ticket: Ticket, name: str | None) -> None:
        """Replace the picker's buttons with the outcome so nobody clicks it again."""
        if not getattr(self, "picker_ts", None):
            return
        text = (f"Thanks! Connecting to *{name}* now." if name else
                "A technician is taking this one from here.")
        self._call("chat.update", channel=ticket.channel, ts=self.picker_ts, text=text,
                   blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
        self.picker_ts = None

    # ---- tech-only thread ----
    def open_thread(self, ticket: Ticket) -> None:
        """Create the escalation-channel thread up front; fails fast on a bad channel."""
        text = (f":robot_face: Helpdesk agent working <{ticket.url}|a ticket> from "
                f"{ticket.requester_name}: _{ticket.subject}_\n{HELP_TEXT}")
        r = self._call("chat.postMessage", channel=self.escalation_channel, text=text,
                       blocks=_blocks(text, [_button("Take over", "hd_takeover", ticket, "danger")]))
        self.thread_ts = r["ts"]

    def add_internal_note(self, ticket: Ticket, note: str) -> None:
        self._call("chat.postMessage", channel=self.escalation_channel,
                   thread_ts=self.thread_ts, text=note)

    def ask(self, ticket: Ticket, text: str) -> None:
        """A question for a technician; the agent keeps the session while it waits."""
        self._call("chat.postMessage", channel=self.escalation_channel,
                   thread_ts=self.thread_ts, text=text, reply_broadcast=True)

    def escalate(self, ticket: Ticket, text: str, machine_id: str | None = None) -> None:
        """Page a technician to take over, with buttons to connect, hand back, or close."""
        buttons = [
            _button("Hand back to agent", "hd_handback", ticket),
            _button("Mark resolved", "hd_resolved", ticket),
            _button("Close", "hd_close", ticket),
        ]
        if machine_id:
            buttons.insert(0, {"type": "button", "action_id": CONNECT_ACTION, "style": "primary",
                               "text": {"type": "plain_text", "text": "Connect in Phaze"},
                               "url": phaze_connect_url(machine_id)})
        self._call("chat.postMessage", channel=self.escalation_channel,
                   thread_ts=self.thread_ts, text=text, reply_broadcast=True,
                   blocks=_blocks(text, buttons))


class DryRunSlack(Slack):
    """Reads the real ticket but prints instead of posting or reacting.
    Technician commands are typed into the console instead (see run.py)."""

    def user_label(self, user_id):
        return "console technician" if user_id == "console" else super().user_label(user_id)

    def react(self, ticket, emoji):
        print(f"[dry-run] react :{emoji}: on {ticket.url}")

    def open_thread(self, ticket):
        print(f"[dry-run] open escalation thread in {self.escalation_channel}")
        print(f"[dry-run] {HELP_TEXT} (type them here in the console)")

    def add_internal_note(self, ticket, note):
        print(f"\n[dry-run] NOTE\n{note}\n")

    def ask(self, ticket, text):
        print(f"\n[dry-run] QUESTION FOR TECH\n{text}\n")

    def ask_requester_machine(self, ticket, machines):
        print("\n[dry-run] REQUESTER THREAD: Which computer is it on?")
        for i, (_, name, online) in enumerate(machines, 1):
            print(f"  {i}. {name}{'' if online else ' (offline)'}")
        print("[dry-run] type the requester's pick (number or name) here\n")

    def requester_hint(self, ticket, count):
        print(f"[dry-run] REQUESTER THREAD: reply with a number from 1 to {count}")

    def machine_chosen(self, ticket, name):
        print(f"[dry-run] REQUESTER THREAD: {'connecting to ' + name if name else 'a technician is taking over'}")

    def escalate(self, ticket, text, machine_id=None):
        connect = f"connect ({phaze_connect_url(machine_id)}) / " if machine_id else ""
        print(f"\n[dry-run] ESCALATION\n{text}\n[dry-run] buttons: {connect}hand back / resolved / close\n")
