"""Frozen row types shared across the toolkit.

Two write-side shapes (ExtractedFact from the code extractors, ProposedFact
from every other extractor/derivation) and three read-side shapes the query
layer returns. All frozen: facts are values, not mutable state.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedFact:
    """A deterministic code fact straight from AST/regex extraction, carrying
    its file:line provenance. Confidence is implied (1.0, the deterministic
    floor) and assigned at write time."""

    subject_kind: str
    subject_name: str
    subject_repo: str | None
    subject_path: str | None
    predicate: str
    object_kind: str | None
    object_name: str | None
    object_lit: str | None
    extractor: str
    source_kind: str
    source_ref: str


@dataclass(frozen=True)
class ProposedFact:
    """A candidate fact for consolidation (lessons, tests, co-change, derived).

    `origin` names where it came from ("doc:<path>", "code:<path>",
    "git:<repo>", "derived") and becomes the provenance source_ref. `excerpt`
    carries the evidence (a quote, an import line, a proof path).
    """

    subject_kind: str
    subject_name: str
    subject_repo: str | None
    predicate: str
    object_kind: str | None
    object_name: str | None
    object_lit: str | None
    confidence: float
    extractor: str
    origin: str
    excerpt: str | None = None


@dataclass(frozen=True)
class FactRow:
    """One triple + its provenance, as returned by FactQuery.query_facts.

    object_name is set when the object is an entity; object_lit when it is a
    literal (XOR, matching the schema CHECK). source_refs lists the provenance
    source_ref strings, oldest first.
    """

    subject_kind: str
    subject_name: str
    predicate: str
    object_kind: str | None
    object_name: str | None
    object_lit: str | None
    confidence: float
    extractor: str
    valid_from: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class EntityRow:
    """An entity's header row, for explain_entity dossiers."""

    entity_id: int
    kind: str
    name: str
    repo: str | None
    path: str | None


@dataclass(frozen=True)
class EntityDossier:
    """The explain_entity neighbourhood: the entity + its outgoing facts
    (entity is subject) and incoming facts (entity is object). entity is None
    when the name matched no entity."""

    entity: EntityRow | None
    outgoing: list
    incoming: list
