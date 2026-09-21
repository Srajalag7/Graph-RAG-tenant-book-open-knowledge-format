"""OKF parsing, source-version mapping, and permission/date-aware bundle reads."""

import hashlib
import json
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from markdown_it import MarkdownIt

POLICY_ID = (
    "claire_policy"  # The behavioral-policy identity defined by the freshness contract.
)
ROLES = {"renter", "landlord", "staff"}
SOURCE_METADATA = (
    "doc_id",
    "title",
    "issuer",
    "layer",
    "source_url",
    "notes",
    "visibility",
    "version",
    "effective_from",
    "effective_to",
    "supersedes",
    "superseded_by",
)
MARKDOWN = MarkdownIt("commonmark")


class BundleError(ValueError):
    pass


class TextLoader(yaml.SafeLoader):
    pass


TextLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in values
        if tag != "tag:yaml.org,2002:timestamp"
    ]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def norm(text):
    text = unicodedata.normalize("NFKC", text or "").replace("\u00ad", "")
    text = text.translate(str.maketrans("‘’“”–—", "''\"\"--"))
    return re.sub(r"\s+", " ", text).strip().casefold()


def fingerprint(corpus):
    corpus = Path(corpus)
    paths = [
        corpus / "manifest.json",
        corpus / "passages.jsonl",
        *sorted((corpus / "docs").rglob("*.md")),
    ]
    digest = hashlib.sha256()
    for path in paths:
        if not path.resolve().is_relative_to(corpus.resolve()):
            raise BundleError("Source file escapes corpus directory")
        digest.update(path.relative_to(corpus).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def parse(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        raise BundleError("Unterminated YAML frontmatter")
    fm = yaml.load(text[4:end], Loader=TextLoader)
    if not isinstance(fm, dict):
        raise BundleError("Frontmatter must be a mapping")
    return fm, text[end + 5 :].removeprefix("\n")


def serialize(fm, body):
    return (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + body
    )


def resolve_link(origin, link):
    url = urlsplit(link)
    if url.scheme or url.netloc:
        return None
    raw = unquote(url.path)
    if not raw:
        raw = origin
    elif not raw.startswith("/"):
        raw = posixpath.dirname(origin) + "/" + raw
    # Root-relative OKF paths are NOT filesystem absolute paths.
    depth = 0
    for part in raw.split("/"):
        if part == "..":
            depth -= 1
        elif part not in ("", "."):
            depth += 1
        if depth < 0:
            raise BundleError("Link escapes the bundle")
    result = "/" + posixpath.normpath(raw).lstrip("/")
    if "\\" in result or "\x00" in result:
        raise BundleError("Invalid concept path")
    if result.endswith("/") or not Path(result).suffix:
        result = result.rstrip("/") + "/index.md"
    return result if result.endswith(".md") else None


def markdown_links(body, origin):
    links = []
    for block in MARKDOWN.parse(body):
        for token in block.children or []:
            if token.type == "link_open":
                path = resolve_link(origin, token.attrGet("href") or "")
                if path and path not in links:
                    links.append(path)
    return links


def sections(text):
    matches = list(re.finditer(r"^## (.+)$", text, re.M))
    return [
        (
            m.group(1).strip(),
            m.end(),
            matches[i + 1].start() if i + 1 < len(matches) else len(text),
        )
        for i, m in enumerate(matches)
    ]


def source_span(text, quote, start=0, end=None):
    """Find exact source offsets, allowing the same normalization as the grader."""
    needle = norm(quote)
    if not needle:
        return None
    end = len(text) if end is None else end
    at = text.find(quote, start, end)
    if at >= 0:
        return at, at + len(quote)
    normalized, offsets = [], []
    for i in range(start, end):
        for char in (
            unicodedata.normalize("NFKC", text[i])
            .translate(str.maketrans("‘’“”–—", "''\"\"--"))
            .casefold()
        ):
            if char == "\u00ad":
                continue
            if char.isspace():
                char = " "
                if normalized and normalized[-1] == " ":
                    continue
            normalized.append(char)
            offsets.append(i)
    at = "".join(normalized).find(needle)
    if at < 0:
        return None
    return offsets[at], offsets[at + len(needle) - 1] + 1


def in_force(meta, as_of):
    return (not meta.get("effective_from") or meta["effective_from"] <= as_of) and (
        not meta.get("effective_to") or as_of <= meta["effective_to"]
    )


def load_sources(corpus):
    """Read a complete supplied corpus, never modify it or recover from an old bundle."""
    corpus = Path(corpus)
    manifest = json.loads((corpus / "manifest.json").read_text())
    documents = manifest["documents"]
    ids = [d["doc_id"] for d in documents]
    if len(ids) != len(set(ids)):
        raise BundleError("Duplicate manifest document IDs")
    passages = [
        json.loads(line)
        for line in (corpus / "passages.jsonl").read_text().splitlines()
        if line.strip()
    ]
    sources = {}
    for doc in documents:
        did = doc["doc_id"]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", did):
            raise BundleError("Invalid document ID")
        if not doc.get("visibility") or not set(doc["visibility"]) <= ROLES:
            raise BundleError(f"Missing/invalid visibility: {did}")
        files = [
            corpus / "docs" / f"{did}.md",
            *sorted((corpus / "docs").glob(f"{did}@v*.md")),
        ]
        for file in files:
            if not file.is_file() or not file.resolve().is_relative_to(
                (corpus / "docs").resolve()
            ):
                raise BundleError(f"Missing or unsafe source: {file.name}")
            text = file.read_text()
            historical = file.stem != did
            version = doc.get("version", 1)
            if historical:
                match = re.search(r"@v(\d+)$", file.stem)
                if not match:
                    raise BundleError(f"Invalid archived version filename: {file.name}")
                version = int(match[1])
            if type(version) is not int or version < 1:
                raise BundleError(f"Invalid source version: {file.name}")
            meta = dict(doc, version=version, current=not historical)
            if historical:
                # Archived headers can carry their original version/date values.
                header = re.search(r"<!--\s*doc_id:.*?-->", text, re.S)
                archived = {}
                if header:
                    for item in header[0][4:-3].split("|"):
                        key, _, value = item.strip().partition(":")
                        archived[key] = value.strip()
                meta["effective_from"] = archived.get("effective_from")
                meta["effective_to"] = archived.get("effective_to")
                for key in ("effective_from", "effective_to"):
                    if meta[key] in ("None", "null", ""):
                        meta[key] = None
            path = f"/docs/{did}/v{version}.md"
            if path in sources:
                raise BundleError(f"Duplicate source version: {path}")
            sources[path] = {
                "meta": meta,
                "text": text,
                "source_path": file.relative_to(corpus).as_posix(),
                "sections": sections(text),
                "evidence": [],
            }
    for p in passages:
        candidates = []
        for path, source in sources.items():
            meta = source["meta"]
            if meta["doc_id"] != p["doc_id"]:
                continue
            for heading, begin, end in source["sections"]:
                if norm(heading) != norm(p["section"]):
                    continue
                span = source_span(source["text"], p["text"], begin, end)
                if span:
                    candidates.append((path, heading, span))
        if len(candidates) > 1:
            # Identical text in multiple revisions is assigned by supplied intervals.
            candidates = [
                c
                for c in candidates
                if (
                    (
                        not sources[c[0]]["meta"].get("effective_from")
                        or p.get("effective_from", "")
                        >= sources[c[0]]["meta"]["effective_from"]
                    )
                    and (
                        not sources[c[0]]["meta"].get("effective_to")
                        or (
                            p.get("effective_to")
                            and p["effective_to"]
                            <= sources[c[0]]["meta"]["effective_to"]
                        )
                    )
                )
            ]
        if len(candidates) != 1:
            raise BundleError(
                f"Passage has no unique matching source version: {p['passage_id']}"
            )
        path, heading, (begin, end) = candidates[0]
        sources[path]["evidence"].append(
            {
                "passage_id": p["passage_id"],
                "section": heading,
                "start": begin,
                "end": end,
                "effective_from": p.get("effective_from"),
                "effective_to": p.get("effective_to"),
            }
        )
    for path, source in sources.items():
        meta, evidence = source["meta"], source["evidence"]
        if not evidence:
            raise BundleError(f"Source version has no supplied passages: {path}")
        if not meta["current"]:
            starts = [p["effective_from"] for p in evidence if p.get("effective_from")]
            ends = [p["effective_to"] for p in evidence if p.get("effective_to")]
            if not starts or len(ends) != len(evidence):
                raise BundleError(
                    f"Historical version needs closed passage intervals: {path}"
                )
            meta["effective_from"], meta["effective_to"] = min(starts), max(ends)
        for p in evidence:
            if (
                p.get("effective_from")
                and p.get("effective_to")
                and p["effective_from"] > p["effective_to"]
            ):
                raise BundleError(f"Reversed passage dates: {p['passage_id']}")
            if meta["doc_id"] != POLICY_ID and (
                p.get("effective_from")
                and not in_force(meta, p["effective_from"])
                or p.get("effective_to")
                and not in_force(meta, p["effective_to"])
            ):
                raise BundleError(
                    f"Passage dates fall outside source version: {p['passage_id']}"
                )
    for did in ids:
        versions = [s["meta"] for s in sources.values() if s["meta"]["doc_id"] == did]
        for i, first in enumerate(versions):
            for second in versions[i + 1 :]:
                if max(
                    first.get("effective_from") or "0000",
                    second.get("effective_from") or "0000",
                ) <= min(
                    first.get("effective_to") or "9999",
                    second.get("effective_to") or "9999",
                ):
                    raise BundleError(f"Overlapping source versions: {did}")
    return sources


@dataclass
class Concept:
    path: str
    meta: dict
    body: str
    links: list

    @property
    def source(self):
        return self.body[: self.meta.get("source_length", 0)]


def scan_bundle(root):
    root = Path(root).resolve()
    concepts = {}
    for file in sorted(root.rglob("*.md")):
        if file == root / "log.md":
            continue
        if not file.resolve().is_relative_to(root):
            raise BundleError("Concept escapes bundle directory")
        path = "/" + file.relative_to(root).as_posix()
        fm, body = parse(file.read_text())
        if fm.get("status") not in (None, "draft", "stable", "deprecated"):
            raise BundleError(f"Invalid OKF lifecycle status: {path}")
        if path == "/index.md" and fm.get("okf_version") != "0.2":
            raise BundleError("The root index must declare OKF version 0.2")
        if file.name != "index.md" and not fm.get("type"):
            raise BundleError(f"Missing concept type: {path}")
        concepts[path] = Concept(path, fm, body, markdown_links(body, path))
    if "/index.md" not in concepts or "/docs/index.md" not in concepts:
        raise BundleError("Missing required application indexes; run reindex")
    return concepts


def derive_graph(root, source_fingerprint):
    concepts = scan_bundle(root)
    nodes, edges = {}, []
    for path, concept in concepts.items():
        nodes[path] = {
            "sha256": hashlib.sha256(
                (Path(root) / path.lstrip("/")).read_bytes()
            ).hexdigest(),
            "doc_id": concept.meta.get("doc_id"),
            "source_path": concept.meta.get("source_path"),
        }
        for target in concept.links:
            relations = [
                r for r in concept.meta.get("relations", []) if r["to"] == target
            ]
            for relation in relations or [{"kind": "links"}]:
                edges.append({**relation, "from": path, "to": target})
    return {
        "format_version": 1,
        "okf_version": "0.2",
        "source_fingerprint": source_fingerprint,
        "nodes": nodes,
        "edges": edges,
        "missing_links": [edge for edge in edges if edge["to"] not in concepts],
    }


class Bundle:
    def __init__(self, corpus, root=None):
        self.corpus = Path(corpus)
        self.root = Path(root) if root else self.corpus / "okf_bundle"
        try:
            graph = json.loads((self.root / "knowledge_graph.json").read_text())
            expected = derive_graph(self.root, fingerprint(self.corpus))
            if graph != expected:
                raise BundleError("Bundle is stale or inconsistent; run reindex")
            self.concepts = scan_bundle(self.root)
            self.graph = graph
            sources = load_sources(self.corpus)
            if set(sources) != {
                p for p, c in self.concepts.items() if c.meta.get("doc_id")
            }:
                raise BundleError(
                    "Bundle source versions differ from the supplied corpus"
                )
            for path, source in sources.items():
                concept = self.concepts.get(path)
                if (
                    not concept
                    or concept.source != source["text"]
                    or concept.meta.get("evidence") != source["evidence"]
                ):
                    raise BundleError(
                        "Bundle does not preserve the supplied source and passage spans"
                    )
                if any(
                    concept.meta.get(k) != source["meta"].get(k)
                    for k in (*SOURCE_METADATA, "current")
                ):
                    raise BundleError(
                        "Bundle metadata differs from the supplied source metadata"
                    )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise BundleError(f"Cannot load OKF bundle: {exc}") from exc

    def eligible(self, concept, role, as_of):
        meta = concept.meta
        if "doc_id" not in meta:
            # Directory/topic visibility is derived from reachable source membership.
            # Do not follow source backlinks, which would widen a restricted topic.
            pending, seen = [concept.path], set()
            while pending:
                path = pending.pop()
                if path in seen:
                    continue
                seen.add(path)
                node = self.concepts.get(path)
                if not node:
                    continue
                if node.meta.get("doc_id"):
                    if self.eligible(node, role, as_of):
                        return True
                else:
                    pending.extend(node.links)
            return concept.path in ("/index.md", "/docs/index.md", "/topics/index.md")
        if role not in meta.get("visibility", []):
            return False
        if meta["doc_id"] == POLICY_ID:
            return meta.get("current", False)
        if any(
            c.meta.get("doc_id") == meta.get("superseded_by")
            and c.meta.get("effective_from")
            and c.meta["effective_from"] <= as_of
            for c in self.concepts.values()
            if meta.get("superseded_by")
        ):
            return False
        return in_force(meta, as_of) and any(
            in_force(p, as_of) for p in meta.get("evidence", [])
        )

    def passages(self, concept, role, as_of):
        if not self.eligible(concept, role, as_of):
            return []
        return [
            {
                "doc_id": concept.meta["doc_id"],
                "version": concept.meta["version"],
                "concept": concept.path,
                "metadata": {k: concept.meta.get(k) for k in SOURCE_METADATA},
                **p,
                "text": concept.source[p["start"] : p["end"]],
            }
            for p in concept.meta.get("evidence", [])
            if concept.meta["doc_id"] == POLICY_ID or in_force(p, as_of)
        ]

    def display(self, concept, role, as_of):
        title = concept.meta.get("title", concept.path)
        description = concept.meta.get("description", "")
        if concept.meta.get("type") == "Topic":
            members = [
                self.concepts[p]
                for p in concept.links
                if p in self.concepts and self.concepts[p].meta.get("doc_id")
            ]
            if any(role not in c.meta.get("visibility", []) for c in members):
                title = "Related source documents"
                description = " ".join(
                    c.meta.get("description", "")
                    for c in members
                    if self.eligible(c, role, as_of)
                )
        return title, description

    def links(self, path, role, as_of):
        result = []
        for edge in self.graph["edges"]:
            target = self.concepts.get(edge["to"])
            if edge["from"] == path and target and self.eligible(target, role, as_of):
                if any(
                    section_key in edge
                    and not any(
                        p["section"] == edge[section_key]
                        for p in self.passages(self.concepts[edge[side]], role, as_of)
                    )
                    for side, section_key in (
                        ("from", "from_section"),
                        ("to", "to_section"),
                    )
                ):
                    continue
                title, description = self.display(target, role, as_of)
                result.append({**edge, "title": title, "description": description})
        return result

    def read(self, path, role, as_of):
        concept = self.concepts.get(path)
        if not concept:
            return {"error": "missing_concept"}
        if not self.eligible(concept, role, as_of):
            return {"error": "unavailable"}
        links = self.links(path, role, as_of)
        if "doc_id" in concept.meta:
            return {
                "path": path,
                "title": concept.meta["title"],
                "metadata": {k: concept.meta.get(k) for k in SOURCE_METADATA},
                "passages": self.passages(concept, role, as_of),
                "links": links,
            }
        # Reconstruct index/topic views so hidden links and descriptions never leak.
        markdown = "\n".join(
            f"* [{link['title']}]({link['to']}) - {link['description']}"
            for link in links
        )
        return {
            "path": path,
            "title": self.display(concept, role, as_of)[0],
            "markdown": markdown,
            "links": links,
        }

    def policy(self, role, as_of):
        return [
            c
            for c in self.concepts.values()
            if c.meta.get("doc_id") == POLICY_ID and self.eligible(c, role, as_of)
        ]

    def restricted_metadata(self, role, as_of):
        return [
            {
                "description": c.meta["description"],
                "tags": c.meta.get("tags", []),
                "sections": list(dict.fromkeys(
                    p["section"] for p in c.meta.get("evidence", [])
                    if c.meta["doc_id"] == POLICY_ID or in_force(p, as_of)
                )),
            }
            for c in self.concepts.values()
            if c.meta.get("doc_id")
            and role not in c.meta.get("visibility", [])
            and self.eligible(c, c.meta["visibility"][0], as_of)
        ]

    def document(self, did, role, as_of):
        candidates = [
            c
            for c in self.concepts.values()
            if c.meta.get("doc_id") == did and self.eligible(c, role, as_of)
        ]
        if len(candidates) > 1:
            raise BundleError("Ambiguous source version for requested date")
        return candidates[0] if candidates else None

    def newer_notice(self, concept):
        did = concept.meta["doc_id"]
        newer = [
            c
            for c in self.concepts.values()
            if c.meta.get("doc_id") == did
            and c.meta.get("version", 0) > concept.meta["version"]
        ]
        target = concept.meta.get("superseded_by")
        if newer or any(
            c.meta.get("doc_id") == target for c in self.concepts.values() if target
        ):
            return f"{did} version {concept.meta['version']} is historical; a newer version exists in the corpus."
        return None
