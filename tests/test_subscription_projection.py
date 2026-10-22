import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pcfs.identifiers import canonical_json, content_digest
from pcfs.models import (
    AccessContext,
    AuthorityLevel,
    Fact,
    FactRelationship,
    FactType,
    RelationshipType,
    Sensitivity,
    SourceClass,
    SourceVersion,
)
from pcfs.subscriptions.projection import (
    UPSTREAM_REVISION,
    ProjectionError,
    RustGraphBackend,
    TraversalBounds,
    build_projection,
    select_impact,
)

NOW = datetime(2026, 1, 10, tzinfo=UTC)
EARLIER = NOW - timedelta(days=1)
ROOT = Path(__file__).resolve().parents[1]


def fact(identity: str, **changes: object) -> Fact:
    source = SourceVersion(
        source_id=f"catalog/{identity}",
        version="v1",
        source_class=SourceClass.SERVICE_CATALOG,
        uri=f"fixture://{identity}",
        content_digest=content_digest(identity),
        observed_at=EARLIER,
        recorded_at=EARLIER,
    )
    return Fact.model_validate(
        {
            "fact_id": identity,
            "subject": "checkout",
            "predicate": identity,
            "value": identity,
            "fact_type": FactType.DEPENDENCY,
            "source": source,
            "observed_at": EARLIER,
            "valid_from": EARLIER,
            "recorded_at": EARLIER,
            "authority": AuthorityLevel.AUTHORITATIVE,
            "owner": "ops",
            "environment": "production",
            "service": "checkout",
            "freshness_policy_id": "dependency-v1",
            **changes,
        }
    )


def edge(prerequisite: str, dependent: str, **changes: object) -> FactRelationship:
    return FactRelationship.model_validate(
        {
            "source_fact_id": dependent,
            "target_fact_id": prerequisite,
            "relationship_type": RelationshipType.DEPENDS_ON,
            "source": fact("edge").source,
            "recorded_at": EARLIER,
            **changes,
        }
    )


def manifest(facts=None, edges=None, **changes):
    return build_projection(
        facts if facts is not None else (fact("a"), fact("b"), fact("c")),
        edges if edges is not None else (edge("a", "b"), edge("b", "c")),
        **{
            "seeds": frozenset({"a"}),
            "service": "checkout",
            "environment": "production",
            "question_time": NOW,
            "access": AccessContext(principal="reviewer"),
            **changes,
        },
    )


@pytest.fixture(scope="module")
def rust_backend():
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--offline",
            "--manifest-path",
            str(ROOT / "graph-adapter/Cargo.toml"),
        ],
        check=True,
        timeout=120,
        capture_output=True,
    )
    binary = ROOT / "graph-adapter/target/debug/pcfs-graph-adapter"
    return RustGraphBackend(
        binary_path=binary,
        expected_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    )


def test_graph_vs_flat_and_direction(rust_backend):
    projection = manifest()
    flat = select_impact(projection)
    graph = select_impact(projection, graph_enabled=True, backend=rust_backend)
    assert flat.nodes == ("a",)
    assert flat.backend == "flat"
    assert flat.upstream_fingerprint is None
    assert graph.nodes == ("a", "b", "c")
    assert graph.backend == "rust_opgraph"
    assert graph.truncated is False
    reverse = manifest(seeds=frozenset({"c"}))
    assert select_impact(reverse, graph_enabled=True, backend=rust_backend).nodes == ("c",)
    with pytest.raises(ProjectionError, match="explicit pinned Rust"):
        select_impact(projection, graph_enabled=True)


@pytest.mark.parametrize(
    "kind",
    [
        RelationshipType.DEPENDS_ON,
        RelationshipType.DERIVED_FROM,
        RelationshipType.SUPPORTS,
    ],
)
def test_normalizes_edges_without_losing_original_provenance(rust_backend, kind):
    relationship = edge("a", "b", relationship_type=kind)
    projection = manifest(edges=(relationship,))
    normalized = projection.projection.edges[0]
    assert normalized.class_ == "depends_on"
    assert (normalized.prerequisite, normalized.dependent) == ("a", "b")
    assert normalized.provenance.source_fact_id == "b"
    assert normalized.provenance.target_fact_id == "a"
    assert normalized.provenance.relationship_type == kind.value
    assert normalized.provenance.relationship_id == relationship.relationship_id
    assert normalized.provenance.source_key == relationship.source.key
    assert normalized.provenance.relationship_sha256 == content_digest(relationship)
    assert select_impact(projection, graph_enabled=True, backend=rust_backend).nodes == ("a", "b")


@pytest.mark.parametrize(
    ("hops", "cap", "expected", "truncated"),
    [
        (0, 10, ("a",), True),
        (1, 10, ("a", "b"), True),
        (8, 1, ("a",), True),
        (8, 2, ("a", "b"), True),
        (8, 3, ("a", "b", "c"), False),
    ],
)
def test_cycles_and_bounds(rust_backend, hops, cap, expected, truncated):
    projection = manifest(
        edges=(edge("a", "b"), edge("b", "c"), edge("c", "a")),
        bounds=TraversalBounds(max_hops=hops, max_nodes=cap),
    )
    result = select_impact(projection, graph_enabled=True, backend=rust_backend)
    assert result.nodes == expected
    assert result.truncated is truncated


