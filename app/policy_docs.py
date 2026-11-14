"""Policy documents: Markdown rendering/parsing and chunk construction.

Markdown files under ``data/property_docs/`` are the source of truth for
retrieval. Each file carries document metadata in front matter (one
``key: <JSON value>`` per line, which is also valid YAML) and one ``##``
section per policy section, tagged with its stable section ID.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT

POLICY_DOCS_DIR = PROJECT_ROOT / "data" / "property_docs"

FRONT_MATTER_FIELDS = (
    "document_id",
    "property_id",
    "title",
    "version",
    "effective_from",
    "status",
    "audience",
    "language",
)

_SECTION_RE = re.compile(
    r"^## (?P<heading>.+?)\n"
    r"<!-- section_id: (?P<section_id>\S+) -->\n\n"
    r"(?P<text>.*?)\s*(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class PolicySection:
    section_id: str
    heading: str
    text: str


@dataclass(frozen=True)
class PolicyDocument:
    document_id: str
    property_id: str
    title: str
    version: str
    effective_from: str
    status: str
    audience: tuple[str, ...]
    language: str
    sections: tuple[PolicySection, ...]

    @classmethod
    def from_dict(cls, d: dict) -> PolicyDocument:
        doc = cls(
            **{k: d[k] for k in FRONT_MATTER_FIELDS if k != "audience"},
            audience=tuple(d["audience"]),
            sections=tuple(
                PolicySection(s["section_id"], s["heading"], s["text"]) for s in d["sections"]
            ),
        )
        doc.validate()
        return doc

    def validate(self) -> None:
        if not self.sections:
            raise ValueError(f"{self.document_id}: no sections")
        seen: set[str] = set()
        for s in self.sections:
            if not s.section_id.startswith(f"{self.document_id}-"):
                raise ValueError(f"{s.section_id} does not belong to {self.document_id}")
            if s.section_id in seen:
                raise ValueError(f"duplicate section_id {s.section_id}")
            if "\n## " in f"\n{s.text}" or not s.text.strip():
                raise ValueError(f"{s.section_id}: empty text or embedded '## ' heading")
            seen.add(s.section_id)

    @property
    def filename(self) -> str:
        return f"{self.document_id}.md"


def render_markdown(doc: PolicyDocument) -> str:
    meta = {k: getattr(doc, k) for k in FRONT_MATTER_FIELDS}
    meta["audience"] = list(doc.audience)
    lines = ["---", *(f"{k}: {json.dumps(v)}" for k, v in meta.items()), "---", ""]
    lines += [f"# {doc.title}", ""]
    for s in doc.sections:
        lines += [f"## {s.heading}", f"<!-- section_id: {s.section_id} -->", "", s.text, ""]
    return "\n".join(lines)


def parse_markdown(text: str) -> PolicyDocument:
    if not text.startswith("---\n"):
        raise ValueError("missing front matter")
    front, _, body = text[4:].partition("\n---\n")
    meta = {}
    for line in front.splitlines():
        key, _, value = line.partition(": ")
        meta[key] = json.loads(value)
    missing = set(FRONT_MATTER_FIELDS) - meta.keys()
    if missing:
        raise ValueError(f"front matter missing {sorted(missing)}")
    sections = [m.groupdict() for m in _SECTION_RE.finditer(body)]
    return PolicyDocument.from_dict({**meta, "sections": sections})


def load_policy_documents(directory: Path = POLICY_DOCS_DIR) -> list[PolicyDocument]:
    paths = sorted(directory.glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"no policy Markdown files in {directory}")
    docs = []
    for p in paths:
        doc = parse_markdown(p.read_text())
        if p.name != doc.filename:
            raise ValueError(f"{p.name}: filename does not match document_id {doc.document_id}")
        docs.append(doc)
    return docs


# --- Chunks -----------------------------------------------------------------


@dataclass(frozen=True)
class PolicyChunk:
    """One embeddable unit: a single policy section plus its document metadata."""

    chunk_id: str  # == section_id; stable Firestore document ID
    section_id: str
    document_id: str
    property_id: str
    title: str
    heading: str
    text: str
    version: str
    effective_from: str
    status: str
    audience: tuple[str, ...]
    language: str
    source_uri: str

    @property
    def embedding_text(self) -> str:
        return f"{self.title}\n{self.heading}\n\n{self.text}"

    def content_hash(self, embedding_model: str, embedding_dimensions: int) -> str:
        """Changes whenever the embedding (or the stored chunk) would change."""
        payload = json.dumps(
            {
                "text": self.embedding_text,
                "meta": [self.document_id, self.property_id, self.version, self.status],
                "audience": list(self.audience),
                "source_uri": self.source_uri,
                "model": embedding_model,
                "dims": embedding_dimensions,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def build_chunks(docs: list[PolicyDocument], source_uri_for: dict[str, str]) -> list[PolicyChunk]:
    """One chunk per section. Only published documents are indexed."""
    chunks = []
    for doc in docs:
        if doc.status != "published":
            continue
        for s in doc.sections:
            chunks.append(
                PolicyChunk(
                    chunk_id=s.section_id,
                    section_id=s.section_id,
                    document_id=doc.document_id,
                    property_id=doc.property_id,
                    title=doc.title,
                    heading=s.heading,
                    text=s.text,
                    version=doc.version,
                    effective_from=doc.effective_from,
                    status=doc.status,
                    audience=doc.audience,
                    language=doc.language,
                    source_uri=source_uri_for[doc.document_id],
                )
            )
    return chunks
