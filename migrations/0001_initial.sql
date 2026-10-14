PRAGMA foreign_keys = ON;
CREATE TABLE users (
 id TEXT PRIMARY KEY, github_id TEXT NOT NULL UNIQUE, github_login TEXT NOT NULL,
 name TEXT NOT NULL, email TEXT, avatar_url TEXT,
 role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')),
 active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE INDEX sessions_expiry ON sessions(expires_at);
CREATE TABLE oauth_states (state_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
CREATE TABLE invitations (
 code_hash TEXT PRIMARY KEY, created_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_by TEXT REFERENCES users(id)
);
CREATE TABLE templates (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), recipient TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','sending','accepted','simulated','failed','uncertain','cancelled','expired')),
 target_at TEXT NOT NULL, expires_at TEXT, created_at TEXT NOT NULL, claimed_at TEXT,
 finished_at TEXT, provider_id TEXT UNIQUE, reason TEXT,
 unsubscribe_token TEXT NOT NULL UNIQUE
);
CREATE INDEX tasks_due ON tasks(status,target_at);
CREATE INDEX tasks_owner ON tasks(user_id,created_at DESC);
CREATE TABLE suppressions (recipient TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL DEFAULT 0);
INSERT INTO settings(id,paused) VALUES(1,0);
