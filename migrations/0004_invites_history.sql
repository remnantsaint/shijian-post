ALTER TABLE invitations ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1 CHECK(max_uses BETWEEN 1 AND 1000);
ALTER TABLE invitations ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count BETWEEN 0 AND max_uses);
ALTER TABLE invitations ADD COLUMN code TEXT;
ALTER TABLE invitations ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0 CHECK(is_public IN (0,1));
ALTER TABLE invitations ADD COLUMN revoked_at TEXT;
UPDATE invitations SET used_count=1 WHERE used_by IS NOT NULL;
CREATE TABLE invitation_redemptions (
 code_hash TEXT NOT NULL REFERENCES invitations(code_hash),
 user_id TEXT NOT NULL UNIQUE REFERENCES users(id),
 created_at TEXT NOT NULL,
 PRIMARY KEY(code_hash,user_id)
);
INSERT OR IGNORE INTO invitation_redemptions(code_hash,user_id,created_at) SELECT code_hash,used_by,created_at FROM invitations WHERE used_by IS NOT NULL;
CREATE TRIGGER redeem_invitation AFTER INSERT ON invitation_redemptions
BEGIN
 UPDATE invitations SET used_count=used_count+1,used_by=COALESCE(used_by,NEW.user_id) WHERE code_hash=NEW.code_hash;
 UPDATE users SET active=1 WHERE id=NEW.user_id;
END;
ALTER TABLE tasks ADD COLUMN deleted_at TEXT;
