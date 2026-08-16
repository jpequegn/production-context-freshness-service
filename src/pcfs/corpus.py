"""Load and validate the versioned fictional operational corpus."""

from __future__ import annotations

from datetime import datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml
from pydantic import model_validator

from pcfs.models import ContextState, Sensitivity, StrictModel


class ServiceFixture(StrictModel):
    id: str
    owner: str
    dependencies: tuple[str, ...]
    slo: str
    metric: str
    procedure: str
    flag: str
    sensitivity: Sensitivity


class ReleaseFixture(StrictModel):
    id: str
    service: str
    deployed_at: datetime


class IncidentFixture(StrictModel):
    id: str
    service: str
    opened_at: datetime
    closed_at: datetime
    observation: str
    mitigation: str

    @model_validator(mode="after")
    def validate_interval(self) -> IncidentFixture:
        if self.closed_at <= self.opened_at:
            raise ValueError("incident closure must follow opening")
        return self


class ChangeFixture(StrictModel):
    id: str
    type: str
    service: str
    occurred_at: datetime
    old_value: str
    new_value: str


class QuestionTemplate(StrictModel):
    kind: str
    prompt: str


class EvalQuestion(StrictModel):
    id: str
    incident_id: str
    service: str
    kind: str
    prompt: str
    question_time: datetime
    expected_state: ContextState
    expected_answer_ids: tuple[str, ...] = ()


class Corpus(StrictModel):
    fictional: bool
    corpus_version: str
    description: str
    services: tuple[ServiceFixture, ...]
    releases: tuple[ReleaseFixture, ...]
    incidents: tuple[IncidentFixture, ...]
    changes: tuple[ChangeFixture, ...]
    question_templates: tuple[QuestionTemplate, ...]

    def questions(self) -> tuple[EvalQuestion, ...]:
        services = {service.id: service for service in self.services}
        changes = sorted(self.changes, key=lambda change: change.occurred_at)
        templates = {template.kind: template for template in self.question_templates}
        questions: list[EvalQuestion] = []

        for index, incident in enumerate(self.incidents, start=1):
            service = services[incident.service]
            prior_changes = [
                change
                for change in changes
                if change.service == service.id and change.occurred_at <= incident.opened_at
            ]
            owner = service.owner
            metric = service.metric
            for change in prior_changes:
                if change.type == "ownership_change":
                    owner = change.new_value
                elif change.type == "metric_rename":
                    metric = change.new_value

            owner_state = (
                ContextState.DISPUTED
                if any(change.type == "authority_conflict" for change in prior_changes)
                else ContextState.CURRENT
            )
            questions.append(
                _question(
                    index,
                    incident,
                    templates["owner_at_open"],
                    incident.opened_at,
                    owner_state,
                    () if owner_state is ContextState.DISPUTED else (f"owner:{owner}",),
                )
            )

            renamed = any(change.type == "metric_rename" for change in prior_changes)
            metric_state = ContextState.CURRENT if renamed or index % 3 else ContextState.STALE
            questions.append(
                _question(
                    index,
                    incident,
                    templates["metric_at_open"],
                    incident.opened_at,
                    metric_state,
                    (f"metric:{metric}",) if metric_state is ContextState.CURRENT else (),
                )
            )

            questions.append(
                _question(
                    index,
                    incident,
                    templates["mitigation_after_close"],
                    incident.closed_at + timedelta(minutes=1),
                    ContextState.STALE,
                    (),
                )
            )
            missing_state = (
                ContextState.UNKNOWN if index % 2 else ContextState.INSUFFICIENT_EVIDENCE
            )
            questions.append(
                _question(
                    index,
                    incident,
                    templates["undocumented_objective"],
                    incident.opened_at,
                    missing_state,
                    (),
                )
            )
        return tuple(questions)


def _question(
    index: int,
    incident: IncidentFixture,
    template: QuestionTemplate,
    question_time: datetime,
    state: ContextState,
    answers: tuple[str, ...],
) -> EvalQuestion:
    return EvalQuestion(
        id=f"q-{index:02d}-{template.kind}",
        incident_id=incident.id,
        service=incident.service,
        kind=template.kind,
        prompt=template.prompt.format(service=incident.service, incident=incident.id),
        question_time=question_time,
        expected_state=state,
        expected_answer_ids=answers,
    )


def load_corpus() -> Corpus:
    resource = files("pcfs").joinpath("fixtures/corpus.yaml")
    raw: dict[str, Any] = yaml.safe_load(resource.read_text())
    return Corpus.model_validate(raw)


def validate_corpus(corpus: Corpus) -> list[str]:
    errors: list[str] = []
    service_ids = {service.id for service in corpus.services}
    if not corpus.fictional:
        errors.append("corpus must declare fictional: true")
    if len(corpus.services) < 8:
        errors.append("corpus must contain at least eight services")
    if len(corpus.incidents) < 15:
        errors.append("corpus must contain at least fifteen incidents")
    for service in corpus.services:
        unknown = set(service.dependencies) - service_ids
        if unknown:
            errors.append(f"{service.id} has unknown dependencies: {sorted(unknown)}")
    for item in (*corpus.releases, *corpus.incidents, *corpus.changes):
        if item.service not in service_ids:
            errors.append(f"{item.id} references unknown service {item.service}")
    questions = corpus.questions()
    if len(questions) < 60:
        errors.append("corpus must generate at least sixty questions")
    covered = {question.expected_state for question in questions}
    missing = set(ContextState) - covered
    if missing:
        errors.append(f"question set is missing states: {sorted(state.value for state in missing)}")
    serialized = corpus.model_dump_json().lower()
    for real_name in ("amazon", "anthropic", "google", "meta", "microsoft", "openai"):
        if real_name in serialized:
            errors.append(f"corpus contains disallowed real organization name: {real_name}")
    return errors
