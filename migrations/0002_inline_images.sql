ALTER TABLE tasks ADD COLUMN images_metadata TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN images_cleaned_at TEXT;
CREATE TABLE task_images (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  image_id TEXT NOT NULL,
  content BLOB NOT NULL,
  PRIMARY KEY (task_id, image_id)
);
CREATE INDEX tasks_image_cleanup ON tasks(status) WHERE images_cleaned_at IS NULL AND images_metadata != '[]';
