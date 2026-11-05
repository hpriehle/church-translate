-- Anonymous download totals: one row per day and version. Nothing about who downloaded.
CREATE TABLE IF NOT EXISTS downloads (
  day     TEXT    NOT NULL,   -- UTC, YYYY-MM-DD
  version TEXT    NOT NULL,
  count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, version)
);
