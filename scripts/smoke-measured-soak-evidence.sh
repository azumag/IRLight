#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/irlight-measured-soak-ci.XXXXXX")"
evidence_dir="$work_dir/evidence"
canonical_summary="$work_dir/canonical-summary.json"
diagnostic_dir="${MEASURED_SOAK_DIAGNOSTIC_DIR:-}"
trap 'rm -rf "$work_dir"' EXIT

preserve_failure_evidence() {
  if [[ -z "$diagnostic_dir" || ! -d "$evidence_dir" ]]; then
    return 0
  fi

  if ! install -d -m 0755 "$diagnostic_dir"; then
    echo "Could not create measured-soak diagnostic directory: $diagnostic_dir" >&2
    return 0
  fi

  # Keep the CI artifact deliberately narrow. These are the machine-readable
  # evidence files produced by this smoke; do not copy environment dumps,
  # Compose configuration, docker inspect output, or other secret-bearing data.
  local name
  for name in run-result.json summary.json samples.jsonl report.json; do
    if [[ -f "$evidence_dir/$name" ]]; then
      install -m 0644 "$evidence_dir/$name" "$diagnostic_dir/$name" || \
        echo "Could not preserve diagnostic evidence file: $name" >&2
    fi
  done
}

dump_failure_diagnostics() {
  echo "::group::Measured soak failure diagnostics" >&2

  if [[ -d "$evidence_dir" ]]; then
    echo "Evidence files:" >&2
    find "$evidence_dir" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort >&2 || true

    for name in run-result.json summary.json; do
      if [[ -f "$evidence_dir/$name" ]]; then
        echo "--- $name ---" >&2
        cat "$evidence_dir/$name" >&2 || true
      fi
    done

    if [[ -f "$evidence_dir/samples.jsonl" ]]; then
      echo "--- samples.jsonl (last 20 lines) ---" >&2
      tail -n 20 "$evidence_dir/samples.jsonl" >&2 || true
    fi
  else
    echo "Evidence directory was not created." >&2
  fi

  echo "--- Docker container state (names/status/images only) ---" >&2
  docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' >&2 || true
  echo "::endgroup::" >&2
}

fail_stage() {
  local stage="$1"
  local message="$2"
  local status="${3:-1}"
  printf '::error title=IRLight docker smoke failure::stage=%s status=%s\n' "$stage" "$status" >&2
  echo "$message" >&2
  dump_failure_diagnostics
  preserve_failure_evidence
  exit "$status"
}

# Keep this deliberately short: the purpose is to execute the complete evidence
# chain on the same Linux/Docker environment used by the integration suite, not
# to replace the multi-hour media-quality acceptance run from Issue #13.
set +e
python3 "$repo_root/scripts/run-measured-soak.py" \
  --output-dir "$evidence_dir" \
  --scenario "CI measured soak evidence-chain smoke" \
  --duration-seconds 8 \
  --interval-seconds 2 \
  --allow-unmeasured-media
runner_status=$?
set -e
if (( runner_status != 0 )); then
  fail_stage "measured_soak_runner" "measured soak evidence runner failed" "$runner_status"
fi

# Re-run the canonical validator outside the wrapper and compare its semantic
# summary with the persisted summary. This proves the caller-visible bundle is
# independently consumable after Compose cleanup, rather than only checking the
# wrapper's exit status.
if ! python3 "$repo_root/scripts/validate-soak-report.py" \
  "$evidence_dir/report.json" --json >"$canonical_summary"; then
  fail_stage "measured_soak_revalidation" "persisted soak report failed canonical revalidation"
fi

if ! python3 - "$evidence_dir" "$canonical_summary" <<'PY'
import json
import pathlib
import sys


def load_json(path: pathlib.Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


evidence_dir = pathlib.Path(sys.argv[1])
canonical_summary_path = pathlib.Path(sys.argv[2])
expected_files = {
    "samples.jsonl",
    "report.json",
    "summary.json",
    "run-result.json",
}
actual_files = {path.name for path in evidence_dir.iterdir() if path.is_file()}
if actual_files != expected_files:
    raise SystemExit(
        f"unexpected evidence bundle files: expected={sorted(expected_files)} actual={sorted(actual_files)}"
    )

result = load_json(evidence_dir / "run-result.json")
expected_result = {
    "schema_version": 1,
    "scenario": "CI measured soak evidence-chain smoke",
    "target_duration_seconds": 8,
    "sample_interval_seconds": 2,
    "media_mode": "diagnostic-unmeasured",
    "soak_exit_code": 0,
    "cleanup_verified": True,
    "samples_file": "samples.jsonl",
    "report_file": "report.json",
    "summary_file": "summary.json",
    "evidence_error": None,
}
for key, expected in expected_result.items():
    actual = result.get(key)
    if actual != expected:
        raise SystemExit(f"run-result mismatch for {key}: expected={expected!r} actual={actual!r}")
run_id = result.get("run_id")
if not isinstance(run_id, str) or not run_id:
    raise SystemExit("run-result is missing a non-empty run_id")

persisted_summary = load_json(evidence_dir / "summary.json")
canonical_summary = load_json(canonical_summary_path)
if persisted_summary != canonical_summary:
    raise SystemExit("persisted summary differs from independent canonical validation")

samples = []
with (evidence_dir / "samples.jsonl").open("r", encoding="utf-8") as handle:
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            raise SystemExit(f"samples.jsonl contains a blank line at {line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise SystemExit(f"sample {line_number} is not a JSON object")
        samples.append(value)

if len(samples) < 2:
    raise SystemExit(f"expected at least 2 samples, got {len(samples)}")
elapsed = [sample.get("elapsed_seconds") for sample in samples]
if elapsed[0] != 0:
    raise SystemExit(f"first sample is not the required zero baseline: {elapsed[0]!r}")
if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in elapsed):
    raise SystemExit(f"sample elapsed_seconds contains non-numeric values: {elapsed!r}")
if any(current < previous for previous, current in zip(elapsed, elapsed[1:])):
    raise SystemExit(f"sample elapsed_seconds is not monotonic: {elapsed!r}")
if elapsed[-1] < 8:
    raise SystemExit(f"final sample does not cover target duration: {elapsed[-1]!r}")
PY
then
  fail_stage "measured_soak_bundle" "measured soak evidence bundle contract failed"
fi

echo "Measured soak evidence-chain smoke passed."
