"""Run the helpdesk agent.

  python run.py                     # listen: every new message in SLACK_HELP_CHANNEL is a ticket
  python run.py <permalink>         # work one ticket (right-click message > Copy link)

Safety flags (same for both modes). The default is SAFE: observe-only + dry-run.
  --allow-input   the agent may click and type
  --post          really post notes/reactions to Slack (otherwise printed)
  --live          both

Technicians steer a ticket from its escalation thread (buttons, or replies such as
`takeover`, `back <instructions>`, `resolved`, `close`). In dry-run, or without
SLACK_APP_TOKEN, type those same commands into this console instead.
"""
import argparse
import asyncio
import re
import sys
import threading

from agent import HelpdeskRun
from config import Config
from phaze_api import PhazeAPI
from phaze_mcp import PhazeMCP
from ticketing import (ACTIONS, CONNECT_ACTION, MACHINE_ACTION_PREFIX, DryRunSlack, Signal, Slack,
                       parse_command)

STATUS_EMOJI = {"start": "eyes", "resolved": "white_check_mark", "escalated": "raising_hand",
                "closed": "ballot_box_with_check"}
FAILED_EMOJI = "warning"  # anything else: the requester sees it wasn't handled
TICKET_SUBTYPES = {None, "file_share"}  # a message with a screenshot attached is still a ticket


class Desk:
    """Runs tickets and routes technician signals (Slack or console) to them."""

    def __init__(self, cfg: Config, observe_only: bool, dry_run: bool):
        self.cfg = cfg
        self.observe_only = observe_only
        self.dry_run = dry_run
        self.api = PhazeAPI(cfg.phaze_api_key)
        self.phaze = PhazeMCP(cfg.phaze_mcp_url)
        self.runs: dict[str, HelpdeskRun] = {}  # ticket id -> active run
        self.threads: dict[str, str] = {}       # escalation thread ts -> ticket id
        self.seen: set[str] = set()
        self.machine_locks: dict[str, str] = {}
        # Dry-run commands come from one console, so work one ticket at a time.
        self.slots = asyncio.Semaphore(1 if dry_run else cfg.max_concurrent)

    def _slack(self) -> Slack:
        cls = DryRunSlack if self.dry_run else Slack
        return cls(self.cfg.slack_bot_token, self.cfg.slack_channel)

    async def work(self, permalink: str | None = None, channel: str = "", ts: str = ""):
        key = permalink or f"{channel}:{ts}"
        if key in self.seen:
            return
        self.seen.add(key)
        async with self.slots:
            slack = self._slack()
            ticket = await asyncio.to_thread(slack.get, permalink) if permalink else \
                await asyncio.to_thread(slack.get_message, channel, ts)
            if not ticket.requester_email:
                print(f"[skip]  {ticket.url}: couldn't read the requester's email "
                      f"(check the users:read.email scope)")
                return
            try:
                await asyncio.to_thread(slack.open_thread, ticket)
            except RuntimeError as e:
                print(f"[skip]  {e}\nSLACK_ESCALATION_CHANNEL must be a channel ID (C...) "
                      f"and the bot must be invited to that channel.")
                return
            run = HelpdeskRun(self.cfg, ticket, slack, self.api, self.phaze,
                              self.observe_only, self.machine_locks)
            self.runs[ticket.id] = run
            if slack.thread_ts:
                self.threads[slack.thread_ts] = ticket.id
            run.log(f"[run]   {ticket.requester_name}: {ticket.subject}")
            await asyncio.to_thread(slack.react, ticket, STATUS_EMOJI["start"])
            try:
                outcome = await run.run()
            except Exception as e:
                outcome = "failed"
                run.log(f"[error] agent crashed: {type(e).__name__}: {e}")
            finally:
                self.runs.pop(ticket.id, None)
                self.threads.pop(slack.thread_ts or "", None)
            await asyncio.to_thread(slack.react, ticket, STATUS_EMOJI.get(outcome, FAILED_EMOJI))
            run.log(f"[run]   final outcome: {outcome}")

    def route(self, ticket_id: str | None, sig: Signal) -> bool:
        run = self.runs.get(ticket_id or "")
        if run:
            run.log(f"[human] {sig.kind} {sig.text[:80]!r}")
            run.inbox.put_nowait(sig)
        return bool(run)

    def start_console(self, loop: asyncio.AbstractEventLoop):
        """Read technician commands from stdin and route them to the active ticket."""
        def reader():
            for line in sys.stdin:
                if not line.strip():
                    continue
                active = list(self.runs)
                if len(active) != 1:
                    print(f"[console] {len(active)} active tickets; commands need exactly one")
                    continue
                sig = parse_command(line, "console")
                loop.call_soon_threadsafe(self.route, active[0], sig)
        threading.Thread(target=reader, daemon=True).start()


