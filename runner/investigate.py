import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST_PATH = ROOT / "scenarios" / "manifest.yaml"
RESULTS_DIR = ROOT / "results"
EXCLUDED_FROM_ALL = {"injection_fixture"}
INGEST_WAIT_SECONDS = 60
BETWEEN_RUNS_SECONDS = 60  # one full rate-limit window, so the next run starts with an empty LLM_MAX_RPM budget


def load_manifest() -> dict[str, dict]:
    entries = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in entries}


def run_scenario_script(name: str, action: str) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scenarios" / f"{name}.py"), action],
        cwd=ROOT,
        check=True,
    )


def investigate(entry: dict) -> None:
    from agent.graph.graph import run_investigation
    from agent.rca import render_markdown

    name = entry["name"]
    rca = run_investigation(entry["incident_prompt"])
    markdown = render_markdown(rca)
    print(f"=== {name} ===")
    print(markdown)

    ground_truth = entry.get("ground_truth")
    out_dir = RESULTS_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.md").write_text(
        f"{markdown}\n\n---\n\n## Ground truth (not shown to the agent)\n\n{ground_truth}\n",
        encoding="utf-8",
    )
    record = {
        "scenario": name,
        "incident_prompt": entry["incident_prompt"],
        "ground_truth": ground_truth,
        "rca": json.loads(rca.model_dump_json()),
    }
    (out_dir / f"{name}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def run_all(manifest: dict[str, dict]) -> int:
    names = [name for name in manifest if name not in EXCLUDED_FROM_ALL]
    failed: set[str] = set()
    for index, name in enumerate(names):
        if index:
            print(f"Sleeping {BETWEEN_RUNS_SECONDS}s before next scenario (LLM_MAX_RPM)...", file=sys.stderr)
            time.sleep(BETWEEN_RUNS_SECONDS)
        revert_failed = False
        try:
            print(f"[{name}] applying", file=sys.stderr)
            run_scenario_script(name, "apply")
            print(f"[{name}] waiting {INGEST_WAIT_SECONDS}s for telemetry ingest", file=sys.stderr)
            time.sleep(INGEST_WAIT_SECONDS)
            investigate(manifest[name])
        except Exception as exc:
            print(f"[{name}] failed: {exc}", file=sys.stderr)
            failed.add(name)
        finally:
            try:
                print(f"[{name}] reverting", file=sys.stderr)
                run_scenario_script(name, "revert")
            except Exception as exc:
                print(f"[{name}] revert failed: {exc}", file=sys.stderr)
                failed.add(name)
                revert_failed = True
        if revert_failed:
            print("Aborting --all: cluster state is unknown after failed revert.", file=sys.stderr)
            break
    if failed:
        print(f"Failed scenarios: {', '.join(sorted(failed))}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the RCA agent against a scenario's incident prompt.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--scenario",
        help="Scenario name from scenarios/manifest.yaml. Investigates only; apply/revert the scenario yourself.",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="For every scenario except injection_fixture: apply, wait, investigate, revert, sequentially.",
    )
    args = parser.parse_args()

    manifest = load_manifest()
    if args.all:
        return run_all(manifest)

    entry = manifest.get(args.scenario)
    if entry is None:
        print(f"Unknown scenario '{args.scenario}'. Available: {', '.join(manifest)}", file=sys.stderr)
        return 2
    if not entry.get("incident_prompt"):
        print(f"Scenario '{args.scenario}' has no incident_prompt and cannot be investigated.", file=sys.stderr)
        return 2
    try:
        investigate(entry)
    except Exception as exc:
        print(f"Investigation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