def test_multiple_seeds_share_global_cap_and_empty_seeds(rust_backend):
    projection = manifest(seeds=frozenset({"a", "c"}), bounds=TraversalBounds(max_nodes=2))
    result = select_impact(projection, graph_enabled=True, backend=rust_backend)
    assert result.nodes == ("a", "c")
    assert result.truncated is True
    result = select_impact(manifest(seeds=frozenset()), graph_enabled=True, backend=rust_backend)
    assert result.nodes == ()
    assert result.truncated is False
    with pytest.raises(ProjectionError, match="seed bounds"):
        manifest(seeds=frozenset({"a", "c"}), bounds=TraversalBounds(max_nodes=1))


@pytest.mark.parametrize(
    "changes",
    [
        {"recorded_at": NOW + timedelta(microseconds=1)},
        {"observed_at": NOW + timedelta(microseconds=1)},
        {"valid_from": NOW + timedelta(microseconds=1)},
        {"valid_to": NOW},
        {"superseded_at": NOW},
        {"service": "billing"},
        {"environment": "staging"},
        {"sensitivity": Sensitivity.RESTRICTED, "required_scopes": frozenset({"secret"})},
    ],
)
def test_excluded_nodes_never_bridge_or_enter_manifests(rust_backend, changes):
    projection = manifest(facts=(fact("a"), fact("b", **changes), fact("c")))
    assert tuple(node.id for node in projection.projection.nodes) == ("a", "c")
    assert projection.projection.edges == ()
    result = select_impact(projection, graph_enabled=True, backend=rust_backend)
    assert result.nodes == ("a",)
    assert result.truncated is False


def test_revocation_and_incident_release_scope(rust_backend):
    restricted = fact("b", required_scopes={"secret"}, sensitivity=Sensitivity.RESTRICTED)
    allowed = manifest(
        facts=(fact("a"), restricted),
        edges=(edge("a", "b"),),
        access=AccessContext(principal="reviewer", scopes={"secret"}),
    )
    revoked = manifest(facts=(fact("a"), restricted), edges=(edge("a", "b"),))
    assert select_impact(allowed, graph_enabled=True, backend=rust_backend).nodes == ("a", "b")
    assert select_impact(revoked, graph_enabled=True, backend=rust_backend).nodes == ("a",)
    assert allowed.sha256 != revoked.sha256
    assert manifest(incident="inc", release="rel").projection.nodes == ()
    assert (
        manifest(access=AccessContext(principal="reviewer", environment="staging")).projection.nodes
        == ()
    )


def test_future_sources_edges_and_non_dependencies_are_omitted(rust_backend):
    future_source = fact("b").source.model_copy(update={"recorded_at": NOW + timedelta(days=1)})
    assert len(manifest(facts=(fact("a"), fact("b", source=future_source))).projection.nodes) == 1
    for relationship in (
        edge("a", "b", recorded_at=NOW + timedelta(microseconds=1)),
        edge("a", "b", source=future_source),
        edge("a", "b", relationship_type=RelationshipType.CONTRADICTS),
        edge("a", "b", relationship_type=RelationshipType.SUPERSEDES),
    ):
        projection = manifest(edges=(relationship,))
        assert projection.projection.edges == ()
        assert select_impact(projection, graph_enabled=True, backend=rust_backend).nodes == ("a",)
    projection = manifest(window_start=NOW)
    assert projection.projection.edges == ()


def test_manifest_is_canonical_and_sensitive_to_complete_provenance(rust_backend):
    first = manifest()
    reordered = manifest(
        facts=(fact("c"), fact("a"), fact("b")),
        edges=(edge("b", "c"), edge("a", "b")),
    )
    assert first.sha256 == reordered.sha256
    expected = hashlib.sha256(
        canonical_json(first.model_dump(mode="json", by_alias=True)).encode()
    ).hexdigest()
    assert first.sha256 == expected
    assert select_impact(first, graph_enabled=True, backend=rust_backend) == select_impact(
        reordered,
        graph_enabled=True,
        backend=rust_backend,
    )
    changed = manifest(facts=(fact("a"), fact("b", confidence=0.5), fact("c")))
    assert first.sha256 != changed.sha256
    assert first.sha256 != manifest(bounds=TraversalBounds(max_hops=1)).sha256
    with pytest.raises(ProjectionError, match="duplicate"):
        manifest(facts=(fact("a"), fact("a")))
    with pytest.raises(ProjectionError, match="duplicate"):
        manifest(edges=(edge("a", "b"), edge("a", "b")))