def _report_crash(task: asyncio.Task):
    if not task.cancelled() and task.exception():
        e = task.exception()
        print(f"[error] ticket failed before it started: {type(e).__name__}: {e}")


def build_slack_app(desk: Desk, listen: bool):
    from slack_bolt.async_app import AsyncApp

    cfg = desk.cfg
    app = AsyncApp(token=cfg.slack_bot_token)

    @app.event("message")
    async def on_message(event):
        if event.get("subtype") not in TICKET_SUBTYPES or event.get("bot_id"):
            return
        channel, ts, thread = event.get("channel"), event["ts"], event.get("thread_ts")
        user = event.get("user", "")
        if channel == cfg.slack_channel and thread in desk.threads:
            desk.route(desk.threads[thread], parse_command(event.get("text", ""), user))
        elif thread and thread != ts and f"{channel}:{thread}" in desk.runs:
            # A reply in a ticket's own thread. Only the requester's count, and they are raw text
            # (never commands): the run uses them only while the requester is picking a machine.
            ticket_id = f"{channel}:{thread}"
            if user == desk.runs[ticket_id].ticket.requester_slack_id:
                desk.route(ticket_id, Signal("requester", event.get("text", ""), user))
        elif listen and channel == cfg.slack_help_channel and (not thread or thread == ts):
            print(f"[slack] new message {ts} in help channel")
            task = asyncio.create_task(desk.work(channel=channel, ts=ts))
            task.add_done_callback(_report_crash)

    @app.action(re.compile(rf"^{MACHINE_ACTION_PREFIX}\d+$"))
    async def on_machine_pick(ack, body, action, client):
        await ack()
        user = body["user"]["id"]
        ticket_id, _, machine_id = (action.get("value") or "").partition("|")
        run = desk.runs.get(ticket_id)
        if not run:
            text = "That request is no longer active."
        elif user != run.ticket.requester_slack_id:
            text = "Only the person who asked for help can pick the computer."
        else:
            desk.route(ticket_id, Signal("machine", machine_id, user))
            return
        await client.chat_postEphemeral(channel=body["channel"]["id"], user=user, text=text)

    @app.action(re.compile(r"^hd_(takeover|handback|resolved|close)$"))
    async def on_button(ack, body, action, client):
        await ack()
        user = body["user"]["id"]
        ticket_id = action.get("value")
        if not desk.route(ticket_id, Signal(ACTIONS[action["action_id"]], "", user)):
            await client.chat_postEphemeral(channel=body["channel"]["id"], user=user,
                                            text="That ticket is no longer active.")
            return
        await client.chat_postMessage(
            channel=cfg.slack_channel, thread_ts=desk.runs[ticket_id].slack.thread_ts,
            text=f"<@{user}> clicked *{action['text']['text']}*.")

    @app.action(CONNECT_ACTION)
    async def on_connect_link(ack):
        await ack()  # Slack opens the phaze:// link itself; the ack just stops Slack showing an error

    return app


async def main_async(a):
    cfg = Config.load()
    observe_only = not (a.allow_input or a.live)
    dry_run = not (a.post or a.live)
    print(f"[run]   mode: {'observe-only' if observe_only else 'INPUT ALLOWED'}, "
          f"{'dry-run' if dry_run else 'POSTING TO SLACK'}")

    desk = Desk(cfg, observe_only, dry_run)
    use_socket = bool(cfg.slack_app_token) and (not dry_run or not a.permalink)
    if not a.permalink and not (cfg.slack_app_token and cfg.slack_help_channel):
        raise SystemExit("Listening needs SLACK_APP_TOKEN (xapp-...) and SLACK_HELP_CHANNEL in .env.")
    if dry_run or not cfg.slack_app_token:
        desk.start_console(asyncio.get_running_loop())
        print("[run]   technician commands: type takeover / back <note> / resolved / close here")

    handler = None
    if use_socket:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        handler = AsyncSocketModeHandler(build_slack_app(desk, listen=not a.permalink), cfg.slack_app_token)
    try:
        if a.permalink:
            if handler:
                await handler.connect_async()
            await desk.work(permalink=a.permalink)
        else:
            print(f"[run]   listening for tickets in {cfg.slack_help_channel}")
            await handler.start_async()
    finally:
        if handler:
            await handler.close_async()
        await desk.phaze.aclose()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("permalink", nargs="?", help="one Slack message link; omit to listen")
    p.add_argument("--allow-input", action="store_true", help="let the agent click and type")
    p.add_argument("--post", action="store_true", help="really post notes/reactions to Slack")
    p.add_argument("--live", action="store_true", help="--allow-input and --post together")
    try:
        asyncio.run(main_async(p.parse_args()))
    except KeyboardInterrupt:
        print("\n[run]   stopped. A Phaze session may still be open; check the Phaze app.")


if __name__ == "__main__":
    main()
