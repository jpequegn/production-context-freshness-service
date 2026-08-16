"""Reproducible retrieval baselines and point-in-time evaluation reports."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pcfs import __version__
from pcfs.corpus import Corpus, EvalQuestion, load_corpus
from pcfs.models import AccessContext, ContextState, Fact, FactType, StrictModel
from pcfs.retrieval import ContextQuery, ContextRetriever
from pcfs.store import Repository


class SystemName(StrEnum):
    NAIVE_TEXT = "naive_text"
    CURRENT_KEYWORD = "current_keyword"
    TEMPORAL_PROVENANCE = "temporal_provenance"


class EvaluationOutcome(StrictModel):
    system: SystemName
    question_id: str
    expected_state: ContextState
    predicted_state: ContextState
    expected_answer_ids: tuple[str, ...]
    predicted_answer_ids: tuple[str, ...]
    citation_ids: tuple[str, ...]
    correct_state: bool
    correct_answer: bool
    stale_fact_used: bool
    conflict_recognized: bool
    unsupported_answer: bool
    abstention_correct: bool


class SystemMetrics(StrictModel):
    system: SystemName
    total: int
    state_accuracy: float
    answer_accuracy: float
    stale_fact_use_rate: float
    conflict_recognition_rate: float
    unsupported_answer_rate: float
    correct_abstention_rate: float
    citation_precision: float
    citation_recall: float


class EvaluationReport(StrictModel):
    generated_at: datetime
    package_version: str
    corpus_version: str
    question_count: int
    systems: tuple[SystemMetrics, ...]
    outcomes: tuple[EvaluationOutcome, ...]
    minimum_state_accuracy: float
    passed: bool
    gate_reasons: tuple[str, ...]


class _Prediction(StrictModel):
    state: ContextState
    answer_ids: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = ()
    stale_fact_used: bool = False


def run_evaluation(
    repository: Repository,
    *,
    corpus: Corpus | None = None,
    minimum_state_accuracy: float = 0.8,
) -> EvaluationReport:
    corpus = corpus or load_corpus()
    facts = repository.list_facts()
    retriever = ContextRetriever(repository)
    outcomes: list[EvaluationOutcome] = []
    for question in corpus.questions():
        predictions = {
            SystemName.NAIVE_TEXT: _naive_prediction(facts, question),
            SystemName.CURRENT_KEYWORD: _current_prediction(facts, question),
            SystemName.TEMPORAL_PROVENANCE: _temporal_prediction(retriever, question),
        }
        for system, prediction in predictions.items():
            expected_abstention = question.expected_state is not ContextState.CURRENT
            predicted_abstention = not prediction.answer_ids
            outcomes.append(
                EvaluationOutcome(
                    system=system,
                    question_id=question.id,
                    expected_state=question.expected_state,
                    predicted_state=prediction.state,
                    expected_answer_ids=question.expected_answer_ids,
                    predicted_answer_ids=prediction.answer_ids,
                    citation_ids=prediction.citation_ids,
                    correct_state=prediction.state is question.expected_state,
                    correct_answer=prediction.answer_ids == question.expected_answer_ids,
                    stale_fact_used=prediction.stale_fact_used,
                    conflict_recognized=(
                        question.expected_state is not ContextState.DISPUTED
                        or prediction.state is ContextState.DISPUTED
                    ),
                    unsupported_answer=bool(prediction.answer_ids and not prediction.citation_ids),
                    abstention_correct=expected_abstention == predicted_abstention,
                )
            )
    metrics = tuple(_metrics(system, outcomes) for system in SystemName)
    by_system = {item.system: item for item in metrics}
    temporal = by_system[SystemName.TEMPORAL_PROVENANCE]
    baseline_best = max(
        by_system[SystemName.NAIVE_TEXT].state_accuracy,
        by_system[SystemName.CURRENT_KEYWORD].state_accuracy,
    )
    gate_reasons = []
    if temporal.state_accuracy < minimum_state_accuracy:
        gate_reasons.append(
            f"temporal state accuracy {temporal.state_accuracy:.3f} is below "
            f"{minimum_state_accuracy:.3f}"
        )
    if temporal.state_accuracy < baseline_best:
        gate_reasons.append("temporal retrieval does not outperform the strongest baseline")
    if temporal.unsupported_answer_rate > 0:
        gate_reasons.append("temporal retrieval emitted unsupported answers")
    return EvaluationReport(
        generated_at=datetime.now(UTC),
        package_version=__version__,
        corpus_version=corpus.corpus_version,
        question_count=len(corpus.questions()),
        systems=metrics,
        outcomes=tuple(outcomes),
        minimum_state_accuracy=minimum_state_accuracy,
        passed=not gate_reasons,
        gate_reasons=tuple(gate_reasons),
    )


def render_evaluation_markdown(report: EvaluationReport) -> str:
    lines = [
        "# Context Retrieval Evaluation",
        "",
        f"- Package: `{report.package_version}`",
        f"- Corpus: `{report.corpus_version}`",
        f"- Questions: {report.question_count}",
        f"- Gate: {'PASS' if report.passed else 'FAIL'}",
        "",
        "| System | State accuracy | Answer accuracy | Stale use | Unsupported | Abstention |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for metrics in report.systems:
        lines.append(
            f"| {metrics.system.value} | {metrics.state_accuracy:.3f} | "
            f"{metrics.answer_accuracy:.3f} | {metrics.stale_fact_use_rate:.3f} | "
            f"{metrics.unsupported_answer_rate:.3f} | "
            f"{metrics.correct_abstention_rate:.3f} |"
        )
    if report.gate_reasons:
        lines.extend(["", "## Gate failures", ""])
        lines.extend(f"- {reason}" for reason in report.gate_reasons)
    lines.extend(["", "## State breakdown", ""])
    for system in SystemName:
        system_outcomes = [item for item in report.outcomes if item.system is system]
        counts = {
            state.value: sum(item.predicted_state is state for item in system_outcomes)
            for state in ContextState
        }
        lines.append(f"- `{system.value}`: {counts}")
    return "\n".join(lines) + "\n"


def _question_contract(question: EvalQuestion) -> tuple[FactType, str | None]:
    mapping = {
        "owner_at_open": (FactType.OWNERSHIP, "owned_by"),
        "metric_at_open": (FactType.METRIC_NAME, "uses_metric"),
        "mitigation_after_close": (FactType.TEMPORARY_MITIGATION, "temporary_mitigation"),
        "undocumented_objective": (FactType.PROCEDURE, "recovery_objective"),
    }
    return mapping[question.kind]


def _matching_facts(facts: tuple[Fact, ...], question: EvalQuestion) -> tuple[Fact, ...]:
    fact_type, predicate = _question_contract(question)
    return tuple(
        fact
        for fact in facts
        if fact.service == question.service
        and fact.fact_type is fact_type
        and (predicate is None or fact.predicate == predicate)
        and (
            question.kind != "mitigation_after_close"
            or fact.incident_scope == question.incident_id
        )
        and fact.required_scopes <= question.access_scopes
    )


def _naive_prediction(facts: tuple[Fact, ...], question: EvalQuestion) -> _Prediction:
    candidates = _matching_facts(facts, question)
    if not candidates:
        state = (
            ContextState.INSUFFICIENT_EVIDENCE
            if _has_inaccessible_match(facts, question)
            else ContextState.UNKNOWN
        )
        return _Prediction(state=state)
    selected = max(candidates, key=lambda fact: (fact.recorded_at, fact.fact_id))
    return _Prediction(
        state=ContextState.CURRENT,
        answer_ids=_answer_ids(question, (selected,)),
        stale_fact_used=(
            not selected.was_valid_at(question.question_time)
            or not selected.was_knowable_at(question.question_time)
        ),
    )


def _current_prediction(facts: tuple[Fact, ...], question: EvalQuestion) -> _Prediction:
    candidates = tuple(
        fact
        for fact in _matching_facts(facts, question)
        if fact.was_knowable_at(question.question_time)
        and fact.was_valid_at(question.question_time)
    )
    if not candidates:
        state = (
            ContextState.INSUFFICIENT_EVIDENCE
            if _has_inaccessible_match(facts, question)
            else ContextState.UNKNOWN
        )
        return _Prediction(state=state)
    selected = max(candidates, key=lambda fact: (fact.recorded_at, fact.fact_id))
    return _Prediction(state=ContextState.CURRENT, answer_ids=_answer_ids(question, (selected,)))


def _temporal_prediction(retriever: ContextRetriever, question: EvalQuestion) -> _Prediction:
    fact_type, predicate = _question_contract(question)
    packet = retriever.retrieve(
        ContextQuery(
            service=question.service,
            question_time=question.question_time,
            fact_type=fact_type,
            predicate=predicate,
            incident=(
                question.incident_id if question.kind == "mitigation_after_close" else None
            ),
            access=AccessContext(
                principal="evaluation-runner",
                scopes=question.access_scopes,
            ),
        )
    )
    facts_by_id = {fact.fact_id: fact for fact in retriever.repository.list_facts()}
    selected = tuple(facts_by_id[item.fact_id] for item in packet.evidence)
    return _Prediction(
        state=packet.state,
        answer_ids=_answer_ids(question, selected),
        citation_ids=tuple(item.citation.source_key for item in packet.evidence),
        stale_fact_used=any(item.state is ContextState.STALE for item in packet.evidence),
    )


def _answer_ids(question: EvalQuestion, facts: tuple[Fact, ...]) -> tuple[str, ...]:
    prefix = {"owner_at_open": "owner", "metric_at_open": "metric"}.get(question.kind)
    if prefix is None:
        return ()
    return tuple(sorted(f"{prefix}:{fact.value}" for fact in facts))


def _has_inaccessible_match(facts: tuple[Fact, ...], question: EvalQuestion) -> bool:
    fact_type, predicate = _question_contract(question)
    return any(
        fact.service == question.service
        and fact.fact_type is fact_type
        and fact.predicate == predicate
        and not fact.required_scopes <= question.access_scopes
        for fact in facts
    )


def _metrics(system: SystemName, outcomes: list[EvaluationOutcome]) -> SystemMetrics:
    selected = [outcome for outcome in outcomes if outcome.system is system]
    total = len(selected)
    disputed = [
        outcome for outcome in selected if outcome.expected_state is ContextState.DISPUTED
    ]
    expected_answers = [outcome for outcome in selected if outcome.expected_answer_ids]
    cited_answers = sum(bool(outcome.citation_ids) for outcome in expected_answers)
    predicted_with_citations = sum(
        bool(outcome.predicted_answer_ids and outcome.citation_ids) for outcome in selected
    )
    return SystemMetrics(
        system=system,
        total=total,
        state_accuracy=_rate(sum(outcome.correct_state for outcome in selected), total),
        answer_accuracy=_rate(sum(outcome.correct_answer for outcome in selected), total),
        stale_fact_use_rate=_rate(sum(outcome.stale_fact_used for outcome in selected), total),
        conflict_recognition_rate=_rate(
            sum(outcome.conflict_recognized for outcome in disputed), len(disputed)
        ),
        unsupported_answer_rate=_rate(
            sum(outcome.unsupported_answer for outcome in selected), total
        ),
        correct_abstention_rate=_rate(
            sum(outcome.abstention_correct for outcome in selected), total
        ),
        citation_precision=_rate(predicted_with_citations, predicted_with_citations),
        citation_recall=_rate(cited_answers, len(expected_answers)),
    )


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0
