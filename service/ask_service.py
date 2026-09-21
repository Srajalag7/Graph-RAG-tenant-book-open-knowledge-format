#!/usr/bin/env python3
"""FastAPI service backed by Gemini traversal of a local OKF bundle."""

import argparse
from datetime import date
import json
import logging
from pathlib import Path
import re
import time
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
import uvicorn

from config import CORPUS_DIR, PORT, QUERY_TIMEOUT
from gemini import GeminiClient, ServiceError, decode_json
from graph_traversal import traverse
from okf import Bundle, BundleError, SOURCE_METADATA, norm, sections, source_span
from prompts import CHATBOT_SYSTEM_PROMPT

Role = Literal["renter", "landlord", "staff"]
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
ANSWER_SCHEMA = json.loads(
    (FRONTEND.parent.parent / "schemas" / "response.schema.json").read_text()
)
ANSWER_SCHEMA.pop("$schema", None)
ANSWER_SCHEMA["properties"].pop("id")
ANSWER_SCHEMA["required"] = ["status"]
log = logging.getLogger(__name__)
SOURCE_MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable("image")


def source_html(text):
    # Metadata comments are not prose; raw HTML and remote images stay disabled.
    return SOURCE_MARKDOWN.render(re.sub(r"<!--[\s\S]*?-->", "", text))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AskRequest(StrictModel):
    id: str
    question: str = Field(min_length=1)
    role: Role
    as_of: str

    @field_validator("as_of")
    @classmethod
    def valid_date(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Use YYYY-MM-DD")
        return value


class Citation(StrictModel):
    doc_id: str
    section: str
    quote: str = Field(min_length=20)


class Governing(StrictModel):
    doc_id: str
    reason: str
    overridden: list[str] = Field(default_factory=list)


class Answer(StrictModel):
    id: str
    status: Literal["answered", "refused"]
    answer: str | None = None
    refusal_reason: Literal["out_of_corpus", "not_permitted"] | None = None
    refusal_message: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    governing: Governing | None = None
    superseded_notice: str | None = None


def validate_answer(raw, question, retrieval, bundle):
    response = Answer.model_validate({**raw, "id": question.id})
    permitted = {p["doc_id"] for p in retrieval.evidence}
    if response.status == "refused":
        if (
            response.citations
            or response.answer
            or response.governing
            or not response.refusal_reason
        ):
            raise ValueError(
                "Refusal must have a reason and no answer, citations, or governing document"
            )
        if response.refusal_reason == "not_permitted" and not retrieval.restricted:
            raise ValueError(
                "No restricted evidence was identified by the access check"
            )
        if retrieval.restricted:
            response.refusal_reason = "not_permitted"
        # User-facing refusals never echo model-produced restricted names or details.
        response.refusal_message = (
            "The information needed to answer is restricted for your role. Please contact openigloo staff."
            if response.refusal_reason == "not_permitted"
            else "The tenant book does not contain enough information to answer this question."
        )
        response.superseded_notice = None
        return response.model_dump()
    if (
        not response.answer
        or not response.answer.strip()
        or not response.citations
        or response.refusal_reason
    ):
        raise ValueError(
            "An answer requires text and supporting citations, and cannot have a refusal reason"
        )
    if retrieval.restricted:
        raise ValueError("Required information is restricted; return not_permitted without an answer")
    notices = []
    for citation in response.citations:
        candidates = [
            p
            for p in retrieval.evidence
            if p["doc_id"] == citation.doc_id
            and norm(p["section"]) == norm(citation.section)
        ]
        if not candidates or norm(citation.quote) not in norm(
            "\n\n".join(p["text"] for p in candidates)
        ):
            raise ValueError(
                "Citation is not in the eligible evidence read for that section"
            )
        concept = bundle.concepts[candidates[0]["concept"]]
        matching_sections = [
            (start, end)
            for heading, start, end in sections(concept.source)
            if norm(heading) == norm(citation.section)
        ]
        if not any(
            source_span(concept.source, citation.quote, start, end)
            for start, end in matching_sections
        ):
            raise ValueError("Quote does not occur in the original source section")
        citation.section = candidates[0]["section"]
        notice = bundle.newer_notice(concept)
        if notice and notice not in notices:
            notices.append(notice)
    if response.governing:
        refs = {response.governing.doc_id, *response.governing.overridden}
        if not refs <= permitted or not response.governing.reason.strip():
            raise ValueError("Governing references must be documents actually read")
        if not refs <= {c.doc_id for c in response.citations}:
            missing = sorted(refs - {c.doc_id for c in response.citations})
            raise ValueError(
                f"Cite both the governing and overridden documents. Missing citations: {missing}"
            )
    # The model never receives restricted bodies; additionally reject explicit restricted identities.
    output = norm(
        response.answer
        + " "
        + (response.governing.reason if response.governing else "")
    )
    for concept in bundle.concepts.values():
        if concept.meta.get("doc_id") and question.role not in concept.meta.get(
            "visibility", []
        ):
            if (
                norm(concept.meta["doc_id"]) in output
                or norm(concept.meta["title"]) in output
            ):
                raise ValueError("Response names restricted material")
    response.refusal_message = None
    response.superseded_notice = " ".join(notices) or None
    return response.model_dump()


def answer_question(question, bundle, client):
    retrieval = traverse(
        bundle, question.question, question.role, question.as_of, client
    )

    if retrieval.restricted:
        response = validate_answer(
            {"status": "refused", "refusal_reason": "not_permitted"},
            question, retrieval, bundle,
        )
        return {"response": response, "traversal": retrieval.trace, "usage": client.usage()}

    def answer_context():
        return {
            "question": question.question,
            "role": question.role,
            "as_of": question.as_of,
            "evidence": [p for p in retrieval.evidence if p not in retrieval.policy],
            "required_restricted_evidence": retrieval.restricted,
        }

    context = answer_context()

    def evidence_message():
        return json.dumps(context, ensure_ascii=False) + (
            "\n\nWrite answer as your own explanation addressed to this requester, not a copied source passage "
            "or a description of what an assistant should do. Apply the current policy to your response. "
            "Preserve the source meaning and give supported next steps without inventing facts or actions. "
            "Do not speak as the source organization or promise its actions. "
            "Put verbatim supporting text in citations[].quote, without duplicating it as the answer. "
            "Return the required JSON."
        )

    messages = [
        {"role": "system", "content": CHATBOT_SYSTEM_PROMPT +
         "\n\nCurrent assistant policy, including its exact citable source sections:\n" +
         json.dumps(retrieval.policy, ensure_ascii=False)},
        {"role": "user", "content": evidence_message()},
    ]
    for attempt in range(2):
        try:
            candidate = client.complete(messages, schema=ANSWER_SCHEMA)
            raw = decode_json(candidate)
            if (
                raw.get("status") == "refused"
                and "/docs/index.md" not in retrieval.read_paths
            ):
                retrieval = traverse(
                    bundle,
                    question.question,
                    question.role,
                    question.as_of,
                    client,
                    previous=retrieval,
                    review_index=True,
                )
                context = answer_context()
                messages[1]["content"] = evidence_message()
                candidate = client.complete(messages, schema=ANSWER_SCHEMA)
                raw = decode_json(candidate)
            response = validate_answer(raw, question, retrieval, bundle)
            return {
                "response": response,
                "traversal": retrieval.trace,
                "usage": client.usage(),
            }
        except (ValidationError, ValueError, ServiceError) as exc:
            if isinstance(exc, ServiceError) and exc.status != 502:
                raise
            detail = (
                {"schema_errors": [
                    {"field": ".".join(map(str, error["loc"])), "type": error["type"]}
                    for error in exc.errors(include_input=False, include_url=False)
                ]}
                if isinstance(exc, ValidationError)
                else "upstream_output" if isinstance(exc, ServiceError)
                else str(exc)
            )
            log.warning("Question %s answer validation attempt %s: %s", question.id, attempt + 1, detail)
            if attempt:
                failure = ServiceError(
                    "invalid_model_response",
                    "The generated answer could not be verified. Please retry.",
                    502,
                )
                failure.diagnostics = {"stage": "answer_validation", "detail": detail}
                raise failure from exc
            context["required_restricted_evidence"] = retrieval.restricted
            messages[1]["content"] = evidence_message()
            if "candidate" in locals():
                messages.append(candidate)
            messages.append(
                {
                    "role": "user",
                    "content": "Return a corrected complete response. Validation failed: "
                    + json.dumps(detail, ensure_ascii=False),
                }
            )


def create_app():
    app = FastAPI(title="Ask the Tenant Book")
    app.state.bundle = None

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        headers = {"Retry-After": "5"} if exc.status == 503 else None
        return JSONResponse(
            status_code=exc.status, content=exc.payload(), headers=headers
        )

    def current_bundle():
        if app.state.bundle is None:
            raise ServiceError(
                "index_unavailable",
                "The knowledge bundle is unavailable. Run reindex before starting the service.",
            )
        return app.state.bundle

    @app.post("/ask")
    def ask(question: AskRequest, trace: bool = False):
        client = GeminiClient(deadline=time.monotonic() + QUERY_TIMEOUT)
        try:
            result = answer_question(question, current_bundle(), client)
        except ServiceError as exc:
            exc.usage = client.usage()
            log.warning(
                "Question %s failed: %s; usage=%s",
                question.id,
                exc.code,
                client.usage(),
            )
            raise
        log.info("Question %s usage=%s", question.id, result["usage"])
        return result if trace else result["response"]

    @app.get("/doc/{doc_id}")
    def get_doc(doc_id: str, role: Role, as_of: str, quote: str = ""):
        try:
            AskRequest(id="", question="source", role=role, as_of=as_of)
            concept = current_bundle().document(doc_id, role, as_of)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid date") from exc
        except BundleError as exc:
            raise ServiceError(
                "index_inconsistent",
                "The source version could not be resolved. Reindex the corpus.",
                500,
            ) from exc
        if not concept:
            raise HTTPException(404, "Document unavailable for this role and date")
        return {
            "doc_id": doc_id,
            "text": concept.source,
            "html": source_html(concept.source),
            "quote_html": source_html(quote),
            "meta": {k: concept.meta.get(k) for k in SOURCE_METADATA},
            "superseded_notice": current_bundle().newer_notice(concept),
        }

    @app.get("/docs")
    def list_docs(role: Role, as_of: str):
        try:
            AskRequest(id="", question="sources", role=role, as_of=as_of)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid date") from exc
        b = current_bundle()
        return [
            {
                "doc_id": c.meta["doc_id"],
                "title": c.meta["title"],
                "version": c.meta["version"],
            }
            for c in b.concepts.values()
            if c.meta.get("doc_id") and b.eligible(c, role, as_of)
        ]

    @app.get("/health")
    def health():
        current_bundle()
        return {"status": "ready"}

    @app.get("/")
    def frontend():
        return FileResponse(FRONTEND)

    return app


app = create_app()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=CORPUS_DIR)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    try:
        app.state.bundle = Bundle(args.corpus)
        # Missing configuration fails at startup, rather than on every request.
        GeminiClient()
    except (BundleError, ServiceError, OSError) as exc:
        parser.exit(1, f"Cannot start: {exc}\n")
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
