# Self-heal roadmap — alerts Bolt can read, triage, and fix

**Status:** Phases 0–3 built (3 goes live on the next bot restart), plus the watchdog; Phase 4 is next. Written 2026-09-24.

**Goal:** every error that reaches Bolt gets diagnosed. If it can be fixed safely
without Kevin, Bolt fixes it and reports what it did. If it can't, Bolt posts
"here's what's probably going on, do you want A or B?" and Kevin answers in the
thread to get it done.

## Why this doesn't work today

1. **Bolt never sees its own alerts.** Every job sends alerts with Bolt's bot token,
   and Telegram doesn't deliver a bot's own messages back to it as updates. As far
   as the bot knows, the alerts don't exist.
2. **Replying to an alert doesn't help either.** When Kevin replies, Telegram does
   include the original message in `update.message.reply_to_message`, but
   `src/bot/orchestrator.py` never reads it. Claude gets "what's wrong here?" with
   no "here".
3. **Every job has its own copy of the alert code.** There are about 10 senders,
   each with a hand-copied notifier and free-text messages. There's no job ID, no
   severity, and no log path in a form a machine can read.
4. **The upstream webhook path is off and not safe for this.** `src/api/` +
   `src/events/handlers.py` already turn a webhook into a Claude run and post the
   answer to Telegram. But `ENABLE_API_SERVER` is off (nothing listens on 8080),
   and `handle_webhook` resumes the default user+directory session, which is
   Kevin's own chat. Alert triage would get mixed into his conversation. The answer
   also isn't linked to the alert, so replies can't continue it.

## Alert senders (inventory, 2026-09-24)

| Sender | Kind | Bolt creds |
|---|---|---|
| `~/bin/ram-monitor.sh` | cron, host | shared file |
| `automations/campaign-replies-to-notion` | cron | shared file |
| `automations/new-subscriber-research` | gateway route | shared file |
| `automations/sheet-leads-to-ghl` | gateway route | shared file |
| `automations/substack-to-ghl` | gateway route | shared file |
| `automations/ghl-contact-backup` | cron 03:30 | own `.env` |
| `automations/sync-email-metrics-to-notion` | cron :15 | own `.env` |
| `automations/weekly-security-audit` (`send-telegram.sh`) | cron Fri | own `.env` |
| `threads-sync` (`notifier.py`, `monitor.py`, `x_monitor.py`, `substack_monitor.py`) | cron | own `.env` |
| `fathom-to-notion/webhook_server.py` | user service | own `.env` |
| `claude-code-telegram/scripts/sdk-bump-check.sh` | cron Tue | shared file |
| `whoop-mcp` | *health alert still pending* | — |

Re-run the inventory before starting Phase 2. It will drift:
`grep -rlE "api\.telegram\.org|bolt/telegram\.env|send_telegram" ~/bin ~/automations ~/<project>`

## Phases

Each phase ships on its own and is useful without the next one.

### Phase 0 — Bolt on Opus 5.5 ✅ (2026-09-24)

SDK 0.2.159 (bundled Claude Code 2.1.281), the `CLAUDE_EFFORT` setting, and the
weekly `scripts/sdk-bump-check.sh`. With `CLAUDE_MODEL` unset, the CLI's default
model is `claude-opus-5-5`.

### Phase 1 — Replies carry the alert ✅ (2026-09-24)

When a message is a reply, put the replied-to message into the prompt (text or
caption, author, timestamp) as quoted context. Kevin replies "what's wrong?" to any
alert and Bolt investigates with the alert in hand. No notifier changes needed.

- Change: `src/bot/orchestrator.py` (agentic text handler) plus tests.
- Deploy: bot restart (Kevin says go).
- Docs: one line in this repo's `CONTEXT.md` ("reply to an alert to triage it").

### Phase 2 — One alert contract, one sender ✅ (2026-09-24)

> **Deferred cleanup (Kevin, 2026-09-24):** once the new alerts have proven themselves
> in production, remove the now-unused `TELEGRAM_*` lines from the real `.env` files of
> ghl-contact-backup, sync-email-metrics-to-notion, threads-sync and fathom-to-notion,
> and delete weekly-security-audit's `.env`. Kevin edits those; Claude never opens them.

Shipped as `~/bin/bolt-alert` + `~/docs/conventions/alerts.md`. Every sender in the
inventory below now calls it; no job loads a Telegram token. `fathom-to-notion` goes
live on its next service restart. History is kept in `~/state/alerts.jsonl`. The design
below is kept for reference; where it differs, the convention doc is the source of truth.

Replace the per-job notifier copies with one shared sender (a Python module plus a
thin bash wrapper). Every alert becomes a structured event:

```
job          "ghl-contact-backup"         stable ID, matches the folder
severity     info | warn | error
summary      one line, human
details      error text / traceback tail (capped)
log          absolute path to the log to read first
fingerprint  job + error class, for dedupe
heal         auto | ask | never           what Bolt is allowed to do (default: ask)
```

The sender does two things:
- **Posts to Telegram exactly as today.** Kevin always gets the alert, even if
  everything else is down.
- **POSTs the event to the bot's local ingress** (Phase 3), best-effort. If the
  ingress is down, the Telegram message alone still goes out. An alert is never
  lost because triage failed.

This also finishes the move to the shared creds file for the 5 senders still on
their own `.env`.

- Migrate **one job per commit**, starting with the smallest (`ram-monitor.sh`).
  Dry-run each with `MONITOR_DRY_RUN=1`.
