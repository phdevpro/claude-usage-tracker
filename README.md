# Claude Code token usage tracker

Tracks Claude Code token usage per session across all your machines and
splits it into **work** and **private**.

```
 laptop A ── agent ─┐
 laptop B ── agent ─┼──► server (Docker) ──► dashboard  http://host:8080
 desktop  ── agent ─┘     SQLite + tags.json in /data
```

- **server/**: receives usage from agents, stores it, serves the dashboard.
- **agent/**: runs on each machine, reads `~/.claude/projects` and sends
  per-session totals. It sends titles, project paths, timestamps and token
  counts only, never conversation content.
- `token_tracker.py` is the original single-machine version and still works on its own.

## Image publishing

Like the other phdevpro apps, the image is built by GitHub Actions
(`.github/workflows/docker.yml`): every push to `main` or a `v*` tag runs the
tests, then builds `server/` and pushes `ghcr.io/phdevpro/claude-usage-tracker`
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

To bring over the tags you already set with the local version:

```bash
docker cp tags.json claude-usage:/data/tags.json
```

The server sends the agent token and dashboard password in plain HTTP. On
anything other than a trusted LAN, put it behind HTTPS (a reverse proxy
such as Caddy, Traefik or nginx, or a Cloudflare tunnel).

## 2. Run an agent on each machine

Copy the `agent` folder to the machine (Python 3.9+ needed, no packages),
copy `agent.example.json` to `agent.json` and fill in the server URL and token.

```bash
python claude_usage_agent.py --dry-run   # scan only, check the numbers
python claude_usage_agent.py             # send once
```

Send automatically every 30 minutes on Windows (Task Scheduler):

```powershell
schtasks /Create /SC MINUTE /MO 30 /TN "Claude usage agent" /TR "pythonw.exe \"C:\path\to\agent\claude_usage_agent.py\""
```

macOS / Linux (cron):

```
*/30 * * * * /usr/bin/python3 /path/to/agent/claude_usage_agent.py >/dev/null 2>&1
```

The machine name defaults to the computer name; override it with
`"machine": "private-laptop"` in `agent.json`.

## Tagging

From strongest to weakest:

1. Tag set on a single session
2. Tag set on a title group (all sessions with the same title, e.g. scheduled runs)
3. Rules in `tags.json`: `title_rules` (keyword in title) and `path_rules` (project folder)
4. Project default

Edit rules in `/data/tags.json` on the server (`docker exec -it claude-usage sh`
or `docker cp`), e.g.:

```json
"title_rules": { "IG3": "work" },
"path_rules":  { "C:\\Users\\simone.palazzin\\Documents\\Dev": "work" }
```

Claude Code deletes transcripts older than ~30 days by default. Once an agent
has sent a session, the server keeps it, so run the agents regularly.
