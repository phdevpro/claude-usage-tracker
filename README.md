# Claude Code token usage tracker

Tracks Claude Code token usage per session across all your machines and
splits it into **work** and **private**.

```
 laptop A ── agent ─┐
 laptop B ── agent ─┼──► server (Docker) ──► dashboard  http://host:8080
 desktop  ── agent ─┘     SQLite + tags.json in /data
```

- **server/**: receives usage from agents, stores it, serves the dashboard.
- **agent/**: runs on each machine, reads Claude Code's `~/.claude/projects`
  and the Claude desktop app's Cowork tasks (`local-agent-mode-sessions`), and
  sends per-session totals. It sends titles, project paths, timestamps and token
  counts only, never conversation content.
  Chat conversations (claude.ai / the desktop Chat tab) are not stored locally
  with token counts, so they can't be tracked.
- `token_tracker.py` is the original single-machine version and still works on its own.

## Image publishing

Like the other phdevpro apps, the image is built by GitHub Actions
(`.github/workflows/docker.yml`): every push to `main` or a `v*` tag runs the
tests, then builds the image (`server/Dockerfile`, repo root as context, so the agent is bundled for the installers) and pushes `ghcr.io/phdevpro/claude-usage-tracker`
with the per-run `GITHUB_TOKEN`. No personal token is needed. Tags:
`latest` (main), `sha-<commit>`, and `1.2.3` for a `v1.2.3` git tag.

Run the tests locally with `python -m unittest discover -s tests -v`.

## Deploy on Portainer

Use `deploy/portainer-stack.yml` (Traefik on the external `letsencrypt`
network) and paste `deploy/stack.env.example` into the stack's environment.

## Or run it with plain Docker Compose

```bash
cp .env.example .env      # then set AGENT_TOKEN and DASHBOARD_PASSWORD
docker compose up -d --build
```

Open `http://<docker-host>:8080` and log in with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.

To bring over the tags you already set with the local version, click
**Import tags** in the dashboard and pick your `tags.json`. It is merged
into the server's tags; **Export tags** downloads the current file.

The server sends the agent token and dashboard password in plain HTTP. On
anything other than a trusted LAN, put it behind HTTPS (a reverse proxy
such as Caddy, Traefik or nginx, or a Cloudflare tunnel).

## 2. Run an agent on each machine

### Quick install from the dashboard

Click **Install agent** in the dashboard and download the installer for the
machine. The file has the agent, the server URL and the agent token built in,
so keep it private. Python 3.9+ is still needed on the machine.

- **Windows**: double-click `claude-usage-agent-windows.cmd`. It installs to
  `%LOCALAPPDATA%\ClaudeUsageAgent`, sends once and registers the
  "Claude usage agent" scheduled task (every 30 minutes).
  `claude-usage-agent-windows.cmd uninstall` removes it.
- **Linux / macOS**: `sh claude-usage-agent-linux.sh`. It installs to
  `~/.local/share/claude-usage-agent`, sends once and adds a cron line (or a
  systemd user timer without cron). `--uninstall` removes it.

The server URL in the installer is the one the browser used to open the
dashboard (`X-Forwarded-Proto`/`Host` behind a proxy). Set `PUBLIC_URL` on the
server if agents must use a different address.

### Manual install

Copy the `agent` folder to the machine (Python 3.9+ needed, no packages),
copy `agent.example.json` to `agent.json` and fill in the server URL and token.

```bash
python claude_usage_agent.py --dry-run   # scan only, check the numbers
python claude_usage_agent.py             # send once
```

Send automatically every 30 minutes on Windows (Task Scheduler, no console
window, log in `~/.claude/usage-agent.log`), from the agent folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows-task.ps1
```

macOS / Linux (cron):

```
*/30 * * * * /usr/bin/python3 /path/to/agent/claude_usage_agent.py >/dev/null 2>&1
```

The machine name defaults to the computer name; override it with
`"machine": "private-laptop"` in `agent.json`.

## Cost per tag

Set your subscription price in **Settings** at the bottom of the dashboard
(saved in `/data/settings.json`). Each line applies from its month until the
next one, so add a line when the plan or price changes.

Each month's price is split across that month's sessions (all machines) in
proportion to their weighted tokens, and a session counts in the month of its
last activity. Weights default to Anthropic's price ratios relative to an
input token: output 5, cache write 1.25, cache read 0.1. Raw token counts
would over-charge sessions with many cheap cache reads.

Claude Code deletes transcripts after ~30 days, so months before the first
agent run are incomplete. Start the first price line at the month tracking
began to avoid charging a whole month to the few sessions that survived.

### Monthly A6 report

The **A6 PDF** link next to each month in the Monthly cost table opens a
one-page report (105 × 148 mm) with tokens and cost for work and private.
Untagged sessions are left out. Click **Print / Save as PDF** and choose
"Save as PDF" in the print dialog; the page size is set automatically.

## Tagging

From strongest to weakest:

1. Tag set on a single session
2. Tag set on a title group (all sessions with the same title, e.g. scheduled runs)
3. Rules in `tags.json`: `title_rules` (keyword in title) and `path_rules` (project folder)
4. Project default

To change rules, click **Export tags**, edit the file, and **Import tags** it
back (it is merged, so existing tags are kept), e.g.:

```json
"title_rules": { "IG3": "work" },
"path_rules":  { "C:\\Users\\simone.palazzin\\Documents\\Dev": "work" }
```

Claude Code deletes transcripts older than ~30 days by default. Once an agent
has sent a session, the server keeps it, so run the agents regularly.
