-- groundgraph store: entities + facts + provenance. One SQLite file.

CREATE TABLE entities (
  entity_id    INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,   -- file | symbol | module | repo | lesson-topic | ...
  name         TEXT NOT NULL,   -- canonical / qualified name
  repo         TEXT,            -- repo label (NULL for cross-repo entities e.g. modules)
  path         TEXT,            -- file path for file/symbol-bound entities
  meta         TEXT,            -- optional JSON
  first_seen   TEXT NOT NULL,   -- ISO-8601
  last_seen    TEXT NOT NULL,
  UNIQUE(kind, name, repo)
);

CREATE TABLE facts (            -- the triples: (subject) --predicate--> (object)
  fact_id       INTEGER PRIMARY KEY,
  subject_id    INTEGER NOT NULL REFERENCES entities(entity_id),
  predicate     TEXT NOT NULL,  -- calls | imports | defined-in | depends-on | tests | ...
  object_id     INTEGER REFERENCES entities(entity_id),  -- NULL when object is a literal
  object_lit    TEXT,           -- literal object; XOR with object_id
  confidence    REAL NOT NULL,  -- 0.0-1.0; deterministic facts = 1.0
  extractor     TEXT NOT NULL,  -- 'det:ast@1' | 'der:inverse@1' | ...
  created_at    TEXT NOT NULL,
  valid_from    TEXT NOT NULL,
  valid_to      TEXT,           -- NULL = currently true; set on supersede (SOFT delete, never DROP)
  superseded_by INTEGER REFERENCES facts(fact_id),
  UNIQUE(subject_id, predicate, object_id, object_lit, valid_from),
  -- enforce the object XOR at insert time: exactly one of object_id/object_lit set.
  CHECK ((object_id IS NULL) != (object_lit IS NULL))
);

-- Query-serving indices. The UNIQUE above indexes by subject first, which does
-- not serve "what calls X" (predicate+object). Trailing valid_to lets the
-- current-facts filter prune archived rows through the index.
CREATE INDEX idx_facts_pred_obj  ON facts (predicate, object_id, valid_to);
CREATE INDEX idx_facts_subj_pred ON facts (subject_id, predicate, valid_to);
CREATE INDEX idx_facts_valid_to  ON facts (valid_to);

CREATE TABLE provenance (       -- one-to-many evidence per fact
  prov_id      INTEGER PRIMARY KEY,
  fact_id      INTEGER NOT NULL REFERENCES facts(fact_id),
  source_kind  TEXT NOT NULL,   -- code_line | commit | doc | derived
  source_ref   TEXT NOT NULL,   -- 'src/app.py:142' | 'git:<repo>' | 'docs/x.md' | 'derived'
  excerpt      TEXT,            -- optional evidence snippet
  created_at   TEXT NOT NULL
);
