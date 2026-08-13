from __future__ import annotations

import sqlite3

from groundgraph.health import effective_confidence, graph_health


def test_effective_confidence_decay_and_tiers() -> None:
    now = "2026-08-10T00:00:00+00:00"
    old = "2026-06-11T00:00:00+00:00"   # 60 days earlier = one half-life
    # a risky inferred fact halves over a half-life
    assert abs(effective_confidence(0.8, old, now, "llm:distiller@1") - 0.4) < 0.01
    # deterministic AND derived facts never decay (both re-derivable)
    assert effective_confidence(0.8, old, now, "det:ast@1") == 0.8
    assert effective_confidence(0.98, old, now, "der:inverse@1") == 0.98
    # missing timestamp -> unchanged; fresh fact ~ unchanged
    assert effective_confidence(0.7, None, now, "llm:x") == 0.7
    assert abs(effective_confidence(0.9, now, now, "llm:x") - 0.9) < 0.001


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE entities(entity_id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE facts(fact_id INTEGER PRIMARY KEY, subject_id INT, predicate TEXT,
      object_id INT, object_lit TEXT, confidence REAL, extractor TEXT,
      created_at TEXT, valid_to TEXT);
    INSERT INTO entities VALUES (1,'funcA'),(2,'funcB'),(3,'');
    """)
    return c


def test_graph_health_flags_rot() -> None:
    now = "2026-08-10T00:00:00+00:00"
    old = "2026-01-01T00:00:00+00:00"   # >200d -> deeply decayed
    c = _db()
    c.executescript(f"""
    INSERT INTO facts VALUES (1,1,'calls',2,NULL,1.0,'det:ast@1','{now}',NULL);
    -- duplicate live inferred facts -> 1 duplicate
    INSERT INTO facts VALUES (2,1,'lesson',NULL,'x',0.6,'llm:q@1','{now}',NULL);
    INSERT INTO facts VALUES (3,1,'lesson',NULL,'x',0.6,'llm:q@1','{now}',NULL);
    -- contradiction: same subj+pred (single-valued), two distinct objects
    INSERT INTO facts VALUES (4,2,'decided',NULL,'use A',0.7,'llm:q@1','{now}',NULL);
    INSERT INTO facts VALUES (5,2,'decided',NULL,'use B',0.7,'llm:q@1','{now}',NULL);
    -- decay-dormant: old inferred fact
    INSERT INTO facts VALUES (6,1,'prefers',NULL,'y',0.7,'llm:q@1','{old}',NULL);
    -- orphan: empty-named subject
    INSERT INTO facts VALUES (7,3,'lesson',NULL,'z',0.6,'llm:q@1','{now}',NULL);
    -- a soft-superseded fact (excluded from live)
    INSERT INTO facts VALUES (8,1,'bug',NULL,'old',0.7,'llm:q@1','{now}','{now}');
    -- MULTI-VALUED: a 2nd distinct call is normal and must NOT flag
    INSERT INTO facts VALUES (9,1,'calls',3,NULL,1.0,'det:ast@1','{now}',NULL);
    """)
    h = graph_health(c, now=now)
    assert h["live_facts"] == 8 and h["superseded"] == 1
    assert h["deterministic"] == 2 and h["inferred"] == 6 and h["derived"] == 0
    assert h["duplicates"] == 1
    assert h["contradiction_candidates"] == 1   # only the single-valued 'decided' pair
    assert h["orphans"] == 1
    assert h["decay_dormant_inferred"] == 1
    assert any("contradiction" in w for w in h["warnings"])
    assert any("orphan" in w for w in h["warnings"])
