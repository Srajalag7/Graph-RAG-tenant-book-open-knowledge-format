"""Gemini follows OKF links through a constrained read-only tool."""

import json
import time
from dataclasses import dataclass, field

from config import MAX_TRAVERSAL_ROUNDS
from gemini import ServiceError, decode_json
from okf import POLICY_ID, resolve_link
from prompts import GRAPH_TRAVERSAL_PROMPT, RESTRICTED_RELEVANCE_PROMPT

NAVIGATION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_concepts",
            "description": "Read linked OKF indexes or concepts. Request several known paths together.",
            "parameters": {
                "type": "object",
                "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_navigation",
            "description": "Finish source collection. This does not answer the question. sufficient is true only when all necessary evidence has been inspected.",
            "parameters": {
                "type": "object",
                "properties": {"sufficient": {"type": "boolean"}},
                "required": ["sufficient"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class Retrieval:
    evidence: list = field(default_factory=list)
    policy: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    read_paths: set = field(default_factory=set)
    sufficient: bool = False
    restricted: bool = False
    rounds: int = 0


def restricted_relevant(bundle, question, role, as_of, client, evidence):
    metadata = bundle.restricted_metadata(role, as_of)
    if not metadata:
        return False
    result = decode_json(
        client.complete(
            [
                {"role": "system", "content": RESTRICTED_RELEVANCE_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps({
                        "question": question,
                        "role": role,
                        "as_of": as_of,
                        "permitted_evidence": [
                            {"section": p["section"], "text": p["text"]}
                            for p in evidence
                        ],
                        "restricted_scopes": metadata,
                    }, ensure_ascii=False),
                },
            ],
            json_output=True,
        )
    )
    if type(result.get("relevant")) is not bool:
        raise ServiceError(
            "invalid_model_response", "The access check could not be completed.", 502
        )
    return result["relevant"]


def traverse(
    bundle,
    question,
    role,
    as_of,
    client,
    max_rounds=MAX_TRAVERSAL_ROUNDS,
    previous=None,
    review_index=False,
):
    result = previous or Retrieval()
    discovered = {"/index.md": None}
    for step in result.trace:
        discovered[step["path"]] = step["from"]
        for link in bundle.links(step["path"], role, as_of):
            discovered.setdefault(link["to"], step["path"])
    evidence_keys = {(p["concept"], p["start"], p["end"]) for p in result.evidence}

    def read(path):
        if path not in discovered:
            return {
                "error": "undiscovered_path",
                "message": "Follow a link from an index or a previously read concept.",
            }
        if path in result.read_paths:
            return {"path": path, "already_read": True}
        view = bundle.read(path, role, as_of)
        if "error" in view:
            return view
        result.read_paths.add(path)
        result.trace.append(
            {"path": path, "from": discovered[path], "title": view["title"]}
        )
        for link in view["links"]:
            discovered.setdefault(link["to"], path)
        for passage in view.get("passages", []):
            key = (passage["concept"], passage["start"], passage["end"])
            if key not in evidence_keys:
                evidence_keys.add(key)
                result.evidence.append(passage)
                if passage["doc_id"] == POLICY_ID:
                    result.policy.append(passage)
        return view

    root = read("/index.md")
    # Policy is a contract-defined root, loaded from current bundle text, never from a prompt.
    policy_views = []
    for policy in bundle.policy(role, as_of):
        discovered[policy.path] = "/index.md"
        read(policy.path)
        policy_views.append(bundle.read(policy.path, role, as_of))
    catalog = read("/docs/index.md") if review_index else None
    messages = [
        {"role": "system", "content": GRAPH_TRAVERSAL_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question": question,
                    "role": role,
                    "as_of": as_of,
                    "index": root,
                    "current_policy": policy_views,
                    "document_index_review": catalog,
                    "previous_evidence": result.evidence if previous else [],
                },
                ensure_ascii=False,
            ),
        },
    ]
    for _ in range(result.rounds, max_rounds):
        if client.deadline and time.monotonic() >= client.deadline:
            raise ServiceError(
                "request_timeout", "The request took too long. Please retry."
            )
        message = client.complete(
            messages, tools=NAVIGATION_TOOLS, tool_choice="required"
        )
        result.rounds += 1
        messages.append(message)
        calls = message.get("tool_calls") or []
        completion = None
        if calls:
            if not isinstance(calls, list) or not all(
                isinstance(c, dict)
                and isinstance(c.get("function"), dict)
                and isinstance(c.get("id"), str)
                for c in calls
            ):
                raise ServiceError(
                    "invalid_model_response",
                    "The model returned an invalid navigation request.",
                    502,
                )
            for call in calls:
                try:
                    args = json.loads(call["function"]["arguments"])
                    if call["function"]["name"] == "finish_navigation":
                        if (
                            set(args) != {"sufficient"}
                            or type(args["sufficient"]) is not bool
                        ):
                            raise ValueError("Expected sufficient")
                        completion = args
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": '{"status":"checking_completion"}',
                            }
                        )
                        continue
                    if call["function"]["name"] != "read_concepts":
                        raise ValueError("Unknown tool")
                    if (
                        set(args) != {"paths"}
                        or not isinstance(args["paths"], list)
                        or not args["paths"]
                    ):
                        raise ValueError("Expected paths")
                    views = []
                    for path in args["paths"]:
                        if not isinstance(path, str) or not path.startswith("/"):
                            raise ValueError(
                                "Use a bundle-root path from a returned link"
                            )
                        target = resolve_link("/index.md", path)
                        views.append(read(target))
                except (ValueError, KeyError, TypeError):
                    views = [
                        {
                            "error": "invalid_tool_arguments",
                            "message": "Use read_concepts with a list of known bundle-root paths.",
                        }
                    ]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(views, ensure_ascii=False),
                    }
                )
            if completion is None:
                continue
        else:
            raise ServiceError(
                "invalid_model_response",
                "The model did not return a required navigation tool call. Please retry.",
                502,
            )
        if completion["sufficient"] and not any(
            path != "/index.md"
            and bundle.concepts[path].meta.get("doc_id") != POLICY_ID
            for path in result.read_paths
        ):
            # Supply the catalog instead of repeatedly asking a model that keeps finishing.
            discovered.setdefault("/docs/index.md", "/index.md")
            catalog = read("/docs/index.md")
            messages.append({
                "role": "user",
                "content": "Review this catalog for any missing sources. You may finish with policy alone "
                "when the question is about assistant behavior and the policy fully answers it. "
                + json.dumps(catalog, ensure_ascii=False),
            })
            continue
        pending = set()
        for path in result.read_paths:
            concept = bundle.concepts[path]
            # The mandatory policy root is a hub; its unrelated edges must not pull in the whole corpus.
            if not concept.meta.get("doc_id") or concept.meta["doc_id"] == POLICY_ID:
                continue
            for link in bundle.links(path, role, as_of):
                if link["kind"] == "conflicts" and link["to"] not in result.read_paths:
                    pending.add(link["to"])
        if pending:
            messages.append(
                {
                    "role": "user",
                    "content": "Before completing, read these applicable conflict counterparts: "
                    + json.dumps(sorted(pending)),
                }
            )
            continue
        if not completion["sufficient"] and "/docs/index.md" not in result.read_paths:
            discovered.setdefault("/docs/index.md", "/index.md")
            view = read("/docs/index.md")
            messages.append(
                {
                    "role": "user",
                    "content": "Check the complete document index for further relevant evidence: "
                    + json.dumps(view, ensure_ascii=False),
                }
            )
            continue
        result.sufficient = completion["sufficient"]
        # Compare requested information with evidence the role can actually read.
        # Recheck after continued navigation because the permitted evidence may have changed.
        result.restricted = restricted_relevant(
            bundle, question, role, as_of, client, result.evidence
        )
        return result
    raise ServiceError(
        "retrieval_incomplete",
        "The source review could not finish within the navigation budget. Please retry or narrow the question.",
    )
