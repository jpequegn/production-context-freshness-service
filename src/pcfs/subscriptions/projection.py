"""Authorized, point-in-time dependency selection; never a policy decision."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from pcfs.identifiers import canonical_json, content_digest
from pcfs.models import (
    EPOCH,
    AccessContext,
    Fact,
    FactRelationship,
    FactType,
    RelationshipType,
    Sensitivity,
    StrictModel,
)

UPSTREAM_REVISION = "9194281ab2a387e820f8047de512a3b8763cc7e0"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
NodeClass = Literal["evidence", "runbook", "metric", "slo", "release", "config"]
DependencyType = Literal["depends_on", "derived_from", "supports"]
NODE_CLASSES: dict[FactType, NodeClass] = {
    FactType.PROCEDURE: "runbook",
    FactType.METRIC_NAME: "metric",
    FactType.SLO: "slo",
    FactType.RELEASE: "release",
    FactType.FLAG: "config",
}


class ProjectionError(ValueError):
    """The graph boundary failed closed."""


class ProjectionNode(StrictModel):
    id: str
    class_: NodeClass = Field(alias="class")
    sensitivity: Sensitivity
    source_key: str
    fact_sha256: str


class EdgeProvenance(StrictModel):
    relationship_id: str
    source_fact_id: str
    target_fact_id: str
    relationship_type: DependencyType
    source_key: str
    relationship_sha256: str


class ProjectionEdge(StrictModel):
    prerequisite: str
    dependent: str
    class_: Literal["depends_on"] = Field(default="depends_on", alias="class")
    observed_at: int
    valid_from: int
    valid_until: int | None = None
    provenance: EdgeProvenance


class TimeWindow(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> TimeWindow:
        if self.start > self.end:
            raise ValueError("time window start must not exceed end")
        return self


class TraversalBounds(StrictModel):
    max_hops: int = Field(default=8, ge=0, le=64, strict=True)
    max_nodes: int = Field(default=1000, ge=1, le=10_000, strict=True)


class GraphProjection(StrictModel):
    schema_version: Literal[1] = 1
    nodes: tuple[ProjectionNode, ...]
    edges: tuple[ProjectionEdge, ...]
    time_window: TimeWindow
    bounds: TraversalBounds
    seeds: tuple[str, ...]

    def wire(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)


class ProjectionManifest(StrictModel):
    schema_version: Literal[1] = 1
    upstream_revision: Literal[UPSTREAM_REVISION] = UPSTREAM_REVISION
    direction: Literal["prerequisite_to_dependent"] = "prerequisite_to_dependent"
    service: str
    environment: str
    incident: str | None
    release: str | None
    principal: str
    access_environment: str | None
    access_scopes: tuple[str, ...]
    projection: GraphProjection

    @property
    def sha256(self) -> str:
        return content_digest(self.model_dump(mode="json", by_alias=True))


class ProjectionResult(StrictModel):
    backend: Literal["flat", "rust_opgraph"]
    nodes: tuple[str, ...]
    truncated: bool
    manifest_sha256: str
    upstream_fingerprint: str | None = None


class AdapterOutput(StrictModel):
    schema_version: Literal[1]
    upstream_revision: Literal[UPSTREAM_REVISION]
    nodes: tuple[str, ...]
    truncated: bool = Field(strict=True)
    upstream_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionError("projection times must be timezone-aware")
    delta = value.astimezone(UTC) - EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros < 0:
        raise ProjectionError("projection times must be on or after the Unix epoch")
    return micros


def build_projection(
    facts: tuple[Fact, ...],
    relationships: tuple[FactRelationship, ...],
    *,
    seeds: frozenset[str],
    service: str,
    environment: str,
    question_time: datetime,
    access: AccessContext,
    incident: str | None = None,
    release: str | None = None,
    window_start: datetime = EPOCH,
    bounds: TraversalBounds | None = None,
) -> ProjectionManifest:
    window = TimeWindow(start=_timestamp(window_start), end=_timestamp(question_time))
    bounds = bounds or TraversalBounds()
    eligible: dict[str, Fact] = {}
    for fact in facts:
        if not (
            fact.service == service
            and fact.environment == environment
            and (incident is None or fact.incident_scope == incident)
            and (release is None or fact.release_scope == release)
            and (access.environment is None or access.environment == fact.environment)
            and fact.required_scopes <= access.scopes
            and fact.was_knowable_at(question_time)
            and fact.was_valid_at(question_time)
            and fact.observed_at <= question_time
            and fact.source.recorded_at <= question_time
            and fact.source.observed_at <= question_time
        ):
            continue
        if not fact.fact_id or fact.fact_id in eligible:
            raise ProjectionError("missing or duplicate eligible fact identity")
        eligible[fact.fact_id] = fact
    nodes = tuple(
        ProjectionNode(
            id=fact_id,
            **{"class": NODE_CLASSES.get(fact.fact_type, "evidence")},
            sensitivity=fact.sensitivity,
            source_key=fact.source.key,
            fact_sha256=content_digest(fact),
        )
        for fact_id, fact in sorted(eligible.items())
    )
    edges: list[ProjectionEdge] = []
    relationship_ids: set[str] = set()
    traversable = {
        RelationshipType.DEPENDS_ON,
        RelationshipType.DERIVED_FROM,
        RelationshipType.SUPPORTS,
    }
    for relationship in relationships:
        if not (
            relationship.relationship_type in traversable
            and relationship.source_fact_id in eligible
            and relationship.target_fact_id in eligible
            and window_start <= relationship.recorded_at <= question_time
            and relationship.source.recorded_at <= question_time
            and relationship.source.observed_at <= question_time
        ):
            continue
        if not relationship.relationship_id or relationship.relationship_id in relationship_ids:
            raise ProjectionError("missing or duplicate eligible relationship identity")
        relationship_ids.add(relationship.relationship_id)
        dependent = eligible[relationship.source_fact_id]
        prerequisite = eligible[relationship.target_fact_id]
        ends = [fact.valid_to for fact in (dependent, prerequisite) if fact.valid_to is not None]
        edges.append(
            ProjectionEdge(
                prerequisite=relationship.target_fact_id,
                dependent=relationship.source_fact_id,
                observed_at=_timestamp(relationship.recorded_at),
                valid_from=_timestamp(max(dependent.valid_from, prerequisite.valid_from)),
                valid_until=_timestamp(min(ends)) if ends else None,
                provenance=EdgeProvenance(
                    relationship_id=relationship.relationship_id,
                    source_fact_id=relationship.source_fact_id,
                    target_fact_id=relationship.target_fact_id,
                    relationship_type=relationship.relationship_type.value,
                    source_key=relationship.source.key,
                    relationship_sha256=content_digest(relationship),
                ),
            )
        )
    selected_seeds = tuple(sorted(seeds & eligible.keys()))
    if len(nodes) > 10_000 or len(edges) > 50_000 or len(selected_seeds) > bounds.max_nodes:
        raise ProjectionError("projection exceeds input or seed bounds")
    return ProjectionManifest(
        service=service,
        environment=environment,
        incident=incident,
        release=release,
        principal=access.principal,
        access_environment=access.environment,
        access_scopes=tuple(sorted(access.scopes)),
        projection=GraphProjection(
            nodes=nodes,
            edges=tuple(
                sorted(
                    edges, key=lambda e: (e.prerequisite, e.dependent, e.provenance.relationship_id)
                )
            ),
            time_window=window,
            bounds=bounds,
            seeds=selected_seeds,
        ),
    )


class RustGraphBackend(StrictModel):
    binary_path: Path
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_seconds: float = Field(default=5.0, gt=0, le=60, allow_inf_nan=False)
    max_bytes: int = Field(default=MAX_REQUEST_BYTES, ge=1, le=MAX_REQUEST_BYTES, strict=True)

    def select(self, projection: GraphProjection) -> AdapterOutput:
        path = self.binary_path
        if not path.is_absolute():
            raise ProjectionError("adapter binary path must be absolute")
        try:
            path = path.resolve(strict=True)
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024 * 1024:
                raise ProjectionError("adapter binary must be a regular file under 64 MiB")
            with path.open("rb") as binary:
                digest = hashlib.file_digest(binary, "sha256").hexdigest()
        except OSError as error:
            raise ProjectionError("adapter binary is unavailable") from error
        if digest != self.expected_sha256:
            raise ProjectionError("adapter binary SHA-256 mismatch")
        payload = canonical_json(projection.wire()).encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ProjectionError("adapter request exceeds byte limit")
        raw = self._invoke(path, payload)
        try:
            output = AdapterOutput.model_validate(json.loads(raw))
        except ValueError as error:
            raise ProjectionError("invalid adapter output") from error
        ids = set(output.nodes)
        allowed = {node.id for node in projection.nodes}
        if (
            output.nodes != tuple(sorted(ids))
            or not ids <= allowed
            or not set(projection.seeds) <= ids
            or len(ids) > projection.bounds.max_nodes
        ):
            raise ProjectionError("adapter returned invalid node selection")
        reachable = set(projection.seeds)
        for _ in range(projection.bounds.max_hops):
            reachable |= {
                edge.dependent
                for edge in projection.edges
                if edge.prerequisite in reachable
                and projection.time_window.start <= edge.observed_at <= projection.time_window.end
                and edge.valid_from <= projection.time_window.end
                and (edge.valid_until is None or projection.time_window.end < edge.valid_until)
            }
        if not ids <= reachable:
            raise ProjectionError("adapter returned unreachable nodes")
        if len(ids) != min(len(reachable), projection.bounds.max_nodes):
            raise ProjectionError("adapter returned incomplete node selection")
        crosses_boundary = any(
            edge.prerequisite in ids
            and edge.dependent not in ids
            and projection.time_window.start <= edge.observed_at <= projection.time_window.end
            and edge.valid_from <= projection.time_window.end
            and (edge.valid_until is None or projection.time_window.end < edge.valid_until)
            for edge in projection.edges
        )
        if output.truncated != crosses_boundary:
            raise ProjectionError("adapter returned inconsistent truncation")
        return output

    def _invoke(self, path: Path, payload: bytes) -> bytes:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            process = subprocess.Popen(
                [str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                env={},
            )
        except OSError as error:
            raise ProjectionError("adapter could not start") from error
        output = bytearray()
        total = 0
        offset = 0
        try:
            with selectors.DefaultSelector() as selector:
                for stream, event in (
                    (process.stdin, selectors.EVENT_WRITE),
                    (process.stdout, selectors.EVENT_READ),
                    (process.stderr, selectors.EVENT_READ),
                ):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, event)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProjectionError("adapter timeout")
                    for key, _ in selector.select(remaining):
                        stream = key.fileobj
                        if stream is process.stdin:
                            try:
                                offset += os.write(
                                    stream.fileno(), payload[offset : offset + 65536]
                                )
                            except BrokenPipeError:
                                offset = len(payload)
                            if offset == len(payload):
                                selector.unregister(stream)
                                stream.close()
                        else:
                            chunk = os.read(stream.fileno(), 65536)
                            if not chunk:
                                selector.unregister(stream)
                                continue
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise ProjectionError("adapter output exceeds byte limit")
                            if stream is process.stdout:
                                output.extend(chunk)
                try:
                    code = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    raise ProjectionError("adapter timeout") from error
                if code:
                    raise ProjectionError("adapter exited unsuccessfully")
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        return bytes(output)


def select_impact(
    manifest: ProjectionManifest,
    *,
    graph_enabled: bool = False,
    backend: RustGraphBackend | None = None,
) -> ProjectionResult:
    """Flat mode selects explicit seeds; graph mode adds bounded dependents."""
    if not graph_enabled:
        return ProjectionResult(
            backend="flat",
            nodes=manifest.projection.seeds,
            truncated=False,
            manifest_sha256=manifest.sha256,
        )
    if backend is None:
        raise ProjectionError("graph mode requires an explicit pinned Rust backend")
    output = backend.select(manifest.projection)
    return ProjectionResult(
        backend="rust_opgraph",
        nodes=output.nodes,
        truncated=output.truncated,
        manifest_sha256=manifest.sha256,
        upstream_fingerprint=output.upstream_fingerprint,
    )