def fake_backend(tmp_path, body):
    path = tmp_path / "adapter"
    path.write_text(f"#!/usr/bin/python3\n{body}\n")
    path.chmod(0o700)
    return RustGraphBackend(
        binary_path=path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_binary_mismatch_and_flat_without_binary(tmp_path):
    backend = fake_backend(tmp_path, "raise RuntimeError('must not run')")
    backend = backend.model_copy(update={"expected_sha256": "0" * 64})
    assert select_impact(manifest(), backend=backend).nodes == ("a",)
    with pytest.raises(ProjectionError, match="SHA-256 mismatch"):
        select_impact(manifest(), graph_enabled=True, backend=backend)
    missing = backend.model_copy(update={"binary_path": tmp_path / "missing"})
    with pytest.raises(ProjectionError, match="unavailable"):
        missing.select(manifest().projection)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("import time; time.sleep(3)", "timeout"),
        ("import sys; sys.stdout.write('x' * 10000)", "byte limit"),
        ("import sys; sys.stderr.write('x' * 10000)", "byte limit"),
        ("print('not json')", "invalid adapter output"),
        ("raise SystemExit(2)", "exited unsuccessfully"),
    ],
)
def test_subprocess_is_bounded_and_fails_closed(tmp_path, body, message):
    backend = fake_backend(tmp_path, body).model_copy(
        update={"timeout_seconds": 0.3, "max_bytes": 4096}
    )
    with pytest.raises(ProjectionError, match=message):
        backend.select(manifest().projection)


@pytest.mark.parametrize(
    "updates",
    [
        {"nodes": ["a", "denied"]},
        {"nodes": ["a", "a"]},
        {"nodes": ["b"]},
        {"nodes": ["b", "a"]},
        {"truncated": "false"},
        {"upstream_revision": "other"},
        {"upstream_fingerprint": "invalid"},
        {"extra": True},
    ],
)
def test_validates_adapter_response(tmp_path, updates):
    response = {
        "schema_version": 1,
        "upstream_revision": UPSTREAM_REVISION,
        "nodes": ["a"],
        "truncated": False,
        "upstream_fingerprint": "0" * 16,
        **updates,
    }
    backend = fake_backend(tmp_path, f"print({json.dumps(response)!r})")
    with pytest.raises(ProjectionError):
        backend.select(manifest().projection)


def test_rejects_unreachable_output_and_oversized_request(tmp_path):
    response = {
        "schema_version": 1,
        "upstream_revision": UPSTREAM_REVISION,
        "nodes": ["a", "c"],
        "truncated": False,
        "upstream_fingerprint": "0" * 16,
    }
    backend = fake_backend(tmp_path, f"print({json.dumps(response)!r})")
    with pytest.raises(ProjectionError, match="unreachable"):
        backend.select(manifest(edges=()).projection)
    with pytest.raises(ProjectionError, match="request exceeds"):
        backend.model_copy(update={"max_bytes": 1}).select(manifest().projection)


def test_rust_contract_rejects_bad_requests_and_filters_time(rust_backend):
    request = manifest().projection.wire()
    for changes in (
        {"schema_version": 2},
        {"seeds": ["missing"]},
        {"bounds": {"max_hops": 65, "max_nodes": 10}},
        {"time_window": {"start": 2, "end": 1}},
        {"nodes": request["nodes"] * 2},
    ):
        result = subprocess.run(
            [str(rust_backend.binary_path)],
            input=json.dumps({**request, **changes}),
            text=True,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 2
        assert result.stdout == ""
    request["edges"][0]["observed_at"] = request["time_window"]["end"] + 1
    result = subprocess.run(
        [str(rust_backend.binary_path)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert json.loads(result.stdout)["nodes"] == ["a"]


def test_rust_sorts_raw_requests_and_applies_temporal_envelope(rust_backend):
    request = manifest().projection.wire()

    def invoke(payload):
        result = subprocess.run(
            [str(rust_backend.binary_path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )
        return json.loads(result.stdout)

    original = invoke(request)
    request["nodes"].reverse()
    request["edges"].reverse()
    assert invoke(request) == original
    first = next(edge for edge in request["edges"] if edge["prerequisite"] == "a")
    first["valid_until"] = request["time_window"]["end"]
    assert invoke(request)["nodes"] == ["a"]
    first["valid_until"] = None
    first["valid_from"] = request["time_window"]["end"] + 1
    assert invoke(request)["nodes"] == ["a"]
    first["valid_from"] = 0
    request["time_window"]["start"] = request["time_window"]["end"]
    assert invoke(request)["nodes"] == ["a"]


@pytest.mark.parametrize(
    ("nodes", "truncated", "message"),
    [
        (["a"], False, "incomplete"),
        (["a", "b", "c"], True, "inconsistent truncation"),
    ],
)
def test_rejects_false_completion_or_truncation(tmp_path, nodes, truncated, message):
    response = {
        "schema_version": 1,
        "upstream_revision": UPSTREAM_REVISION,
        "nodes": nodes,
        "truncated": truncated,
        "upstream_fingerprint": "0" * 16,
    }
    backend = fake_backend(tmp_path, f"print({json.dumps(response)!r})")
    with pytest.raises(ProjectionError, match=message):
        backend.select(manifest().projection)
