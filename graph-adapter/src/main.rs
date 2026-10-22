use org_core::graph::{
    EdgeClass, EdgeData, Envelope, Node, NodeClass, OpGraph, Sensitivity, TrustState,
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::io::{self, Read};

const MAX_BYTES: u64 = 4 * 1024 * 1024;
const REVISION: &str = "9194281ab2a387e820f8047de512a3b8763cc7e0";

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    schema_version: u8,
    nodes: Vec<InputNode>,
    edges: Vec<InputEdge>,
    time_window: TimeWindow,
    bounds: Bounds,
    seeds: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InputNode {
    id: String,
    class: NodeClass,
    sensitivity: Sensitivity,
    source_key: String,
    fact_sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InputEdge {
    prerequisite: String,
    dependent: String,
    class: DependencyClass,
    observed_at: u64,
    valid_from: u64,
    valid_until: Option<u64>,
    provenance: Provenance,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum DependencyClass {
    DependsOn,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Provenance {
    relationship_id: String,
    source_fact_id: String,
    target_fact_id: String,
    relationship_type: OriginalClass,
    source_key: String,
    relationship_sha256: String,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum OriginalClass {
    DependsOn,
    DerivedFrom,
    Supports,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TimeWindow {
    start: u64,
    end: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Bounds {
    max_hops: usize,
    max_nodes: usize,
}

#[derive(Serialize)]
struct Output {
    schema_version: u8,
    upstream_revision: &'static str,
    nodes: Vec<String>,
    truncated: bool,
    upstream_fingerprint: String,
}

fn project(mut input: Input) -> Result<Output, String> {
    if input.schema_version != 1
        || input.time_window.start > input.time_window.end
        || input.bounds.max_hops > 64
        || !(1..=10_000).contains(&input.bounds.max_nodes)
        || input.nodes.len() > 10_000
        || input.edges.len() > 50_000
        || input.seeds.len() > 10_000
    {
        return Err("invalid schema, time window, or bounds".into());
    }
    input.nodes.sort_by(|a, b| a.id.cmp(&b.id));
    let mut ids = BTreeSet::new();
    let mut graph = OpGraph::new();
    for node in input.nodes {
        if node.id.is_empty()
            || node.source_key.is_empty()
            || !is_sha256(&node.fact_sha256)
            || !ids.insert(node.id.clone())
        {
            return Err("invalid or duplicate node".into());
        }
        graph.add_node(Node {
            label: node.id.clone(),
            id: node.id,
            class: node.class,
            sensitivity: node.sensitivity,
        });
    }
    input.edges.sort_by(|a, b| {
        (&a.prerequisite, &a.dependent, &a.provenance.relationship_id).cmp(&(
            &b.prerequisite,
            &b.dependent,
            &b.provenance.relationship_id,
        ))
    });
    let mut relationships = BTreeSet::new();
    for edge in &input.edges {
        if !ids.contains(&edge.prerequisite)
            || !ids.contains(&edge.dependent)
            || edge.prerequisite == edge.dependent
            || edge.prerequisite != edge.provenance.target_fact_id
            || edge.dependent != edge.provenance.source_fact_id
            || edge.provenance.relationship_id.is_empty()
            || edge.provenance.source_key.is_empty()
            || !is_sha256(&edge.provenance.relationship_sha256)
            || !relationships.insert(&edge.provenance.relationship_id)
            || edge.valid_until.is_some_and(|end| end <= edge.valid_from)
        {
            return Err("invalid edge endpoints, interval, or provenance".into());
        }
        let DependencyClass::DependsOn = edge.class;
        if edge.observed_at < input.time_window.start {
            continue;
        }
        graph.add_edge(
            &edge.prerequisite,
            &edge.dependent,
            EdgeData {
                class: EdgeClass::DependsOn,
                confidence: 1.0,
                observed_at: edge.observed_at,
                valid_from: edge.valid_from.max(input.time_window.start),
                valid_until: edge.valid_until,
                trust: TrustState::Verified,
                source: serde_json::to_string(&edge.provenance).map_err(|e| e.to_string())?,
            },
        );
    }
    let seeds: BTreeSet<_> = input.seeds.into_iter().collect();
    if !seeds.is_subset(&ids) || seeds.len() > input.bounds.max_nodes {
        return Err("unknown seeds or seeds exceed node bound".into());
    }
    let envelope = Envelope {
        max_hops: input.bounds.max_hops,
        max_nodes: input.bounds.max_nodes,
        now: input.time_window.end,
        allowed_edge_classes: vec![EdgeClass::DependsOn],
        max_trust: TrustState::Verified,
    };
    let mut selected = seeds.clone();
    let mut omitted = false;
    for seed in &seeds {
        for id in graph.bounded_khop(seed, &envelope) {
            if selected.contains(&id) {
                continue;
            }
            if selected.len() < input.bounds.max_nodes {
                selected.insert(id);
            } else {
                omitted = true;
            }
        }
    }
    let truncated = omitted
        || input.edges.iter().any(|edge| {
            selected.contains(&edge.prerequisite)
                && !selected.contains(&edge.dependent)
                && input.time_window.start <= edge.observed_at
                && edge.observed_at <= envelope.now
                && edge.valid_from <= envelope.now
                && edge.valid_until.is_none_or(|end| envelope.now < end)
        });
    Ok(Output {
        schema_version: 1,
        upstream_revision: REVISION,
        nodes: selected.into_iter().collect(),
        truncated,
        upstream_fingerprint: format!("{:016x}", graph.snapshot_fingerprint()),
    })
}

fn run() -> Result<(), String> {
    let mut bytes = Vec::new();
    io::stdin()
        .take(MAX_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_BYTES {
        return Err("input exceeds byte limit".into());
    }
    let input = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    let output = project(input)?;
    println!(
        "{}",
        serde_json::to_string(&output).map_err(|e| e.to_string())?
    );
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("graph adapter: {error}");
        std::process::exit(2);
    }
}
