#!/usr/bin/env python3
"""Prove installed CLI behavior and retain the exact local verification inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def sources(repo: Path) -> dict[str, str]:
    names = git(repo, "ls-files", "-z", "src/pcfs").split("\0")
    return {name: sha(repo / name) for name in names if name.endswith((".py", ".yaml"))}


def save(output: Path, report: dict) -> None:
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")


def check(report: dict, output: Path, name: str, condition: bool) -> None:
    report["checks"].append({"name": name, "passed": bool(condition)})
    save(output, report)
    if not condition:
        raise AssertionError(name)


def prove(args: argparse.Namespace, report: dict) -> None:
    import pcfs

    output, repo = args.output, args.repo
    package = Path(pcfs.__file__).resolve().parent
    check(
        report,
        output,
        "package loaded from clean production environment",
        package.is_relative_to(output / "prod")
        and not package.is_relative_to(repo)
        and Path(sys.prefix).resolve() == (output / "prod").resolve()
        and args.cli == output / "prod/bin/pcfs",
    )
    installed = {
        "src/pcfs/" + str(p.relative_to(package)): sha(p)
        for p in package.rglob("*")
        if p.suffix in {".py", ".yaml"}
    }
    report["installed_runtime"] = {"package": str(package), "sha256": installed}
    check(
        report,
        output,
        "all installed runtime Python and corpus files match tracked source",
        bool(installed) and installed == sources(repo) == report["source"]["runtime_sha256"],
    )
    distributions = {
        d.metadata["Name"].lower().replace("_", "-"): d.version
        for d in importlib.metadata.distributions()
    }
    report["production_distributions"] = distributions
    check(
        report,
        output,
        "production environment excludes development tools",
        not {"pytest", "ruff", "hypothesis", "pytest-cov"} & distributions.keys(),
    )

    import duckdb

    from pcfs.subscriptions.fixtures import demo_scenario, evaluation_scenarios, evaluation_truth

    def run(label: str, *words: str, rejected: bool = False) -> str:
        command = [str(args.cli), *map(str, words)]
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        result = subprocess.run(
            command, cwd=output / "work", env=env, text=True, capture_output=True, timeout=300
        )
        (output / "logs" / f"cli-{label}.log").write_text(result.stdout + result.stderr)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        report["commands"].append(
            {
                "label": label,
                "argv": command,
                "exit_code": result.returncode,
                "log": f"logs/cli-{label}.log",
            }
        )
        check(report, output, label, result.returncode == 2 if rejected else result.returncode == 0)
        return result.stdout

    def load(path: Path) -> dict:
        return json.loads(path.read_text())

    def state(db: Path, *, facts_only: bool = False) -> dict:
        with duckdb.connect(str(db), read_only=True) as connection:
            tables = [row[0] for row in connection.execute("SHOW TABLES").fetchall()]
            return {
                table: sorted(
                    row[0]
                    for row in connection.execute(
                        'SELECT to_json(t)::VARCHAR FROM "' + table.replace('"', '""') + '" AS t'
                    ).fetchall()
                )
                for table in tables
                if not facts_only or not table.startswith("subscription_")
            }

    legacy = output / "original-demo"
    run("original-demo", "demo", "--db", output / "original.duckdb", "--report-dir", legacy)
    original = load(legacy / "demo-summary.json")
    check(
        report,
        output,
        "original demo evaluation and dispute abstention",
        original["evaluation_passed"] is True
        and load(legacy / "evaluation.json")["passed"]
        and original["disputed_owner"]["abstained"] is True
        and bool(original["historical_owner"]["evidence"]),
    )

    db, flat_dir = output / "subscriptions.duckdb", output / "flat"
    demo_args = ("subscriptions", "demo", "--db", db, "--report-dir", flat_dir)
    run("subscription-demo", *demo_args)
    flat = load(flat_dir / "impact.json")
    ids = {packet["receipt"]["receipt_id"] for packet in flat["packets"]}
    check(
        report,
        output,
        "demo has three unique receipts, one outbox, four deliveries",
        len(flat["packets"]) == len(ids) == 3
        and len(flat["outbox"]) == 1
        and len(flat["deliveries"]) == 4
        and flat["deliveries"][1] == flat["deliveries"][3],
    )
    before = state(db)
    run("subscription-demo-rerun", *demo_args)
    check(
        report,
        output,
        "demo rerun has no persisted effects and stable report identities",
        before == state(db) and flat == load(flat_dir / "impact.json"),
    )
    at = (datetime.fromisoformat(flat["clock"]) + timedelta(minutes=5)).isoformat()
    identity = ("--db", db, "--at", at, "--principal", "synthetic-reviewer")
    denied = json.loads(
        run(
            "denied-inspect",
            "subscriptions",
            "inspect",
            "--db",
            db,
            "--at",
            at,
            "--principal",
            "unauthorized-reviewer",
        )
    )
    check(
        report,
        output,
        "denied principal sees no packets or outbox",
        denied["packets"] == [] and denied["outbox"] == [],
    )
    first = json.loads(run("dispatch", "subscriptions", "dispatch", *identity))
    dispatched = state(db)
    second = json.loads(run("dispatch-repeat", "subscriptions", "dispatch", *identity))
    check(
        report,
        output,
        "dispatch records exactly one delivery across repeats",
        len(first) == 1
        and first[0]["status"] == "delivered"
        and first[0]["attempts"] == 1
        and second == []
        and dispatched == state(db),
    )
    latest = flat["outbox"][0]["receipt_ids"][-1]
    facts = state(db, facts_only=True)
    stale = {
        "command_id": "local-proof-stale-review",
        "receipt_id": flat["outbox"][0]["receipt_ids"][0],
        "reviewer": "synthetic-reviewer",
        "decision": "dismissed",
        "rationale": "Earlier cause does not resolve later causes.",
        "at": at,
    }
    stale_file = output / "stale-review.json"
    stale_file.write_text(json.dumps(stale))
    run("stale-review", "subscriptions", "review", *identity, "--command-file", stale_file)
    with duckdb.connect(str(db), read_only=True) as connection:
        stale_result = json.loads(
            connection.execute(
                "SELECT result_json FROM subscription_commands WHERE command_id = ?",
                [stale["command_id"]],
            ).fetchone()[0]
        )
    check(
        report,
        output,
        "stale disposition is recorded without resolving later causes",
        stale_result["resolved"] is False and facts == state(db, facts_only=True),
    )
    command = {
        "command_id": "local-proof-review",
        "receipt_id": latest,
        "reviewer": "synthetic-reviewer",
        "decision": "dismissed",
        "rationale": "Synthetic local verification only.",
        "at": at,
    }
    command_file = output / "review.json"
    command_file.write_text(json.dumps(command))
    run("review", "subscriptions", "review", *identity, "--command-file", command_file)
    reviewed = state(db)
    run("review-repeat", "subscriptions", "review", *identity, "--command-file", command_file)
    view = json.loads(run("review-inspect", "subscriptions", "inspect", *identity))
    check(
        report,
        output,
        "latest-receipt disposition is idempotent and changes no fact state",
        reviewed == state(db)
        and facts == state(db, facts_only=True)
        and view["outbox"][0]["status"] == "resolved"
        and all(p["usable_for_synthesis"] is False for p in view["packets"]),
    )
    command["reviewer"] = "wrong-reviewer"
    command_file = output / "wrong-review.json"
    command_file.write_text(json.dumps(command))
    run(
        "wrong-review-command",
        "subscriptions",
        "review",
        *identity,
        "--command-file",
        command_file,
        rejected=True,
    )
    check(report, output, "rejected review changes no ledger state", reviewed == state(db))
    command["reviewer"] = "synthetic-reviewer"
    command["rationale"] = "Changed bytes must collide with the accepted command ID."
    command_file = output / "collision-review.json"
    command_file.write_text(json.dumps(command))
    run(
        "review-command-collision",
        "subscriptions",
        "review",
        *identity,
        "--command-file",
        command_file,
        rejected=True,
    )
    check(report, output, "command collision changes no ledger state", reviewed == state(db))

    scenario_file = output / "scenario.json"
    scenario_file.write_text(demo_scenario().model_dump_json())
    missing_dir = output / "missing-cache"
    run(
        "missing-cache",
        "subscriptions",
        "replay",
        "--db",
        output / "missing.duckdb",
        "--scenario-file",
        scenario_file,
        "--principal",
        "synthetic-reviewer",
        "--report-dir",
        missing_dir,
    )
    missing = load(missing_dir / "impact.json")
    check(
        report,
        output,
        "missing cache requests review without current synthesis claims",
        len(missing["packets"]) == 3
        and all(
            p["receipt"]["action"] == "review"
            and p["usable_for_synthesis"] is False
            and p["receipt"]["assessment"] is not None
            and all(
                p["receipt"]["assessment"][key] == "unknown"
                for key in ("relevance", "contradiction", "materiality")
            )
            for p in missing["packets"]
        ),
    )

    report["graph_adapter"] = {"path": str(args.adapter), "sha256": sha(args.adapter)}
    graph_dir = output / "graph"
    run(
        "graph-demo",
        "subscriptions",
        "demo",
        "--db",
        output / "graph.duckdb",
        "--report-dir",
        graph_dir,
        "--graph-adapter",
        args.adapter,
        "--adapter-sha256",
        report["graph_adapter"]["sha256"],
    )
    graph = load(graph_dir / "impact.json")

    def decisions(view: dict) -> list:
        return sorted(
            (
                p["receipt"]["event_id"],
                p["receipt"]["subscription_id"],
                p["receipt"]["action"],
                p["receipt"]["guard"]["mandatory"],
                sorted(p["receipt"]["guard"]["reasons"]),
                sorted(p["receipt"]["guard"]["affected_fact_ids"]),
            )
            for p in view["packets"]
        )

    check(
        report,
        output,
        "pinned graph demo matches flat direct-dependency decisions",
        decisions(graph) == decisions(flat)
        and len(graph["outbox"]) == 1
        and len(graph["deliveries"]) == 4
        and all(not p["receipt"]["guard"]["traversal_truncated"] for p in graph["packets"]),
    )
    check(
        report,
        output,
        "graph execution pins a different impact fingerprint",
        {p["receipt"]["guard"]["graph_fingerprint"] for p in graph["packets"]}
        != {p["receipt"]["guard"]["graph_fingerprint"] for p in flat["packets"]},
    )
    run(
        "graph-pin-rejected",
        "subscriptions",
        "demo",
        "--db",
        output / "bad-pin.duckdb",
        "--report-dir",
        output / "bad-pin",
        "--graph-adapter",
        args.adapter,
        "--adapter-sha256",
        "0" * 64,
        rejected=True,
    )

    help_text = run("subscription-help", "subscriptions", "--help")
    if "evaluate" not in help_text:
        report["evaluation"] = {"status": "unavailable", "reason": "CLI command not integrated"}
        save(output, report)
        return
    evaluation_dir = output / "evaluation"
    run(
        "subscription-evaluate",
        "subscriptions",
        "evaluate",
        "--report-dir",
        evaluation_dir,
        "--graph-adapter",
        args.adapter,
        "--adapter-sha256",
        report["graph_adapter"]["sha256"],
    )
    evaluation = load(evaluation_dir / "evaluation.json")
    comparison = evaluation["flat_vs_graph"]
    check(
        report,
        output,
        "evaluation executes the pinned matched flat/graph case",
        comparison["status"] in {"compared", "available"}
        and comparison["binary_sha256"] == report["graph_adapter"]["sha256"]
        and comparison["flat"]["subscription_ids"] == ["demo/restart"]
        and comparison["graph"]["subscription_ids"] == ["demo/latency", "demo/restart"]
        and comparison["parity"]["flat_reviews_retained"] is True
        and comparison["parity"]["graph_only_subscription_ids"] == ["demo/latency"]
        and comparison["parity"]["flat_only_subscription_ids"] == []
        and comparison["dependent_fact_id"] in comparison["graph"]["affected_fact_ids"],
    )
    truth = evaluation_truth()
    scenarios = evaluation_scenarios()
    expected = {
        (e.event_id, s.subscription_id)
        for case in scenarios
        for e in case.events
        for s in case.subscriptions
    }
    check(
        report,
        output,
        "fixture truth covers every unique event/subscription pair",
        set(truth) == expected and all(type(v) is bool for v in truth.values()),
    )
    baselines = {
        "periodic_full_refresh",
        "lexical_compare",
        "deterministic_dependencies",
        "semantic_subscription",
    }
    for baseline in sorted(baselines):
        rows = [
            row
            for case in evaluation["scenarios"]
            for row in case["baselines"][baseline]["decisions"]
        ]
        pairs = [(row["event_id"], row["subscription_id"]) for row in rows]
        check(
            report,
            output,
            f"{baseline} report truth coverage and unique scoring",
            set(pairs) == expected
            and len(pairs) == len(expected)
            and all(
                row["material_change"] is truth[pair] for row, pair in zip(rows, pairs, strict=True)
            ),
        )
    check(
        report,
        output,
        "evaluation retains synthetic limits and both split reports",
        evaluation["conclusion"]["proven_benefit"] is False
        and set(evaluation["splits"]) == {"dev", "heldout"}
        and (evaluation_dir / "evaluation.md").stat().st_size > 0
        and all(
            case["baselines"][b]["cost_usd"] is None
            and case["baselines"][b]["human_minutes"] is None
            for case in evaluation["scenarios"]
            for b in baselines
        ),
    )
    report["evaluation"] = {
        "status": "passed",
        "truth_pairs": len(expected),
        "flat_vs_graph": comparison["status"],
    }
    save(output, report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("init", "record", "prove", "finish"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--exit-code", type=int, default=0)
    args, command = parser.parse_known_args()
    args.repo, args.output = args.repo.resolve(), args.output.resolve()
    if args.cli:
        args.cli = args.cli.resolve()
    if args.adapter:
        args.adapter = args.adapter.resolve()
    if args.mode == "init":
        report = {
            "schema_version": 1,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "source": {
                "repo": str(args.repo),
                "commit": git(args.repo, "rev-parse", "HEAD"),
                "head_tree": git(args.repo, "rev-parse", "HEAD^{tree}"),
                "git_status": git(args.repo, "status", "--porcelain=v1"),
                "runtime_sha256": sources(args.repo),
                "input_sha256": {
                    p: sha(args.repo / p)
                    for p in (
                        "pyproject.toml",
                        "uv.lock",
                        "graph-adapter/Cargo.toml",
                        "graph-adapter/Cargo.lock",
                        "graph-adapter/src/main.rs",
                        "scripts/prove-subscriptions.py",
                        "scripts/verify-subscriptions-local.sh",
                    )
                },
            },
            "offline": os.environ.get("UV_OFFLINE") == "true",
            "commands": [],
            "checks": [],
        }
        with (args.output / "verification.json").open("x") as stream:
            stream.write(json.dumps(report, indent=2) + "\n")
        return
    report = json.loads((args.output / "verification.json").read_text())
    if args.mode == "record":
        report["commands"].append(
            {
                "label": args.label,
                "argv": command[1:],
                "exit_code": args.exit_code,
                "log": f"logs/{args.label}.log",
            }
        )
    elif args.mode == "prove":
        prove(args, report)
    else:
        unchanged = report["source"]["runtime_sha256"] == sources(args.repo) and all(
            sha(args.repo / p) == digest for p, digest in report["source"]["input_sha256"].items()
        )
        report["source_unchanged"] = unchanged
        report["status"] = "passed" if args.exit_code == 0 and unchanged else "failed"
        report["exit_code"] = args.exit_code if unchanged else 1
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["artifact_sha256"] = {
            str(p.relative_to(args.output)): sha(p) for p in (args.output / "dist").glob("*.whl")
        }
    save(args.output, report)
    if args.mode == "finish" and report["status"] == "failed":
        raise SystemExit(report["exit_code"])


if __name__ == "__main__":
    main()
