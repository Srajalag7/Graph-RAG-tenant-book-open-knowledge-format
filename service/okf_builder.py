#!/usr/bin/env python3
"""Build an OKF v0.2 bundle from a complete supplied corpus without editing inputs."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time

from config import CORPUS_DIR
from gemini import GeminiClient, ServiceError, decode_json
from okf import (
    Bundle,
    BundleError,
    POLICY_ID,
    derive_graph,
    fingerprint,
    load_sources,
    serialize,
)
from prompts import KNOWLEDGE_GRAPH_PROMPT


def validate_enrichment(result, sources):
    docs, topics, relations = (
        result.get("documents"),
        result.get("topics"),
        result.get("relationships"),
    )
    if not isinstance(docs, dict) or set(docs) != set(sources):
        missing = (
            sorted(set(sources) - set(docs))
            if isinstance(docs, dict)
            else sorted(sources)
        )
        unknown = sorted(set(docs) - set(sources)) if isinstance(docs, dict) else []
        raise BundleError(
            f"Documents must use exact concept paths. Missing: {missing}. Unknown: {unknown}"
        )
    for value in docs.values():
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("description"), str)
            or not value["description"].strip()
        ):
            raise BundleError("Missing document description")
        if not isinstance(value.get("tags"), list) or not all(
            isinstance(t, str) for t in value["tags"]
        ):
            raise BundleError("Invalid document tags")
    if not isinstance(topics, list) or not topics or not isinstance(relations, list):
        raise BundleError("Missing topics or relationships")
    covered, ids = set(), set()
    for topic in topics:
        if (
            not isinstance(topic, dict)
            or not isinstance(topic.get("id"), str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]*", topic["id"])
        ):
            raise BundleError("Invalid topic ID")
        if topic["id"] in ids or not all(
            isinstance(topic.get(k), str) and topic[k].strip()
            for k in ("title", "description")
        ):
            raise BundleError("Duplicate or incomplete topic")
        ids.add(topic["id"])
        members = topic.get("documents")
        if (
            not isinstance(members, list)
            or not members
            or not all(isinstance(p, str) and p in sources for p in members)
        ):
            raise BundleError("Topic references an unknown source")
        covered.update(members)
    if covered != set(sources):
        raise BundleError("Topics do not cover every source")
    for relation in relations:
        if not isinstance(relation, dict) or relation.get("kind") not in (
            "conflicts",
            "interprets",
        ):
            raise BundleError("Invalid relationship kind")
        for side in ("from", "to"):
            if not isinstance(relation.get(side), str) or relation[side] not in sources:
                raise BundleError("Relationship references an unknown source")
            headings = {section[0] for section in sources[relation[side]]["sections"]}
            if (
                not isinstance(relation.get(side + "_section"), str)
                or relation[side + "_section"] not in headings
            ):
                raise BundleError(
                    f"Relationship {side}_section {relation.get(side + '_section')!r} for "
                    f"{relation[side]} must be one of {sorted(headings)!r}"
                )
        if relation["from"] == relation["to"]:
            raise BundleError("Self relationship is not useful")
    return result


def enrich(sources, client):
    payload = [
        {
            "path": path,
            "metadata": source["meta"],
            "section_headings": [s[0] for s in source["sections"]],
            "text": source["text"],
        }
        for path, source in sources.items()
    ]
    messages = [
        {"role": "system", "content": KNOWLEDGE_GRAPH_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    def obj(properties):
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    string = {"type": "string"}
    source_path = {"type": "string", "enum": list(sources)}
    description = obj(
        {"description": string, "tags": {"type": "array", "items": string}}
    )
    schema = obj(
        {
            "documents": obj({p: description for p in sources}),
            "topics": {
                "type": "array",
                "items": obj(
                    {
                        "id": string,
                        "title": string,
                        "description": string,
                        "documents": {"type": "array", "items": source_path},
                    }
                ),
            },
            "relationships": {
                "type": "array",
                "items": obj(
                    {
                        "from": source_path,
                        "to": source_path,
                        "kind": {"type": "string", "enum": ["conflicts", "interprets"]},
                        "from_section": string,
                        "to_section": string,
                    }
                ),
            },
        }
    )
    for attempt in range(2):
        try:
            message = client.complete(messages, schema=schema, timeout=180)
            return validate_enrichment(decode_json(message), sources)
        except (BundleError, ServiceError) as exc:
            if isinstance(exc, ServiceError) and exc.status != 502:
                raise
            print(f"Bundle description validation: {exc}", flush=True)
            if attempt:
                raise BundleError(
                    f"Gemini did not produce a valid complete bundle description: {exc}"
                ) from exc
            if "message" in locals():
                messages.append(message)
            messages.append(
                {
                    "role": "user",
                    "content": f"Return corrected complete JSON. Validation error: {exc}",
                }
            )


def write_bundle(root, sources, result, source_hash, model):
    now = datetime.now(timezone.utc)
    generated = {"by": f"tenant_book/{model}", "at": now.isoformat(timespec="seconds")}
    topic_locations = {}
    for topic in result["topics"]:
        visibility = {
            tuple(sorted(sources[p]["meta"]["visibility"])) for p in topic["documents"]
        }
        slug = topic["id"]
        if len(visibility) > 1:
            # A mixed topic's identifier must not reveal names from its restricted members.
            slug = "shared_" + hashlib.sha256(slug.encode()).hexdigest()[:12]
        topic_locations[topic["id"]] = f"/topics/{slug}.md"
    relations = {path: [] for path in sources}
    for relation in result["relationships"]:
        relations[relation["from"]].append(
            {k: relation[k] for k in ("to", "kind", "from_section", "to_section")}
        )
        if relation["kind"] == "conflicts":
            relations[relation["to"]].append(
                {
                    "to": relation["from"],
                    "kind": "conflicts",
                    "from_section": relation["to_section"],
                    "to_section": relation["from_section"],
                }
            )
    for path, source in sources.items():
        meta = source["meta"]
        # Version links come only from corpus metadata, never model suggestions.
        for other, target in sources.items():
            if other == path:
                continue
            same_doc = target["meta"]["doc_id"] == meta["doc_id"]
            replacement = target["meta"]["doc_id"] == meta.get("superseded_by")
            previous = target["meta"]["doc_id"] == meta.get("supersedes")
            if same_doc or replacement or previous:
                relations[path].append({"to": other, "kind": "version"})

    def write(path, text):
        file = root / path.lstrip("/")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")

    def label(text):
        return text.replace("[", "\\[").replace("]", "\\]").replace("\n", " ")

    def entry(path):
        source = sources[path]
        return f"* [{label(source['meta']['title'])}, v{source['meta']['version']}]({path}) - {result['documents'][path]['description']}\n"

    for path, source in sources.items():
        meta = source["meta"]
        topic_paths = [
            topic_locations[t["id"]] for t in result["topics"] if path in t["documents"]
        ]
        fm = {
            "type": "Policy" if meta["doc_id"] == POLICY_ID else "Reference",
            "title": meta["title"],
            **result["documents"][path],
            "status": (
                "deprecated"
                if not meta["current"] or meta.get("superseded_by")
                else "stable"
            ),
            "generated": generated,
            "sources": [
                {"resource": f"corpus:{source['source_path']}", "title": meta["title"]}
            ],
            **{
                k: meta.get(k)
                for k in (
                    "doc_id",
                    "version",
                    "visibility",
                    "issuer",
                    "layer",
                    "source_url",
                    "notes",
                    "effective_from",
                    "effective_to",
                    "supersedes",
                    "superseded_by",
                    "current",
                )
            },
            "source_path": source["source_path"],
            "source_length": len(source["text"]),
            "evidence": source["evidence"],
            "relations": relations[path],
        }
        links = "\n\n# Related concepts\n\n"
        for relation in relations[path]:
            links += f"* {relation['kind']}: [{label(sources[relation['to']]['meta']['title'])}]({relation['to']})"
            if "from_section" in relation:
                links += f" — {label(relation['from_section'])} ↔ {label(relation['to_section'])}"
            links += "\n"
        links += "".join(
            f"* Topic: [{p.rsplit('/', 1)[1][:-3]}]({p})\n" for p in topic_paths
        )
        links += f"* [Document versions](/docs/{meta['doc_id']}/index.md)\n"
        write(path, serialize(fm, source["text"] + links))
    topic_entries = []
    for topic in result["topics"]:
        path = topic_locations[topic["id"]]
        fm = {
            "type": "Topic",
            "title": topic["title"],
            "description": topic["description"],
            "tags": [topic["id"]],
            "status": "stable",
            "generated": generated,
            "sources": [{"resource": p} for p in topic["documents"]],
        }
        write(
            path,
            serialize(
                fm,
                "# Sources\n\n"
                + "".join(entry(p) for p in dict.fromkeys(topic["documents"])),
            ),
        )
        topic_entries.append(
            f"* [{label(topic['title'])}]({path}) - {topic['description']}\n"
        )
    write("/topics/index.md", "# Topics\n\n" + "".join(topic_entries))
    write(
        "/docs/index.md",
        "# Source documents and versions\n\n" + "".join(entry(p) for p in sources),
    )
    for did in {s["meta"]["doc_id"] for s in sources.values()}:
        write(
            f"/docs/{did}/index.md",
            "# Versions\n\n"
            + "".join(
                entry(p) for p, s in sources.items() if s["meta"]["doc_id"] == did
            ),
        )
    write(
        "/index.md",
        serialize(
            {"okf_version": "0.2"},
            "# Tenant Book\n\n* [Topics](/topics/index.md) - Browse related subjects.\n"
            "* [Documents](/docs/index.md) - Browse all source documents and versions.\n",
        ),
    )
    write(
        "/log.md",
        f"# Bundle update log\n\n## {now.date()}\n\n* **Rebuild**: Generated from corpus content `{source_hash}`.\n",
    )
    graph = derive_graph(root, source_hash)
    if graph["missing_links"]:
        missing = sorted({edge["to"] for edge in graph["missing_links"]})
        raise BundleError(f"Bundle contains missing concept links: {missing}")
    (root / "knowledge_graph.json").write_text(json.dumps(graph, indent=2))


def build(corpus, output=None):
    started = time.monotonic()
    corpus = Path(corpus).resolve()
    output = Path(output).resolve() if output else corpus / "okf_bundle"
    if output.exists() and not output.is_dir():
        raise BundleError("Bundle output must be a directory")
    if (
        output == corpus
        or output in corpus.parents
        or output == corpus / "docs"
        or (corpus / "docs") in output.parents
    ):
        raise BundleError("Output must not replace source corpus files")
    source_hash = fingerprint(corpus)
    sources = load_sources(corpus)
    client = GeminiClient(deadline=started + 840)
    print(f"Building OKF from {len(sources)} source versions...", flush=True)
    result = enrich(sources, client)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".okf-build-", dir=output.parent))
    backup = None
    try:
        write_bundle(staging, sources, result, source_hash, client.model)
        Bundle(corpus, staging)
        if fingerprint(corpus) != source_hash:
            raise BundleError(
                "Corpus changed during reindex; retry with a stable corpus"
            )
        if time.monotonic() - started >= 840:
            raise BundleError("Reindex exceeded its time budget")
        if output.exists():
            backup = Path(tempfile.mkdtemp(prefix=".okf-backup-", dir=output.parent))
            backup.rmdir()
            output.rename(backup)
        try:
            staging.rename(output)
        except OSError:
            if backup:
                backup.rename(output)
                backup = None
            raise
        if backup:
            shutil.rmtree(backup)
        print(
            f"OKF bundle ready: {output}\nUsage: {json.dumps(client.usage())}",
            flush=True,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus_dir", nargs="?", default=CORPUS_DIR)
    parser.add_argument("--okf-dir")
    args = parser.parse_args()
    try:
        build(args.corpus_dir, args.okf_dir)
    except (BundleError, ServiceError, OSError) as exc:
        parser.exit(1, f"Reindex failed: {exc}\n")


if __name__ == "__main__":
    main()
