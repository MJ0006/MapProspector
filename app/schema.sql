-- MapProspector schema. Every statement is idempotent; db.init() runs this on startup.

CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);

-- Businesses we have looked up. google_place_id is the durable key; the other
-- Places fields are a refreshable cache (fetched_at) per Google's caching terms.
CREATE TABLE IF NOT EXISTS places (
  id INTEGER PRIMARY KEY,
  google_place_id TEXT UNIQUE,
  cid TEXT,
  name TEXT NOT NULL,
  address TEXT, phone TEXT, website TEXT,
  lat REAL, lng REAL,
  rating REAL, review_count INTEGER,
  primary_category TEXT, categories TEXT,
  photos_count INTEGER, hours TEXT, business_status TEXT,
  maps_url TEXT,
  fetched_at TEXT
);

-- A1-A5 rank tracking ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_schedules (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  place_id INTEGER REFERENCES places(id) ON DELETE CASCADE,
  keywords TEXT NOT NULL,            -- JSON list
  surface TEXT NOT NULL,             -- google_maps | local_pack | organic
  provider TEXT NOT NULL,            -- places | dataforseo | fixture
  grid TEXT NOT NULL,                -- JSON {size, spacing_m, shape, polygon}
  recurrence TEXT NOT NULL,          -- daily | weekly | biweekly | monthly_day | first_monday
  day INTEGER,                       -- weekday 0-6 or day-of-month
  paused INTEGER NOT NULL DEFAULT 0,
  next_run_at TEXT, last_run_at TEXT, last_error TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY,
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  schedule_id INTEGER REFERENCES scan_schedules(id) ON DELETE SET NULL,
  keyword TEXT NOT NULL,
  surface TEXT NOT NULL,
  provider TEXT NOT NULL,
  grid_size INTEGER, spacing_m REAL, shape TEXT,
  center_lat REAL, center_lng REAL,
  status TEXT NOT NULL DEFAULT 'queue', -- queue | in_progress | completed | incomplete | failed
  error TEXT,
  stats TEXT,                        -- JSON: avg rank, top3 %, found points ...
  share_token TEXT UNIQUE,
  created_at TEXT, completed_at TEXT
);

CREATE TABLE IF NOT EXISTS scan_points (
  id INTEGER PRIMARY KEY,
  scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  lat REAL NOT NULL, lng REAL NOT NULL,
  rank INTEGER,                      -- target's rank at this point, NULL = not in top N
  results TEXT                       -- JSON list of businesses seen here, in rank order
);
CREATE INDEX IF NOT EXISTS ix_scan_points_scan ON scan_points(scan_id);

CREATE TABLE IF NOT EXISTS keyword_data (
  keyword TEXT PRIMARY KEY,
  search_volume INTEGER, cpc REAL, competition REAL, difficulty INTEGER, intent TEXT,
  fetched_at TEXT
);

