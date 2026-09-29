import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise SystemExit(f"Missing required env var: {name}")
    return val


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, "").strip() or default)


@dataclass(frozen=True)
class Config:
    model: str
    phaze_api_key: str
    phaze_mcp_url: str
    slack_bot_token: str
    slack_app_token: str        # xapp-..., for Socket Mode (listener + buttons)
    slack_help_channel: str     # channel ID where employees post tickets
    slack_channel: str          # channel ID of the private escalation channel
    human_join_timeout_min: int     # after a handoff page, how long to wait for a tech to join
    human_reply_timeout_min: int    # how long to wait for an answer to ask_technician
    human_session_timeout_min: int  # how long a tech may hold control before the run gives up
    max_handoffs: int               # agent<->human round trips per ticket
    max_concurrent: int
    max_turns: int

    @classmethod
    def load(cls) -> "Config":
        return cls(
            model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-sonnet-5-5",
            phaze_api_key=_req("PHAZE_API_KEY"),
            phaze_mcp_url=os.getenv("PHAZE_MCP_URL", "").strip() or "http://127.0.0.1:41010/mcp",
            slack_bot_token=_req("SLACK_BOT_TOKEN"),
            slack_app_token=os.getenv("SLACK_APP_TOKEN", "").strip(),
            slack_help_channel=os.getenv("SLACK_HELP_CHANNEL", "").strip(),
            slack_channel=_req("SLACK_ESCALATION_CHANNEL"),
            human_join_timeout_min=_int("HUMAN_JOIN_TIMEOUT_MIN", 15),
            human_reply_timeout_min=_int("HUMAN_REPLY_TIMEOUT_MIN", 15),
            human_session_timeout_min=_int("HUMAN_SESSION_TIMEOUT_MIN", 120),
            max_handoffs=_int("MAX_HANDOFFS", 3),
            max_concurrent=_int("MAX_CONCURRENT_TICKETS", 2),
            max_turns=_int("MAX_TURNS", 80),
        )