- Docs, per migrated job: its `CONTEXT.md` gets an **Alerts** line (job ID, where
  its log is, `heal` policy). Server-wide: `STRUCTURE.md`/`SECRETS.md` say "new
  jobs alert through the shared sender, never a copied notifier", and the
  `feedback_shared_bolt_token` memory is updated to match.

### Phase 3 — Triage: alert in, diagnosis out ✅ built 2026-09-24 (at high effort)

**How it works.** `bolt-alert` sends the alert, then (for `warn`/`error` with
`heal != never`) drops the event, including the Telegram message ID, into
`~/state/bolt-triage-queue/`. Bolt's `TriageWorker` (`src/triage/`) claims one file at
a time, runs a **separate, read-only Claude session** (never Kevin's chat session),
and replies to the alert's message with `🩺 Triage`: a verdict line (✅ Transient /
🔧 Fix available / 🙋 Needs you), likely cause, confidence, evidence, and up to 3 options.
Rows live in `data/triage.db` (own file, not the bot DB), with `session_id` and
`triage_message_id` stored for Phase 4.

**Changed from the plan: a queue folder instead of the HTTP API server.** No port,
no Bearer secret for Kevin to set, and alerts sent while Bolt is down wait in the
folder (skipped as stale after 3 h). Deleting the folder stops queueing.

**Gates (in order):** info or `heal: never` → skipped · older than 3 h → stale ·
same fingerprint triaged in the last 6 h → a short "same issue" reply · spend today at
or above **$5 API-equivalent** → paused · any plan usage window at or above **70%**, or a
usage warning → paused (paused alerts get at most one ⏸ note per hour per reason) ·
free RAM under 700 MB → the queue waits. The watchdog and deploy alerts are
`heal: never` (Bolt can't triage itself).

**Plan-usage meter:** every Claude run in the bot (chats and triage) records the
SDK's `RateLimitEvent`. Its raw `unifiedWindows` give real 5-hour and 7-day
utilization on OAuth (verified: 10% / 37% on 2026-09-24).

**Security model (verified live 2026-09-24 with fake secrets and prompt injection):**
- Without extra rules, Claude Code auto-approves commands it considers read-only,
  and `grep -r` or globs (`.e*`) then read denied `.env` files. So the triage
  settings add `ask` rules that send **every** Bash/Read/Glob/Grep call to our
  deny-by-default `can_use_tool` (`src/triage/policy.py`): a read-only command
  allowlist, no recursion/globs/redirects/variables, `cd`-aware secret-path checks,
  and `ps` without its env-printing `e` form.
- Deny rules for secret paths (`.env`, tokens, `~/.config/bolt`, `/etc/whoop-mcp`,
  `/proc`, keys) plus `env`/`printenv`/`sudo`.
- No settings files are loaded (`~/.claude/settings.json` auto-allows `python3 -c`);
  no Write/Edit/Task/WebFetch/WebSearch; secret-looking env vars are blanked for the
  subprocess.
- Result: a cooperative "run these" test and a prompt-injection alert leaked nothing.
  A real triage of the swap warning took 14 s and $0.27.

**Deploy:** a bot restart (Kevin says go). Settings: `docs/configuration.md` → Alert Triage.

### Phase 4 — Kevin answers in the thread

A reply to the triage message, or to the alert, looks up the alert by
`reply_to_message_id` and **resumes that alert's session** with write tools. Kevin
says "do option 2", and it runs with full context. **Both input styles (decided 2026-09-24):**
inline buttons for the offered options, plus a typed reply for a custom instruction.

- Ask-first actions stay ask-first. The reply *is* the approval, but only for the
  action it names.
- Report back in the thread: what changed, commit sha, verification run.
- Update the alert's status in the table (`fixed` / `dismissed`).

### Phase 5 — Self-heal (auto-fix)

Auto-fixing is opt-in **per job**, through a runbook section in that job's docs:

```
## Runbook
Auto-fixable:  re-run after a transient 429/5xx; clear a stale lock file older than 1h; …
Always ask:    anything else
```

- **Never automatic, whatever a runbook says** (from `~/CLAUDE.md`): sending
  email or anything reaching real recipients, production data writes (GHL
  contacts, DBs), migrations, and restarting or flipping live infrastructure.
  These are hard-coded in the triage policy, not left to the prompt.
- **Shadow mode first.** For about 2 weeks per job, Bolt posts "I would have done
  X" instead of doing it. Promote the job to real auto-fix once Kevin agrees with
  its calls.
- Every auto-fix is reported in the alert thread (what, sha, verification) and
  recorded in the `alerts` table. Nothing heals silently.
- Docs: each promoted job's runbook, plus a table here of which jobs are
  auto-heal enabled.

## Decisions (Kevin, 2026-09-24)

- **Phase 4 input:** buttons for the offered options, plus a typed reply for a custom fix.
- **Triage budget:** $5/day API-equivalent, and pause at ≥ 70% plan utilization (Phase 3).
- **First Phase 5 jobs:** `sync-email-metrics-to-notion` and `threads-sync`. They're
  read-mostly, retries are safe, and they run often. Anything that sends email or writes
  GHL contacts stays ask-only.
- **Who watches Bolt:** `scripts/bolt-watchdog.sh`, see below.

## Bolt watchdog ✅ (2026-09-24)

`scripts/bolt-watchdog.sh`, run from cron every 5 minutes. It alerts through the Bot API
directly, so it works while the bot process is down. It pings after 2 consecutive failed
checks (so 10 minutes, which skips blips that systemd's `Restart=on-failure` already
fixes), sends a reminder every hour while the bot stays down, and sends one "recovered"
message. It also flags a crash loop (≥ 3 restarts between checks). It only alerts and
never restarts the bot: restarting live infra stays Kevin's call.
