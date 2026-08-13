"""Deterministic, structure-aware fact extractor for lesson/notes markdown.

Curated engineering notes are ALREADY distilled findings — a bolded rule, a
"Rule:" line, `file:line` citations — so a parser produces higher-quality
facts than an LLM distiller, for free. No model call anywhere.

Predicates emitted (all with subject = the cleaned lesson TOPIC, so a
section's facts share one graph node):

    lesson   topic -> the rule/finding      (object_lit)
    because  topic -> a rationale           (object_lit; the rule's "Why")
    cites    topic -> a file:line / path    (object_lit)

Confidence is 0.9: structure-derived and high, but below the 1.0 floor
reserved for deterministic CODE facts, because the object text is natural
language.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from groundgraph.types import ProposedFact

logger = logging.getLogger(__name__)

LESSON_PREDICATES = {"lesson", "because", "cites"}

_CONF = 0.9
_EXTRACTOR = "det:lesson@1"
_SUBJECT_KIND = "lesson-topic"
_MAX_LIT = 240
_MAX_CITES = 5
_MAX_BECAUSE = 3
_MIN_BODY = 40
_MIN_RULE = 40
_SKIP_TITLES = {"overview", "contents", "table of contents"}

# --- title -> topic cleaning ------------------------------------------------
_LESSON_PREFIX = re.compile(r"^(?:lessons?|landmine)\s*[—–-]+\s*", re.IGNORECASE)
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[—–:-]+\s*")
_NUM_PREFIX = re.compile(r"^\d+(?:\.\d+)*\.?\s+")
_TRAIL_PAREN = re.compile(r"\s*\([^()]*\)\s*$")

# --- rule / rationale / citation detection ----------------------------------
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RULE_MARKER = re.compile(
    r"(?:^|(?<=[.\n]))\s*(?:Rule|The discipline|Discipline|Lesson)\s*:\s*(.+)",
    re.IGNORECASE,
)
_BECAUSE_MARKER = re.compile(
    r"(?:^|(?<=[.\n;]))\s*(?:Why|Because|Cause|Root cause|Rationale)\b[:,]?\s*(.+)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"*])")
_SENTENCE_END = re.compile(r"^(.*?[.!?])(?:\s|$)")
_CITE_LINE = re.compile(r"[\w./-]+\.[A-Za-z]{1,5}:\d+")
_BACKTICK = re.compile(r"`([^`]+)`")
_PATHISH = re.compile(r"[\w./-]+")
_HAS_EXT = re.compile(r"\.[A-Za-z]{1,6}(?::\d+)?$")

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")


def _clean_topic(title: str) -> str:
    """Strip a leading 'Lesson —'/'Landmine —', a bared date, a section number,
    and a single trailing parenthetical (a date/issue tag)."""
    topic, prev = title.strip(), None
    while topic and topic != prev:
        prev = topic
        topic = _LESSON_PREFIX.sub("", topic)
        topic = _DATE_PREFIX.sub("", topic)
        topic = _NUM_PREFIX.sub("", topic)
    return _TRAIL_PAREN.sub("", topic).strip()


def _flatten(body: str) -> str:
    """Join wrapped body lines into single-line prose (drop heading/table lines
    and list bullets) so sentence- and marker-regexes see whole sentences."""
    kept: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "|", "```")):
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", line)  # list marker
        kept.append(line)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _sentence_end(text: str) -> str:
    """`text` truncated at its first sentence-ending '. ' (a period inside
    `foo.py:9` is skipped: it is not followed by whitespace/end)."""
    m = _SENTENCE_END.search(text)
    return (m.group(1) if m else text).strip()


def _trim(text: str, limit: int = _MAX_LIT) -> str:
    """Drop bold markers and clamp to ~limit chars on a word boundary."""
    text = text.replace("**", "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"


def _extract_rule(prose: str, sentences: list[str]) -> tuple[str, str] | tuple[None, None]:
    """The section's RULE as (object_lit, excerpt): a bolded sentence, else a
    'Rule:'/'The discipline:'/'Lesson:' marker, else the first real sentence."""
    for m in _BOLD.finditer(prose):
        span = m.group(1).strip().rstrip(":")
        if len(span) >= _MIN_RULE and " " in span:
            return span, span
    marker = _RULE_MARKER.search(prose)
    if marker:
        rule = _sentence_end(marker.group(1))
        if len(rule) >= 8:  # a bare "Rule:" with no clause is noise
            return rule, marker.group(0).strip()
    for sentence in sentences:
        if len(sentence) >= _MIN_RULE:
            return sentence, sentence
    return None, None


def _extract_because(prose: str) -> list[str]:
    """Rationales from 'Why:'/'Because'/'Cause:'/'Root cause:' clauses."""
    out: list[str] = []
    for m in _BECAUSE_MARKER.finditer(prose):
        rationale = _sentence_end(m.group(1))
        if len(rationale) >= 12 and rationale not in out:
            out.append(rationale)
        if len(out) >= _MAX_BECAUSE:
            break
    return out


def _is_path_ref(token: str) -> bool:
    if not _PATHISH.fullmatch(token):
        return False
    return "/" in token or _HAS_EXT.search(token) is not None


def _extract_cites(body: str) -> list[str]:
    """Distinct 'file.ext:NN' refs + backticked path refs, capped."""
    refs: list[str] = []
    seen: set[str] = set()

    def add(ref: str) -> None:
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for m in _CITE_LINE.finditer(body):
        add(m.group(0))
    for m in _BACKTICK.finditer(body):
        token = m.group(1).strip()
        if _is_path_ref(token):
            add(token)
    return refs[:_MAX_CITES]


def _fact(
    topic: str, repo: str | None, origin: str,
    predicate: str, object_lit: str, excerpt: str,
) -> ProposedFact:
    return ProposedFact(
        subject_kind=_SUBJECT_KIND, subject_name=topic, subject_repo=repo,
        predicate=predicate, object_kind=None, object_name=None,
        object_lit=_trim(object_lit), confidence=_CONF, extractor=_EXTRACTOR,
        origin=origin, excerpt=_trim(excerpt),
    )


def extract_lesson_facts(
    title: str, text: str, source: str, repo: str | None = None,
) -> list[ProposedFact]:
    """Parse ONE lesson/notes section into ProposedFacts. Pure — no I/O, no
    model. `title` and `text` are a section (heading + body); `source` is the
    doc path (becomes the provenance origin). Boilerplate sections (body < 40
    chars, or an Overview/Contents heading) return []."""
    topic = _clean_topic(title)
    body = text.strip()
    if not topic or len(body) < _MIN_BODY or topic.lower() in _SKIP_TITLES:
        return []

    origin = f"doc:{source}"
    prose = _flatten(body)
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(prose) if s.strip()]

    facts: list[ProposedFact] = []
    rule, rule_excerpt = _extract_rule(prose, sentences)
    if rule is not None:
        facts.append(_fact(topic, repo, origin, "lesson", rule, rule_excerpt))
    for rationale in _extract_because(prose):
        facts.append(_fact(topic, repo, origin, "because", rationale, rationale))
    for ref in _extract_cites(body):
        facts.append(_fact(topic, repo, origin, "cites", ref, ref))
    return facts


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split a markdown document into (heading, body) sections at #/##/###
    headings. Text before the first heading is attributed to the document's
    first heading-less pseudo-section only when non-trivial."""
    sections: list[tuple[str, str]] = []
    title: str | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        m = _HEADING.match(line)
        if m:
            if title is not None:
                sections.append((title, "\n".join(buf)))
            title = m.group(2).strip()
            buf = []
        elif title is not None:
            buf.append(line)
    if title is not None:
        sections.append((title, "\n".join(buf)))
    return sections


def extract_doc_facts(doc_path: str | Path, repo: str | None = None) -> list[ProposedFact]:
    """Read one markdown file and extract lesson facts from every section."""
    p = Path(doc_path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("cannot read %s: %s", p, e)
        return []
    facts: list[ProposedFact] = []
    for title, body in split_sections(text):
        facts.extend(extract_lesson_facts(title, body, str(p), repo))
    return facts
