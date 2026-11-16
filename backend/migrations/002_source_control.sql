-- New databases bootstrap metadata; apply reviewed migrations for later upgrades.
CREATE INDEX endmember_library_idx ON endmembers (library_id);
CREATE INDEX note_run_status_idx ON replication_notes (run_id,status);

-- 002: measurement source lineage, revocation and replacement control.
-- Run `python -m scripts.backfill_sources` after applying, to give legacy
-- samples their measured source and legacy studies their frozen links.
CREATE TABLE IF NOT EXISTS spectrum_sources (
  id INT PRIMARY KEY AUTO_INCREMENT,
  code VARCHAR(160) NOT NULL UNIQUE,
  library_id INT NOT NULL,
  sample_id INT NOT NULL,
  kind VARCHAR(24) NOT NULL DEFAULT 'measured',
  status VARCHAR(24) NOT NULL DEFAULT 'active',
  observed JSON NOT NULL,
  reason TEXT NOT NULL,
  supersedes_id INT NULL,
  owner VARCHAR(100) NOT NULL DEFAULT '',
  created_at VARCHAR(64) NOT NULL DEFAULT '',
  revoked_at VARCHAR(64) NOT NULL DEFAULT '',
  CONSTRAINT fk_source_library FOREIGN KEY (library_id) REFERENCES spectral_libraries(id),
  CONSTRAINT fk_source_sample FOREIGN KEY (sample_id) REFERENCES mixture_samples(id),
  CONSTRAINT fk_source_supersedes FOREIGN KEY (supersedes_id) REFERENCES spectrum_sources(id),
  INDEX source_sample_status_idx (sample_id, status),
  INDEX source_code_idx (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS run_source_links (
  id INT PRIMARY KEY AUTO_INCREMENT,
  run_id INT NOT NULL,
  sample_id INT NOT NULL,
  source_id INT NULL,
  source_code VARCHAR(160) NOT NULL DEFAULT '',
  relation VARCHAR(24) NOT NULL DEFAULT 'snapshot',
  created_at VARCHAR(64) NOT NULL DEFAULT '',
  CONSTRAINT fk_link_run FOREIGN KEY (run_id) REFERENCES unmix_runs(id),
  CONSTRAINT fk_link_sample FOREIGN KEY (sample_id) REFERENCES mixture_samples(id),
  CONSTRAINT fk_link_source FOREIGN KEY (source_id) REFERENCES spectrum_sources(id),
  CONSTRAINT run_sample_source_unique UNIQUE (run_id, sample_id),
  INDEX run_source_run_idx (run_id),
  INDEX run_source_source_idx (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS run_controls (
  id INT PRIMARY KEY AUTO_INCREMENT,
  run_id INT NOT NULL,
  publish_state VARCHAR(24) NOT NULL DEFAULT 'active',
  blocked_reason TEXT NOT NULL,
  replacement_of_run_id INT NULL,
  replacement_run_id INT NULL,
  replacement_state VARCHAR(24) NOT NULL DEFAULT 'none',
  replacement_error TEXT NOT NULL,
  replacement_sample_ids JSON NOT NULL,
  created_at VARCHAR(64) NOT NULL DEFAULT '',
  updated_at VARCHAR(64) NOT NULL DEFAULT '',
  CONSTRAINT fk_control_run FOREIGN KEY (run_id) REFERENCES unmix_runs(id),
  CONSTRAINT fk_control_original FOREIGN KEY (replacement_of_run_id) REFERENCES unmix_runs(id),
  CONSTRAINT fk_control_replacement FOREIGN KEY (replacement_run_id) REFERENCES unmix_runs(id),
  CONSTRAINT run_control_run_unique UNIQUE (run_id),
  CONSTRAINT run_control_one_replacement UNIQUE (replacement_of_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- New columns on existing tables (MySQL 8 has no ADD COLUMN IF NOT EXISTS;
-- skip duplicates on re-apply).
ALTER TABLE unmix_runs ADD COLUMN updated_at VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE unmix_runs ADD COLUMN job_token VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE replication_notes ADD COLUMN approved_hash VARCHAR(64) NOT NULL DEFAULT '';
