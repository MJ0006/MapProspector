# MapProspector: notes for Claude Code

Personal local-SEO app (FastAPI + SQLite). Feature map: `docs/feature-catalog.md`. SEO logic comes from the
vendored, **unmodified** `vendor/local-seo-brain`; don't edit it, wrap it in `app/brain.py`.

## Before changing a workflow
1. Check what has actually been failing. With the app's MCP server (`.mcp.json`), call
   `get_journal(kind="failure", days=30)`; or open `/journal`. Fix causes that recur, not one-offs.
2. For AI-drafting changes, read `get_journal(kind="decision")`: edited/rejected drafts include the AI text and
   the owner's final text. Prefer improving prompts or adding lessons over hard-coding.
3. Run `python3 -m pytest -q`. Tests must never call the real `claude` CLI (conftest hides it) or live APIs
   (use fixtures in `tests/fixtures`).

## Rules that must hold
- No cold message leaves without approval (`app/mailer.py`). Cold emails need the postal address.
- Review requests send every customer to the same Google review link. Never add review gating.
- No invented scores: any grade shows its formula. No ranking guarantees.
- New background work: register recovery with `@jobs.on_startup`, take rows with `db.claim()`, and record
  failures with `journal.failure()`.
- Windows app (`desktop/`) only starts and shows this same server; build features in `app/`, not there. The
  server runs from `%APPDATA%\MapProspector` (`DATA_DIR`, `MAPPROSPECTOR_ENV_FILE`), so never assume `./data` or `./.env`.
- New DB columns: add them to `app/schema.sql` **and** `db.COLUMNS` (migrates existing databases).
