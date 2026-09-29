# Phaze API + MCP example: an AI helpdesk agent

This was 100% AI vibe coded with Opus 5.5. This example is not meant to be used for anything other than to teach you or an AI agent how to implement the Phaze API and MCP for a common IT use case. I hope this inspires ideas.

**Please note:** Phaze currently only supports a connection between a *Windows computer and another Windows computer*. It uses accelerated graphics, so this will not work on PCs without graphics capabilities (either via your integrated CPU graphics or your discrete GPU). Almost all CPUs support this other than server-class CPUs.


This repo is a working example of what you can build with the **Phaze Enterprise API** and
the **Phaze MCP server**. It's an IT helpdesk agent that watches a Slack channel. When
someone posts "my sound isn't working", it finds their computer through the API. Then it
connects to that computer through Phaze and fixes the problem by looking at the screen and
using the mouse and keyboard, the way a technician would. When it gets stuck, it pages a
human, hands them the live session, and takes it back when they're done.

It's meant as a starting point. The first half of this README teaches the Phaze API and
MCP from scratch, so you (or your AI coding agent) can build something similar or
something entirely different. The second half explains this example.

- [Phaze in 60 seconds](#phaze-in-60-seconds)
- [Part 1: The Phaze Enterprise API](#part-1-the-phaze-enterprise-api)
- [Part 2: The Phaze MCP server](#part-2-the-phaze-mcp-server)
- [Part 3: Putting them together](#part-3-putting-them-together)
- [Part 4: This example, the helpdesk agent](#part-4-this-example-the-helpdesk-agent)
- [Notes for AI coding agents](#notes-for-ai-coding-agents)

---

## Phaze in 60 seconds

[Phaze](https://phaze.app) is a high-performance remote desktop. The pieces:

| Term | Meaning |
|---|---|
| **Enterprise** | Your company's Phaze account. Contains organizations and members. |
| **Organization (org)** | A group of machines and people inside the enterprise, e.g. per office or per customer. |
| **Member** | A person with a Phaze account (`id` looks like `u_...`). |
| **Machine** | A computer running Phaze that can be connected to (a *host*). Assigned to a member *or* to a group. |
| **Group** | A set of members inside an org. Machines assigned to a group are reachable by its members. |
| **Connection** | A live remote-desktop session from a Phaze app to a machine. |
| **Guest** | A participant in a machine's session. One guest at a time has **control** (mouse and keyboard). |

The two developer surfaces do different jobs:

```
 Phaze Enterprise API  (cloud, HTTPS, API key)          Phaze MCP server  (local, in the Phaze app)
 ─────────────────────────────────────────────          ─────────────────────────────────────────────
 "the control plane": who, what, where                  "the hands": see and operate a machine
 members, orgs, machines, groups, assignments,          list reachable machines, connect, screenshot,
 connection logs, relays, locations                     click, type, scroll, hand control to a guest
```

An agent typically uses the **API** to decide *which* machine and whether it's allowed, and
the **MCP** to actually *do* something on it.

---

## Part 1: The Phaze Enterprise API

### Get an API key

Phaze is currently in beta, so we don't have a web sign up for the administrator system. First, visit https://web.phaze.app/signup to create your account. Then email founders@phaze.app from the email address that you created your account with. We will create an administrator account for you.

API keys are created in the Phaze admin portal, which needs an **administrator account**
at **[admin.phaze.app](https://admin.phaze.app)**. If you're not an admin, ask your Phaze
administrator for a key.

1. Sign in to [admin.phaze.app](https://admin.phaze.app) as an administrator.
2. Open the **Enterprise settings** page ([admin.phaze.app/settings](https://admin.phaze.app/settings)).
3. Generate an API key and store it somewhere safe, such as an `.env` file that's in
   `.gitignore`. Never commit it and never paste it into a chat.

Step-by-step help: **[The Phaze API](https://help.phaze.app/articles/6515713992-the-phaze-api)**.
Full reference: **[apidocs.phaze.app](https://apidocs.phaze.app)**.

### First request

```bash
export PHAZE_API_KEY=...   # from admin.phaze.app/settings

curl -s https://public-api.phaze.app/enterprise/v1/orgs \
  -H "Authorization: Bearer $PHAZE_API_KEY"
```

```python
import httpx

api = httpx.Client(base_url="https://public-api.phaze.app/enterprise/v1",
                   headers={"Authorization": f"Bearer {PHAZE_API_KEY}"})
orgs = api.get("/orgs").json()["data"]
machines = api.get(f"/orgs/{orgs[0]['id']}/machines", params={"limit": 200}).json()["data"]
```

### Essentials

| | |
|---|---|
| Base URL | `https://public-api.phaze.app/enterprise/v1` |
| Auth | `Authorization: Bearer <API_KEY>` |
| Responses | `{"status": "OK", "data": {...}}`, and for lists `{"status": "OK", "data": [...], "count": N}` |
| Pagination | `limit` (1–200, default 50) and `offset`; stop when `offset >= count` |
| Errors | `{"status": "Not Found", "errors": [{"code": "not_found", "message": "..."}]}` |
| Rate limit | 600 requests/min (headers `X-RateLimit-Remaining`, `Retry-After`; back off on `429`) |

Main resources (see [apidocs.phaze.app](https://apidocs.phaze.app) for every endpoint):

| Resource | Endpoints |
|---|---|
| Members | `GET /members` (`q=` searches), `PUT /members/role`, invites under `/invites` |
| Orgs | `GET/POST /orgs`, `PATCH/DELETE /orgs/{org_id}`, org members under `/orgs/{org_id}/members` |
| Machines | `GET /orgs/{org_id}/machines`, `PUT .../machines/assign-user`, `PUT .../machines/assign-group` |
| Groups | `/orgs/{org_id}/groups`, members under `/orgs/{org_id}/groups/{group_id}/members` |
| Connections | `GET /orgs/{org_id}/connections` (who connected to what, and when) |
| Also | machine keys, locations, relays |

### Things we learned building this (not obvious from the reference)

- **Machine IDs match across API and MCP.** A machine's `id` in the API equals its
  `machine_id` in the MCP (64 hex chars). That's how you join "the API says this person owns
  machine X" with "the MCP can reach X".
- **Member IDs match guests.** A member's `id` (`u_...`) is the `user` field of a guest in
  the MCP's `phaze_status`. You can tell *who* is in a session.
- **A machine has `assignee_id` or `group_id`, never both.** Assigning a machine to a group
  replaces its user assignment.
- **No member-by-ID endpoint.** To resolve a `u_...` ID to a name, list `/members` once and
  cache it (see `phaze_api.py`).
- **Machine objects** include `id`, `name`, `org_id`, `assignee_id`, `group_id`,
  `is_online`, `os`, `platform`, `location_id`, `version`.

---

## Part 2: The Phaze MCP server

The [Model Context Protocol](https://modelcontextprotocol.io) (MCP) lets an AI model call
tools. The Phaze app includes an MCP server that lets an AI operate remote machines through
Phaze: connect, look at the screen, click, type. It acts as the Phaze account that's
signed in to the app.

### Turn it on

The MCP server is currently an **experimental feature**. Help article:
**[The Phaze MCP server](https://help.phaze.app/articles/6522930271-the-phaze-mcp-server?lang=en)**.

1. Install the Phaze app and sign in.
2. In the Phaze app's **client settings**, turn on the experimental **MCP server** feature.
3. Keep the app running. It serves the MCP at **`http://127.0.0.1:41010/mcp`** (HTTP
   transport, this computer only).

### Connect an AI client

```bash
# Claude Code
claude mcp add --transport http phaze http://127.0.0.1:41010/mcp

# Codex
codex mcp add phaze --url http://127.0.0.1:41010/mcp
```

Claude Desktop (**Settings → Developer → Edit Config**, then fully quit and reopen):

```json
{
  "mcpServers": {
    "phaze": { "command": "npx", "args": ["-y", "mcp-remote@latest", "http://127.0.0.1:41010/mcp"] }
  }
}
```

ChatGPT needs OpenAI's Secure MCP Tunnel, because it can't reach `127.0.0.1` directly; see
the help article. After connecting, try: *"List my Phaze hosts, connect to one, and tell me
what's on its screen."*

### Call it from code, no MCP SDK needed

The server speaks plain JSON-RPC over HTTP, so any language can call it:

```bash
curl -s -X POST http://127.0.0.1:41010/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"phaze_status","arguments":{}}}'
```

The result's `content[0].text` is a JSON string. `phaze_mcp.py` is a 60-line async client
that does this, with retries.

### Tools

| Tool | Arguments | What it does |
|---|---|---|
| `phaze_list_hosts` | none | Machines this account can reach: `machine_id`, `peer_id`, `name`, `online`, `requires_relay` |
| `phaze_connect` | `host_peer_id`, `machine_id`, `relay?` | Start a connection. Returns a `connection_id` immediately; it's live once `phaze_status` shows `connected: true`. Pass `relay=true` only if `requires_relay`. |
| `phaze_status` | none | App state and every connection: `owner`, `connected`, `guests`, who `has_control`, monitors (`outputs`) |
| `phaze_screenshot` | `connection_id`, `output?` | PNG of a monitor. Mouse coordinates are pixels in this image. |
| `phaze_set_control` | `connection_id`, `guest_id` | Give control to a guest: your own `is_self` guest to act, a human's to hand over, `0` to release |
| `phaze_left_click`, `phaze_right_click`, `phaze_middle_click`, `phaze_double_click`, `phaze_triple_click` | `connection_id`, `coordinate: [x, y]`, `output?` | Clicks. `phaze_left_click` also takes a modifier in `text` (`shift`, `ctrl`, `alt`, `super`) |
| `phaze_left_click_drag` | `connection_id`, `start_coordinate`, `coordinate` | Drag |
| `phaze_left_mouse_down` / `_up`, `phaze_mouse_move` | `connection_id`, `coordinate` | Fine-grained mouse control |
| `phaze_scroll` | `connection_id`, `coordinate`, `scroll_direction`, `scroll_amount?` | Scroll |
| `phaze_send_keys` | `connection_id`, `text`, `modifiers?` | Type text. Named keys use brackets: `[Enter]`, `[Escape]`, `[F1]`. Modifiers: `ControlLeft`, `ShiftLeft`, `AltLeft`, `MetaLeft`, … |
| `phaze_disconnect` | `connection_id` | End a connection |

### How sessions, guests and control work

A `phaze_status` payload looks like this (trimmed):

```json
{"connected": true, "connections": [{
  "connection_id": "connection-1a2b…", "owner": "agent", "connected": true,
  "host_machine_id": "9f8e…", "has_control": true,
  "guests": [{"guest_id": 1234567, "user": "u_abc…", "has_control": true, "is_self": true}],
  "outputs": [{"id": 111, "primary": true, "width": 1920, "height": 1080}]}]}
```

- **`owner`**: `"agent"` means the connection was opened through the MCP. `"user"` means it's
  the human's own session in the Phaze app. Don't take over or disconnect `"user"`
  connections unless that's really intended.
- **Guests are per machine, not per connection.** Everyone connected to the same machine
  shows up in every connection's `guests` list, including your own other connections.
  `is_self` marks yours. Tell people apart by `user` (a member ID).
- **One guest has control at a time.** Input tools only work while your `is_self` guest
  has control. Take it with `phaze_set_control(your guest_id)`. Control lands
  asynchronously, so poll `phaze_status` before sending input.
- **Handing off to a human** is `phaze_set_control(conn, their guest_id)`. **Taking it back**
  is `phaze_set_control(conn, your guest_id)`.

### Practical tips

- **Connecting takes time.** Expect several seconds, sometimes 30+. Poll `phaze_status`.
- **Transient errors.** While a connection is landing, the app can briefly answer
  `main loop job timed out during execution`. Retry with backoff.
- **Always screenshot after acting.** Treat each click as a hypothesis and verify it.
- **The MCP acts as the signed-in Phaze account** and can reach whatever that account can.
  For automation, sign the app in as a dedicated account whose access you control.
- **Deep link for humans:** `phaze://connect?id=<machine_id>` opens the Phaze app and
  connects to that machine. It's handy as a button in Slack, email, or a web page.

---

## Part 3: Putting them together

The pattern this repo uses, which works for most agents:

1. **Decide with the API.** Look up the person and their machines, and turn that into an
   allow-list of `machine_id`s.
2. **Act with the MCP.** Give an AI model the MCP tools, plus your own tools for your
   business logic.
3. **Enforce in code, not only in the prompt.** Put a hook in front of every tool call that
   checks it against the allow-list. For example: only allowed machines, never `owner:
   "user"` sessions, never grab control from someone else.
4. **Keep humans in the loop deterministically.** Waiting for people, detecting that they
   joined, and moving control are done by your code calling the MCP directly, not by the model.

Ideas for other things to build:

- **Scheduled maintenance agent:** each night, use the API to find machines in an org,
  connect, and run updates or check disk space. Post a report.
- **Onboarding assistant:** after `POST /invites`, assign a machine with
  `PUT /machines/assign-user`, connect, and set up the apps a new hire needs.
- **Session audit bot:** pull `GET /orgs/{org_id}/connections` into your SIEM or a weekly
  Slack summary of who accessed which machines.
- **Access requests:** a Slack or ServiceNow workflow that grants access by assigning
  machines to a group, and revokes it later.
- **Render-farm or lab monitor:** screenshot long-running jobs on many machines and alert
  when something looks stuck.
- **Guided support:** instead of fixing it, the agent connects and walks the user through
  the steps while a human watches.

---

## Part 4: This example, the helpdesk agent

```
#it-help msg ─▶ Phaze Enterprise API ─▶ Phaze MCP ─▶ resolved ✅
                (who is this user,      (connect,     │
                 which machine)          see, act)    ├─ ask_technician ─▶ tech replies ─▶ agent continues
                                                      │
                                                      └─ request_handoff ─▶ page in #it-escalations
                                                            ─▶ tech clicks "Connect in Phaze", gets control
                                                            ─▶ "Hand back to agent" ─▶ agent continues
                                                            ─▶ or Mark resolved / Close
```

Built with the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) (Python)
and [Slack Bolt](https://slack.dev/bolt-python) in Socket Mode, so it needs no public URL.

### What happens on a ticket

1. Someone posts in the help channel. The bot reacts 👀.
2. The agent calls the API to find the requester's machines. If they have several and the
   message doesn't say which, it replies in their thread with a numbered list and a button
   per machine, and waits for their pick.
3. It connects through the MCP, takes control, and works in small verified steps.
4. It finishes one of three ways:
   - **Resolved:** posts notes for technicians and marks it ✅.
   - **Needs a fact:** `ask_technician` posts a question in the escalation thread and keeps
     the session until someone replies.
   - **Needs a human:** `request_handoff` releases control and pages the escalation
     channel. The page has a **Connect in Phaze** button (`phaze://connect?id=…`). When the
     technician joins, the code gives them control. **Hand back to agent** resumes the
     same agent conversation with the technician's notes.

The requester sees emoji reactions (👀 working, ✅ resolved, 🙋 with a human, ☑️ closed,
⚠️ failed), plus the machine picker when it's needed. Everything else stays in the
private escalation thread.

### Files

| File | What's in it |
|---|---|
| `run.py` | Entry point. Slack listener (Socket Mode), buttons, routing of replies, concurrency |
| `agent.py` | One ticket: the Agent SDK session, custom tools, guardrail hook, and the ask / handoff / handback state machine |
| `phaze_api.py` | Minimal Phaze Enterprise API client (pagination, 429 retry, member lookup) |
| `phaze_mcp.py` | Minimal direct Phaze MCP client used by the orchestrator (status, control, disconnect) |
| `ticketing.py` | Slack: tickets, the escalation thread, buttons, machine picker, technician commands |
| `prompts.py` | The agent's system prompt |
| `config.py` | Settings from `.env` |
| `tests/test_flow.py` | Offline tests of the state machine with fake Slack, Phaze and API |

### Setup

You need:
- A computer running the **Phaze app** with the MCP enabled (Part 2). Ideally it's signed in
  as a dedicated account such as `helpdesk-agent@yourco.com` that can reach the machines
  it should support.
- A **Phaze API key** from an administrator account at [admin.phaze.app](https://admin.phaze.app) (Part 1).
- An **Anthropic API key** ([console.anthropic.com](https://console.anthropic.com)).
- **Python 3.10+** and the **Claude Code CLI**, which the Agent SDK drives: `npm install -g @anthropic-ai/claude-code`.
- A **Slack workspace** where you can create an app.

**1. Install**

```bash
git clone https://github.com/boxerbk/phaze-mcp-api-helpdesk-example
cd phaze-mcp-api-helpdesk-example
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

**2. Create the Slack app** at [api.slack.com/apps](https://api.slack.com/apps):

- **Socket Mode:** turn it on. Create an app-level token with `connections:write` and put
  it in `SLACK_APP_TOKEN`.
- **OAuth & Permissions → Bot token scopes:** `channels:history`, `groups:history`,
  `chat:write`, `reactions:write`, `users:read`, `users:read.email`. Put the bot token in
  `SLACK_BOT_TOKEN`.
- **Event Subscriptions:** turn on **Enable Events**, and under **Subscribe to bot events**
  add `message.channels`, plus `message.groups` if a channel is private. **Save Changes.**
- **Interactivity & Shortcuts:** turn it on. No request URL is needed with Socket Mode.
- **Install App → Reinstall to Workspace** after any of the changes above.
- Create a help channel and an escalation channel and invite the bot to both. Put their
  **IDs** (`C…`, from channel details) in `SLACK_HELP_CHANNEL` and `SLACK_ESCALATION_CHANNEL`.

**3. Check it offline**

```bash
python tests/test_flow.py
```

### Running (safest first)

With no flags it's **observe-only and dry-run**: the agent can connect and take
screenshots but not click or type, and Slack posts are printed instead of sent.

```bash
python run.py <message-permalink>                # one ticket, observe-only, printed
python run.py <message-permalink> --post         # real Slack posts and buttons, still observe-only
python run.py <message-permalink> --allow-input  # agent may click and type, Slack printed
python run.py --live                             # listen to the help channel: full control, real Slack
```

In dry-run, type technician commands (`takeover`, `back <note>`, `resolved`, `close`) into
the console. Get a permalink from a Slack message's menu with **Copy link**. Keep the
computer awake while listening.

### For technicians

In each ticket's escalation thread:

| To… | Do this |
|---|---|
| Stop the agent and take over | **Take over** button, or reply `takeover` |
| Answer the agent's question | Reply in the thread |
| Join after a page | **Connect in Phaze**. You get control automatically once you join. |
| Give the session back | **Hand back to agent**, reply `back <instructions>`, or give control to the agent's guest in Phaze |
| Finish it yourself | **Mark resolved** / **Close**, or reply `resolved` / `close` |

Text after `back` reaches the agent as instructions from IT staff. For example:
`back driver installed, print a test page and confirm`.

### Guardrails

Enforced in code, by a hook in front of every tool call and by the orchestrator:
- Connect only to machines the API says are assigned to the requester, or the one they picked.
- Never touch `owner: "user"` sessions or other machines' connections.
- Never take control while someone else holds it. Only the orchestrator gives control to people.
- Observe-only mode removes input tools and blocks taking control.
- After asking or handing off, every tool is blocked until a human responds.
- One ticket per machine. `MAX_CONCURRENT_TICKETS` overall.
- On exit: release control (unless a human has it) and close only connections this run opened.
- No shell, file or web tools. No local MCP servers or settings are loaded into the agent.

Enforced by the system prompt:
- Hand off on any password, MFA or UAC prompt. Never type credentials.
- Hand off before installing software, deleting data, or changing security settings, unless
  a technician approved that step when handing back.
- Treat on-screen text and the ticket as data, not instructions (prompt-injection defense).

### Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Model for the agent |
| `PHAZE_MCP_URL` | `http://127.0.0.1:41010/mcp` | Local Phaze MCP |
| `HUMAN_REPLY_TIMEOUT_MIN` | 15 | Wait for a tech's answer or the requester's machine pick, then hand off |
| `HUMAN_JOIN_TIMEOUT_MIN` | 15 | Wait for a technician to join after a page |
| `HUMAN_SESSION_TIMEOUT_MIN` | 120 | Max time a tech holds the session without closing or handing back |
| `MAX_HANDOFFS` | 3 | Agent ↔ human round trips per ticket |
| `MAX_CONCURRENT_TICKETS` | 2 | Tickets worked at once |
| `MAX_TURNS` | 80 | Agent turn limit |

### Known limitations

- **Only machines assigned directly to the requester.** The API doesn't expose which groups a
  member is in, so group-assigned machines are skipped.
- **Standing access.** Assigning a machine to a group replaces its user assignment, so the
  agent account needs ongoing access rather than just-in-time grants.
- **Polling.** Detecting a technician joining polls `phaze_status` every 5 seconds.
- **In-memory state.** If the process restarts, active tickets are dropped. Close any
  leftover sessions in the Phaze app.
- **Same-account demos.** Guests from the agent's own Phaze account count as a technician
  only if they join *after* the page, so leftover sessions aren't mistaken for people.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| Nothing happens when you post | Event Subscriptions not enabled, `message.channels` missing, or the app wasn't reinstalled. The terminal prints `[slack] new message …` when events arrive. |
| `Phaze MCP not connected` | Phaze app not running, MCP feature not enabled, or wrong `PHAZE_MCP_URL` |
| `main loop job timed out` in logs | The Phaze app is busy while a connection lands. It's retried automatically. |
| Agent says another guest has control | A leftover session holds control of that machine. Close it in the Phaze app. |
| `No Phaze member found` | The Slack user's email doesn't match their Phaze account email |

---

## Notes for AI coding agents

If you're an AI helping someone build on Phaze, start here:

1. **Confirm access first.** Run `phaze_status` through the MCP (or the curl above) and
   `GET /orgs` with the API key. If the MCP call fails, the Phaze app isn't running or the
   experimental MCP feature is off. If the API call returns `401`, the key is wrong or
   missing; keys come from an administrator at [admin.phaze.app](https://admin.phaze.app).
2. **Never ask the user to paste keys into chat.** Have them put keys in `.env`.
3. **Join API and MCP data** on `machine_id` (the API machine `id`) and on member `id` = guest `user`.
4. **Connecting is asynchronous:** `phaze_connect` → poll `phaze_status` until
   `connected: true` → `phaze_set_control(own guest_id)` → poll until `has_control` → act.
5. **Respect other people's sessions.** Check `owner`, and don't take control from a guest
   who holds it unless the user explicitly wants that.
6. **Screenshot, act, screenshot.** Coordinates are pixels in the latest screenshot of that `output`.
7. Reuse `phaze_api.py` and `phaze_mcp.py` from this repo: they're small, dependency-light
   clients you can copy into a new project.
8. Reference: [apidocs.phaze.app](https://apidocs.phaze.app) ·
   [The Phaze API](https://help.phaze.app/articles/6515713992-the-phaze-api) ·
   [The Phaze MCP server](https://help.phaze.app/articles/6522930271-the-phaze-mcp-server?lang=en)

## License

[MIT](LICENSE)