-- D CRM -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (       -- clients / lead sources
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  website TEXT, phone TEXT, address TEXT, timezone TEXT,
  is_client INTEGER NOT NULL DEFAULT 0,
  vault_slug TEXT,
  review_link TEXT,
  brand_voice TEXT,                  -- notes that prime AI drafts (B1/B2)
  gbp_location TEXT,                 -- "accounts/X/locations/Y" once Google is connected
  gsc_site TEXT,                     -- Search Console property, e.g. "sc-domain:example.com"
  lock_checked_at TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  first_name TEXT, last_name TEXT, business_name TEXT,
  email TEXT, phone TEXT, website TEXT, address TEXT,
  source TEXT,                       -- prospecting | form | manual | import
  status TEXT NOT NULL DEFAULT 'new',
  tags TEXT,                         -- JSON list
  custom TEXT,                       -- JSON {field: value}
  is_spam INTEGER NOT NULL DEFAULT 0, spam_reason TEXT,
  do_not_contact INTEGER NOT NULL DEFAULT 0,
  last_activity_at TEXT, last_activity_type TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS activities (       -- contact timeline
  id INTEGER PRIMARY KEY,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
  type TEXT NOT NULL,                -- note | email | sms | call | stage | audit | form | task | automation
  body TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS pipelines (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS pipeline_stages (
  id INTEGER PRIMARY KEY,
  pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  name TEXT NOT NULL, position INTEGER NOT NULL, win_pct INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deals (
  id INTEGER PRIMARY KEY,
  pipeline_id INTEGER NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
  contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
  title TEXT NOT NULL, value REAL, status TEXT NOT NULL DEFAULT 'open', -- open | won | lost
  created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
  company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
  title TEXT NOT NULL, notes TEXT, due_at TEXT,
  done INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, done_at TEXT
);

CREATE TABLE IF NOT EXISTS forms (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
  fields TEXT NOT NULL,              -- JSON [{name,label,type,required}]
  token TEXT UNIQUE NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS form_submissions (
  id INTEGER PRIMARY KEY,
  form_id INTEGER REFERENCES forms(id) ON DELETE CASCADE,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  data TEXT, ip TEXT, country TEXT,
  is_spam INTEGER NOT NULL DEFAULT 0, spam_reason TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS spam_rules (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                -- keyword | domain | email | country | url_in_message | phone_format | ip
  value TEXT, active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS automations (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  trigger TEXT NOT NULL,             -- form_submitted | contact_created | deal_created | deal_stage | field_updated | review_request_sent | manual
  trigger_config TEXT,               -- JSON e.g. {stage_id: 3}
  filters TEXT,                      -- JSON [{field, op, value}]
  actions TEXT NOT NULL,             -- JSON [{type, ...}]
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS automation_runs (
  id INTEGER PRIMARY KEY,
  automation_id INTEGER NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
  step INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running', -- running | waiting | done | stopped | failed
  resume_at TEXT, context TEXT, log TEXT,
  created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS questionnaires (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  questions TEXT NOT NULL,           -- JSON [{id,label,type: text|longtext|choice|multi|likert, options}]
  token TEXT UNIQUE NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS questionnaire_responses (
  id INTEGER PRIMARY KEY,
  questionnaire_id INTEGER NOT NULL REFERENCES questionnaires(id) ON DELETE CASCADE,
  company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  answers TEXT NOT NULL, created_at TEXT
);

-- Outbox: every outgoing message waits for approval unless it answers inbound contact.
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,             -- email | sms
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  to_addr TEXT, subject TEXT, body TEXT,
  status TEXT NOT NULL DEFAULT 'draft', -- draft | approved | sent | failed | cancelled
  origin TEXT, error TEXT,
  created_at TEXT, sent_at TEXT
);

-- G audit / A6 AI visibility -----------------------------------------------------
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY,
  place_id INTEGER REFERENCES places(id) ON DELETE CASCADE,
  url TEXT, checks TEXT NOT NULL,    -- JSON [{key,label,group,status: pass|fail|warn|na,detail,source}]
  issues INTEGER NOT NULL DEFAULT 0,  -- -1 while running
  share_token TEXT UNIQUE, created_at TEXT
);

CREATE TABLE IF NOT EXISTS aivis_runs (
  id INTEGER PRIMARY KEY,
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  business_name TEXT NOT NULL, city TEXT, category TEXT,
  status TEXT NOT NULL DEFAULT 'queue', error TEXT,
  report TEXT,                       -- JSON report (engines, prompts, competitors, sources, score)
  share_token TEXT UNIQUE,
  created_at TEXT, completed_at TEXT
);

-- B GBP management -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gbp_posts (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  topic TEXT, body TEXT NOT NULL, cta TEXT, cta_url TEXT, photo_id INTEGER,
  status TEXT NOT NULL DEFAULT 'draft', -- draft | approved | scheduled | published | failed
  scheduled_at TEXT, published_at TEXT, error TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  external_id TEXT, author_hash TEXT, rating INTEGER, text TEXT, review_date TEXT,
  reply_draft TEXT, reply_status TEXT NOT NULL DEFAULT 'none', -- none | draft | approved | published
  reply_text TEXT, replied_at TEXT, created_at TEXT,
  UNIQUE(company_id, external_id)
);
CREATE TABLE IF NOT EXISTS review_requests (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  token TEXT UNIQUE NOT NULL, channel TEXT,
  status TEXT NOT NULL DEFAULT 'created', -- created | sent | opened | clicked | stopped
  created_at TEXT, opened_at TEXT, clicked_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (            -- optional private feedback; never filters the review link
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
  request_id INTEGER REFERENCES review_requests(id) ON DELETE SET NULL,
  message TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  filename TEXT NOT NULL, original_name TEXT, caption TEXT, category TEXT,
  gps_lat REAL, gps_lng REAL,
  status TEXT NOT NULL DEFAULT 'library', -- library | scheduled | published | failed
  scheduled_at TEXT, published_at TEXT, created_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS profile_snapshots (     -- B5 approved state of a profile
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  data TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS profile_changes (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  field TEXT NOT NULL, approved_value TEXT, live_value TEXT,
  status TEXT NOT NULL DEFAULT 'open', -- open | accepted | reverted | dismissed
  detected_at TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  provider TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT
);

-- C citations -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS citation_profiles (     -- C2 single source of truth for NAP
  company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
  data TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS citation_audits (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
  place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
  business TEXT NOT NULL,            -- JSON NAP used for matching
  status TEXT NOT NULL DEFAULT 'queue', score INTEGER, grade TEXT,
  share_token TEXT UNIQUE,
  created_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS citation_listings (
  id INTEGER PRIMARY KEY,
  audit_id INTEGER REFERENCES citation_audits(id) ON DELETE CASCADE,
  company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
  directory TEXT NOT NULL,
  status TEXT NOT NULL,              -- live | wrong | missing | pending | unavailable
  listing_url TEXT, fields TEXT,     -- JSON {name: {status, found}}
  claimable INTEGER, checked_by TEXT,-- auto | manual
  submission_status TEXT,            -- tracker: todo | submitted | live | rejected
  updated_at TEXT
);

-- F reports -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sections TEXT NOT NULL,            -- JSON list of section keys
  recipients TEXT,                   -- comma separated emails
  schedule TEXT,                     -- none | weekly | monthly
  require_approval INTEGER NOT NULL DEFAULT 1,
  next_run_at TEXT, last_error TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS report_runs (
  id INTEGER PRIMARY KEY,
  report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  html TEXT NOT NULL, token TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_approval', -- pending_approval | approved | sent | failed
  created_at TEXT, sent_at TEXT
);

-- Journal (self-improvement layer) -------------------------------------------------------------
-- Every job run, AI call, decision on AI output, and failure. Read by the Journal page, the
-- notifier, the lesson generator and the MCP server (so agents can learn from history).
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  area TEXT NOT NULL,                -- rank | aivis | audit | citations | posts | reviews | outbox | automation | report | brain | ai | job
  kind TEXT NOT NULL,                -- job | ai_call | decision | failure | outcome
  action TEXT NOT NULL,              -- e.g. run, draft, approve, edit_approve, reject, new_review
  outcome TEXT NOT NULL DEFAULT 'ok',-- ok | failed | edited | rejected | unchanged
  subject_type TEXT, subject_id INTEGER, company_id INTEGER,
  detail TEXT,
  data TEXT                          -- JSON: durations, errors, draft vs final text, etc.
);
CREATE INDEX IF NOT EXISTS ix_events_area ON events(area, kind, created_at);
CREATE INDEX IF NOT EXISTS ix_events_subject ON events(subject_type, subject_id);

-- Lessons distilled from the journal. Proposed by Claude, only used once you approve them.
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY,
  area TEXT NOT NULL,                -- posts | reviews | outbox | tools | citations | ...
  company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,  -- NULL = applies everywhere
  text TEXT NOT NULL,
  evidence TEXT,                     -- JSON list of event ids it was learned from
  status TEXT NOT NULL DEFAULT 'proposed', -- proposed | active | retired
  uses INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
