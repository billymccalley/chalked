PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  handle TEXT NOT NULL UNIQUE,
  email TEXT UNIQUE,
  email_verified_at TEXT,
  display_name TEXT,
  avatar_url TEXT,
  last_handle_change_at TEXT,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL,
  user_agent TEXT,
  ip_address TEXT,
  last_seen_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_login_aliases (
  alias TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  email TEXT,
  purpose TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_outbox (
  id TEXT PRIMARY KEY,
  recipient TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  error TEXT,
  sent_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leagues (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  owner_id TEXT NOT NULL REFERENCES users(id),
  visibility TEXT NOT NULL DEFAULT 'open',
  bankroll INTEGER NOT NULL DEFAULT 1000,
  min_stake INTEGER NOT NULL DEFAULT 10,
  max_stake INTEGER NOT NULL DEFAULT 500,
  min_mult REAL NOT NULL DEFAULT 1.2,
  max_mult REAL NOT NULL DEFAULT 4.0,
  streak_step REAL NOT NULL DEFAULT 10,
  streak_cap REAL NOT NULL DEFAULT 50,
  margin_bonus REAL NOT NULL DEFAULT 0.25,
  matchups_per_slate INTEGER NOT NULL DEFAULT 12,
  drift INTEGER NOT NULL DEFAULT 70,
  avatar_url TEXT,
  playoff_enabled INTEGER NOT NULL DEFAULT 1,
  playoff_size INTEGER NOT NULL DEFAULT 8,
  season_weeks INTEGER NOT NULL DEFAULT 10,
  playoff_weeks INTEGER NOT NULL DEFAULT 3,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
  league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member',
  display_name TEXT,
  avatar_url TEXT,
  joined_at TEXT NOT NULL,
  PRIMARY KEY (league_id, user_id)
);

CREATE TABLE IF NOT EXISTS players (
  id TEXT PRIMARY KEY,
  external_id TEXT,
  name TEXT NOT NULL,
  team TEXT NOT NULL,
  position TEXT NOT NULL,
  stat_group TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slates (
  id TEXT PRIMARY KEY,
  league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
  week INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  game_date TEXT,
  locks_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (league_id, week)
);

CREATE TABLE IF NOT EXISTS matchups (
  id TEXT PRIMARY KEY,
  slate_id TEXT NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
  stat_key TEXT NOT NULL,
  stat_label TEXT NOT NULL,
  unit TEXT NOT NULL,
  player_a_id TEXT NOT NULL REFERENCES players(id),
  player_b_id TEXT NOT NULL REFERENCES players(id),
  game_pk TEXT,
  game_pk_a TEXT,
  game_pk_b TEXT,
  game_start_a TEXT,
  game_start_b TEXT,
  game_status_a TEXT,
  game_status_b TEXT,
  inning_a TEXT,
  inning_b TEXT,
  live_state_a TEXT,
  live_state_b TEXT,
  opponent_a TEXT,
  opponent_b TEXT,
  eligibility_role_a TEXT,
  eligibility_role_b TEXT,
  eligibility_reason_a TEXT,
  eligibility_reason_b TEXT,
  eligibility_confidence_a TEXT,
  eligibility_confidence_b TEXT,
  game_start TEXT,
  game_status TEXT,
  inning TEXT,
  live_state TEXT,
  stat_current_a REAL,
  stat_current_b REAL,
  last5_a TEXT,
  last5_b TEXT,
  projection_line REAL NOT NULL,
  pub_a INTEGER NOT NULL,
  pub_b INTEGER NOT NULL,
  pub_tie INTEGER NOT NULL DEFAULT 80,
  winner_side TEXT,
  actual_a REAL,
  actual_b REAL,
  margin_bonus_hit INTEGER NOT NULL DEFAULT 0,
  stat_source TEXT,
  stat_synced_at TEXT,
  settled_at TEXT,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS picks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
  slate_id TEXT NOT NULL REFERENCES slates(id) ON DELETE CASCADE,
  matchup_id TEXT NOT NULL REFERENCES matchups(id) ON DELETE CASCADE,
  side TEXT NOT NULL,
  stake INTEGER NOT NULL,
  mult_at_lock REAL NOT NULL,
  payout INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  UNIQUE (user_id, matchup_id)
);

CREATE TABLE IF NOT EXISTS standings (
  league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  season INTEGER NOT NULL DEFAULT 0,
  streak INTEGER NOT NULL DEFAULT 0,
  wins INTEGER NOT NULL DEFAULT 0,
  losses INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (league_id, user_id)
);

CREATE TABLE IF NOT EXISTS playoff_matchups (
  id TEXT PRIMARY KEY,
  league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
  round_no INTEGER NOT NULL,
  matchup_no INTEGER NOT NULL,
  week INTEGER NOT NULL,
  seed_a INTEGER,
  seed_b INTEGER,
  user_a_id TEXT REFERENCES users(id),
  user_b_id TEXT REFERENCES users(id),
  winner_user_id TEXT REFERENCES users(id),
  score_a INTEGER NOT NULL DEFAULT 0,
  score_b INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (league_id, round_no, matchup_no)
);

CREATE TABLE IF NOT EXISTS activity_events (
  id TEXT PRIMARY KEY,
  league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
  user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  metadata TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_picks_league_user ON picks(league_id, user_id);
CREATE INDEX IF NOT EXISTS idx_matchups_slate ON matchups(slate_id);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_playoffs_league ON playoff_matchups(league_id, round_no, matchup_no);
CREATE INDEX IF NOT EXISTS idx_activity_league ON activity_events(league_id, created_at);
CREATE INDEX IF NOT EXISTS idx_email_tokens_hash ON email_tokens(token_hash);
