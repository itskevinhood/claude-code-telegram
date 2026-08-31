# claude-code-telegram ("Bolt") — what's here and where to go

Policy and the venv trap are in `CLAUDE.md` (auto-loaded). This is the geography.

**What it is:** the Telegram bot that runs Claude Code sessions from chat — and the
alert channel every other job on this box reports failures to. Runs as the
`claude-telegram-bot.service` system unit. A fork; upstream docs still apply.

| Folder | What's in it |
|---|---|
| `src/bot/` | Telegram handlers, commands, middleware |
| `src/claude/` | Claude Code session management — the integration core |
| `src/config/` | Pydantic settings, feature flags |
| `src/security/` | Auth, rate limiting, the tool allowlist |
| `src/storage/` | SQLite persistence |
| `src/scheduler/`, `src/notifications/`, `src/events/`, `src/mcp/`, `src/api/`, `src/projects/`, `src/utils/` | Supporting subsystems |
| `docs/` | Full documentation set → `docs/CONTEXT.md` |
| `tests/` | `poetry run pytest` |

| Your task | Go here |
|---|---|
| Add a bot command | `CLAUDE.md` → "Adding a New Bot Command" |
| Change config or a feature flag | `docs/configuration.md` |
| Change what tools Claude may use | `docs/tools.md` |
| Set it up somewhere else | `docs/setup.md` |
| Local dev, tests, linting | `docs/development.md` |
| Why an alert didn't arrive | this bot is the alert *channel*; check the sending job's log first |

## 🚫 Don't

- **Don't change dependencies with poetry alone** — see the venv trap in `CLAUDE.md`.
- This bot is how every other job reports failure. Breaking it makes the whole box
  go quiet, which looks exactly like everything working.
