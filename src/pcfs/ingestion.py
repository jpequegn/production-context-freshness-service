"""Deterministic source parsers and corpus-to-fact conversion."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from pcfs.corpus import Corpus
from pcfs.identifiers import content_digest
from pcfs.models import (
    AuthorityLevel,
    Fact,
    FactRelationship,
    FactType,
    RelationshipType,
    ReviewState,
    Sensitivity,
    SourceClass,
    SourceVersion,
    StrictModel,
)


class SourceBundle(StrictModel):
    source: SourceVersion
    facts: tuple[Fact, ...] = ()
    relationships: tuple[FactRelationship, ...] = ()
    raw_document: dict[str, Any] = Field(default_factory=dict)


def parse_yaml_source(path: Path) -> SourceBundle:
    """Parse a structured YAML source envelope."""
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("source"), dict):
        raise ValueError("source document must contain a source mapping")
    return _bundle_from_mapping(raw, path.as_posix())


def parse_markdown_source(path: Path) -> SourceBundle:
    """Parse Markdown with a structured YAML front matter fact envelope."""
    text = path.read_text()
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("Markdown source requires YAML front matter")
    front_matter, _body = text[4:].split("\n---\n", maxsplit=1)
    raw = yaml.safe_load(front_matter)
    if not isinstance(raw, dict) or not isinstance(raw.get("source"), dict):
        raise ValueError("Markdown front matter must contain a source mapping")
    return _bundle_from_mapping(raw, path.as_posix())


def _bundle_from_mapping(raw: dict[str, Any], default_uri: str) -> SourceBundle:
    metadata = dict(raw["source"])
    metadata.setdefault("uri", default_uri)
    metadata["content_digest"] = content_digest(raw)
    source = SourceVersion.model_validate(metadata)
    facts = tuple(Fact.model_validate({**item, "source": source}) for item in raw.get("facts", []))
    relationships = tuple(
        FactRelationship.model_validate({**item, "source": source})
        for item in raw.get("relationships", [])
    )
    return SourceBundle(
        source=source,
        facts=facts,
        relationships=relationships,
        raw_document=raw,
    )


def build_corpus_bundles(corpus: Corpus) -> tuple[SourceBundle, ...]:
    """Convert the packaged synthetic corpus into immutable source-class bundles."""
    catalog_time = datetime(2026, 1, 1, tzinfo=UTC)
    catalog_payload = {"services": [service.model_dump(mode="json") for service in corpus.services]}
    catalog_source = _source(
        "synthetic/service-catalog",
        corpus.corpus_version,
        SourceClass.SERVICE_CATALOG,
        "fixture://corpus/services",
        catalog_payload,
        catalog_time,
    )
    catalog_facts: list[Fact] = []
    base_by_key: dict[tuple[str, str], Fact] = {}
    for service in corpus.services:
        values = [
            ("owned_by", service.owner, FactType.OWNERSHIP),
            ("has_slo", service.slo, FactType.SLO),
            ("uses_metric", service.metric, FactType.METRIC_NAME),
            ("runbook_procedure", service.procedure, FactType.PROCEDURE),
            ("feature_flag", service.flag, FactType.FLAG),
        ]
        values.extend(
            ("depends_on", dependency, FactType.DEPENDENCY)
            for dependency in service.dependencies
        )
        for predicate, value, fact_type in values:
            item = _fact(
                service=service.id,
                predicate=predicate,
                value=value,
                fact_type=fact_type,
                source=catalog_source,
                observed_at=catalog_time,
                valid_from=catalog_time,
                owner=service.owner,
                sensitivity=service.sensitivity,
            )
            catalog_facts.append(item)
            base_by_key[(service.id, predicate)] = item

    bundles = [
        SourceBundle(
            source=catalog_source,
            facts=tuple(catalog_facts),
            raw_document=catalog_payload,
        )
    ]
    for release in corpus.releases:
        payload = release.model_dump(mode="json")
        source = _source(
            f"synthetic/release/{release.id}",
            "1",
            SourceClass.DEPLOYMENT,
            f"fixture://corpus/releases/{release.id}",
            payload,
            release.deployed_at,
        )
        bundles.append(
            SourceBundle(
                source=source,
                facts=(
                    _fact(
                        service=release.service,
                        predicate="deployed_release",
                        value=release.id,
                        fact_type=FactType.RELEASE,
                        source=source,
                        observed_at=release.deployed_at,
                        valid_from=release.deployed_at,
                        owner="release-controller",
                    ),
                ),
                raw_document=payload,
            )
        )
    for incident in corpus.incidents:
        payload = incident.model_dump(mode="json")
        source = _source(
            f"synthetic/incident/{incident.id}",
            "1",
            SourceClass.INCIDENT,
            f"fixture://corpus/incidents/{incident.id}",
            payload,
            incident.opened_at,
        )
        bundles.append(
            SourceBundle(
                source=source,
                facts=(
                    _fact(
                        service=incident.service,
                        predicate="incident_observation",
                        value=incident.observation,
                        fact_type=FactType.INCIDENT_OBSERVATION,
                        source=source,
                        observed_at=incident.opened_at,
                        valid_from=incident.opened_at,
                        valid_to=incident.closed_at,
                        owner="incident-command",
                        incident_scope=incident.id,
                    ),
                    _fact(
                        service=incident.service,
                        predicate="temporary_mitigation",
                        value=incident.mitigation,
                        fact_type=FactType.TEMPORARY_MITIGATION,
                        source=source,
                        observed_at=incident.opened_at,
                        valid_from=incident.opened_at,
                        valid_to=incident.closed_at,
                        owner="incident-command",
                        incident_scope=incident.id,
                    ),
                ),
                raw_document=payload,
            )
        )
    for change in corpus.changes:
        payload = change.model_dump(mode="json")
        source = _source(
            f"synthetic/change/{change.id}",
            "1",
            SourceClass.SERVICE_CATALOG,
            f"fixture://corpus/changes/{change.id}",
            payload,
            change.occurred_at,
        )
        fact_type = (
            FactType.OWNERSHIP
            if "owner" in change.type or "conflict" in change.type
            else FactType.METRIC_NAME
        )
        predicate = "owned_by" if fact_type is FactType.OWNERSHIP else "uses_metric"
        changed_fact = _fact(
            service=change.service,
            predicate=predicate,
            value=change.new_value,
            fact_type=fact_type,
            source=source,
            observed_at=change.occurred_at,
            valid_from=change.occurred_at,
            owner="catalog-maintainers",
        )
        prior = base_by_key[(change.service, predicate)]
        relationship_type = (
            RelationshipType.CONTRADICTS
            if change.type == "authority_conflict"
            else RelationshipType.SUPERSEDES
        )
        relationship = FactRelationship(
            source_fact_id=changed_fact.fact_id,
            target_fact_id=prior.fact_id,
            relationship_type=relationship_type,
            source=source,
            recorded_at=change.occurred_at,
        )
        bundles.append(
            SourceBundle(
                source=source,
                facts=(changed_fact,),
                relationships=(relationship,),
                raw_document=payload,
            )
        )
        if relationship_type is RelationshipType.SUPERSEDES:
            base_by_key[(change.service, predicate)] = changed_fact
    return tuple(bundles)


def _source(
    source_id: str,
    version: str,
    source_class: SourceClass,
    uri: str,
    payload: Any,
    timestamp: datetime,
) -> SourceVersion:
    return SourceVersion(
        source_id=source_id,
        version=version,
        source_class=source_class,
        uri=uri,
        content_digest=content_digest(payload),
        observed_at=timestamp,
        recorded_at=timestamp,
    )


def _fact(
    *,
    service: str,
    predicate: str,
    value: Any,
    fact_type: FactType,
    source: SourceVersion,
    observed_at: datetime,
    valid_from: datetime,
    owner: str,
    valid_to: datetime | None = None,
    incident_scope: str | None = None,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
) -> Fact:
    required_scopes = (
        frozenset({f"service:{service}:restricted"})
        if sensitivity is Sensitivity.RESTRICTED
        else frozenset()
    )
    return Fact(
        subject=service,
        predicate=predicate,
        value=value,
        fact_type=fact_type,
        source=source,
        observed_at=observed_at,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=source.recorded_at,
        authority=AuthorityLevel.AUTHORITATIVE,
        owner=owner,
        review_state=ReviewState.REVIEWED,
        environment="production",
        service=service,
        incident_scope=incident_scope,
        sensitivity=sensitivity,
        required_scopes=required_scopes,
        freshness_policy_id=f"{fact_type.value}-v1",
    )
