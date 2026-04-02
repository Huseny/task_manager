#!/usr/bin/env python3
"""
trajectory_validator.py
QA tool to detect merged, spliced, or tampered trajectory.json sessions.
Usage: python trajectory_validator.py trajectory.json
       python trajectory_validator.py --batch ./submissions/
"""

import re
import json, sys, os, argparse
from datetime import datetime
from collections import Counter
from pathlib import Path

RED = "\033[91m"
YEL = "\033[93m"
GRN = "\033[92m"
DIM = "\033[2m"
BOLD = "\033[1m"
RST = "\033[0m"

WEIGHTS = {"fail": 25, "warn": 8, "pass": 0}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_structure(data):
    has_msg = isinstance(data.get("messages"), list)
    has_meta = isinstance(data.get("meta"), dict) and "session_meta" in data["meta"]
    has_ctx = isinstance(data.get("meta", {}).get("turn_contexts"), list)
    if has_msg and has_meta:
        return (
            "pass",
            f"Valid structure. messages[{len(data['messages'])}], meta present, turn_contexts={'yes' if has_ctx else 'missing'}",
        )
    missing = []
    if not has_msg:
        missing.append("messages[]")
    if not has_meta:
        missing.append("meta.session_meta")
    return "fail", f"Missing required fields: {', '.join(missing)}"


def check_session_id(data):
    sid = data.get("meta", {}).get("session_meta", {}).get("id")
    if sid:
        return "pass", f"Session ID: {sid}"
    return "warn", "No session ID — metadata may have been stripped or manually created"


def check_timestamps(data):
    ctxs = data.get("meta", {}).get("turn_contexts", [])
    if len(ctxs) < 2:
        return "pass", "Not enough turn_contexts for temporal analysis"
    times = []
    for c in ctxs:
        ts = c.get("_timestamp")
        if ts:
            try:
                times.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except:
                pass
    if len(times) < 2:
        return "warn", "Insufficient parseable timestamps"
    flags = []
    for i in range(1, len(times)):
        delta = (times[i] - times[i - 1]).total_seconds()
        if delta < 0:
            flags.append(f"Turn {i}: BACKWARD by {abs(int(delta))}s")
        elif delta > 14400:
            flags.append(f"Turn {i}: gap of {delta/3600:.1f}h (suspicious)")
    if not flags:
        return "pass", f"All {len(times)} timestamps are chronologically valid"
    severity = "fail" if any("BACKWARD" in f for f in flags) else "warn"
    return severity, " | ".join(flags)


def check_models(data):
    ctxs = data.get("meta", {}).get("turn_contexts", [])
    models = list(dict.fromkeys(c.get("model") for c in ctxs if c.get("model")))
    if not models:
        return "warn", "No model information found in turn_contexts"
    if len(models) == 1:
        return "pass", f"Consistent model: {models[0]}"
    if len(models) == 2:
        return (
            "warn",
            f"2 models detected: {', '.join(models)} — acceptable if intentional",
        )
    return (
        "fail",
        f"{len(models)} models: {', '.join(models)} — strong indicator of session splice",
    )


def check_cwd(data):
    ctxs = data.get("meta", {}).get("turn_contexts", [])
    cwds = list(dict.fromkeys(c.get("cwd") for c in ctxs if c.get("cwd")))
    if not cwds:
        return "warn", "No working directory info found"
    if len(cwds) == 1:
        return "pass", f"Consistent cwd: {cwds[0]}"
    return (
        "fail" if len(cwds) > 2 else "warn"
    ), f"{len(cwds)} different dirs detected: {' -> '.join(cwds)}"


def check_role_sequence(data):
    msgs = data.get("messages", [])
    flags = []
    for i in range(1, len(msgs)):
        p, c = msgs[i - 1].get("role"), msgs[i].get("role")
        if p == c and c != "tool":
            flags.append(f"Consecutive '{c}' at index {i}")
    if not flags:
        return "pass", f"Role sequence valid across {len(msgs)} messages"
    return "warn", f"{len(flags)} anomaly(s): {'; '.join(flags[:3])}"


def check_tool_ids(data):
    seen, dupes = set(), []
    for m in data.get("messages", []):
        for tc in m.get("tool_calls", []):
            tid = tc.get("id")
            if tid:
                if tid in seen:
                    dupes.append(tid)
                else:
                    seen.add(tid)
    if not dupes:
        return "pass", f"All {len(seen)} tool call IDs are unique"
    return (
        "fail",
        f"{len(dupes)} duplicate tool_call_id(s): {', '.join(dupes[:3])} — strong merge indicator",
    )


def check_approval_policy(data):
    ctxs = data.get("meta", {}).get("turn_contexts", [])
    policies = list(
        dict.fromkeys(
            c.get("approval_policy") for c in ctxs if c.get("approval_policy")
        )
    )
    if not policies:
        return "warn", "No approval policy found"
    if len(policies) == 1:
        return "pass", f"Consistent policy: {policies[0]}"
    return "warn", f"Policy changed across turns: {', '.join(policies)}"


def check_originator(data):
    sm = data.get("meta", {}).get("session_meta", {})
    orig = sm.get("originator")
    src = sm.get("source")
    cli = sm.get("cli_version")
    if orig:
        return (
            "pass",
            f"Originator: {orig} | source: {src or 'N/A'} | cli_version: {cli or 'N/A'}",
        )
    return "warn", "No originator info — metadata may have been stripped"


def check_content_density(data):
    lengths = []
    for m in data.get("messages", []):
        for c in m.get("content", []):
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                lengths.append(len(c["text"]))
    if len(lengths) < 5:
        return "pass", "Not enough messages for density analysis"
    avg = sum(lengths) / len(lengths)
    spikes = [l for l in lengths if l > avg * 5]
    if spikes:
        return (
            "warn",
            f"{len(spikes)} message(s) are 5x above avg ({int(avg)} chars) — check for pasted content",
        )
    return (
        "pass",
        f"{len(lengths)} messages, avg {int(avg)} chars/message — no anomalies",
    )


def check_collab_mode(data):
    ctxs = data.get("meta", {}).get("turn_contexts", [])
    modes = list(
        dict.fromkeys(
            c.get("collaboration_mode", {}).get("mode")
            for c in ctxs
            if c.get("collaboration_mode", {}).get("mode")
        )
    )
    if not modes:
        return "warn", "No collaboration mode found"
    if len(modes) == 1:
        return "pass", f"Consistent mode: {modes[0]}"
    return "warn", f"Mode changed: {', '.join(modes)}"


def check_hardcoded(data):
    patterns = [
        (r'return\s+["\'].*[Ss]uccess["\']', "hardcoded success return"),
        (r'console\.log\(["\'][0-9]+["\']\)', "numeric debug log"),
        (r'password\s*=\s*["\'][^"\']+["\']', "plaintext password"),
        (
            r"(SELECT|INSERT|UPDATE|DELETE).*\+\s*(user|input|query)",
            "SQL concatenation",
        ),
    ]
    hits = []
    for m in data.get("messages", []):
        for c in m.get("content", []):
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                for pat, label in patterns:
                    if re.search(pat, c["text"], re.IGNORECASE):
                        hits.append(label)
    if not hits:
        return "pass", "No hardcoded or insecure patterns detected"
    counts = Counter(hits)
    return (
        "warn",
        f"{len(hits)} pattern(s): {', '.join(f'{v}x {k}' for k,v in counts.items())}",
    )


CHECKS = [
    ("Structure integrity", check_structure),
    ("Session ID uniqueness", check_session_id),
    ("Timestamp continuity", check_timestamps),
    ("Model consistency", check_models),
    ("Working directory (cwd)", check_cwd),
    ("Message role sequence", check_role_sequence),
    ("Tool call ID integrity", check_tool_ids),
    ("Approval policy consistency", check_approval_policy),
    ("Originator & source", check_originator),
    ("Content density analysis", check_content_density),
    ("Collaboration mode", check_collab_mode),
    ("Hardcoded content detection", check_hardcoded),
]


def run_checks(data):
    results = []
    for name, fn in CHECKS:
        try:
            status, detail = fn(data)
        except Exception as e:
            status, detail = "warn", f"Check error: {e}"
        results.append({"name": name, "status": status, "detail": detail})
    return results


def score(results):
    return min(100, sum(WEIGHTS.get(r["status"], 0) for r in results))


def print_report(path, results, risk_score):
    icons = {"pass": f"{GRN}✓{RST}", "warn": f"{YEL}!{RST}", "fail": f"{RED}✕{RST}"}
    print(f"\n{BOLD}{'='*60}{RST}")
    print(f"{BOLD}Trajectory Validation Report{RST}")
    print(f"File : {path}")
    print(f"Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}{RST}")
    for r in results:
        icon = icons.get(r["status"], "?")
        print(f"  {icon}  {r['name']}")
        print(f"     {DIM}{r['detail']}{RST}")
    print(f"\n{'='*60}")
    flags = sum(1 for r in results if r["status"] != "pass")
    if risk_score == 0:
        verdict = f"{GRN}{BOLD}LEGITIMATE{RST} — No anomalies detected"
    elif risk_score < 30:
        verdict = f"{YEL}{BOLD}LOW-MEDIUM RISK{RST} ({risk_score}/100) — Review flagged checks"
    elif risk_score < 60:
        verdict = (
            f"{YEL}{BOLD}MEDIUM RISK{RST} ({risk_score}/100) — Likely manipulation"
        )
    else:
        verdict = f"{RED}{BOLD}HIGH RISK{RST} ({risk_score}/100) — Reject submission"
    print(f"Risk score : {risk_score}/100")
    print(f"Flags      : {flags}/{len(results)}")
    print(f"Verdict    : {verdict}")
    print(f"{'='*60}\n")


def validate_file(path):
    try:
        data = load_json(path)
        results = run_checks(data)
        risk = score(results)
        print_report(path, results, risk)
        return risk
    except Exception as e:
        print(f"{RED}ERROR{RST} reading {path}: {e}")
        return 100


def batch_validate(folder):

    folder = Path(folder)
    if not folder.is_dir():
        print(f"{RED}ERROR{RST}: {folder} is not a directory")
        raise SystemExit(1)

    all_files = [p for p in folder.rglob("*") if p.is_file()]

    # 1) Fail immediately if any non-JSON file exists
    non_json_files = [p for p in all_files if p.suffix.lower() != ".json"]
    if non_json_files:
        print(f"{RED}ERROR{RST}: Non-JSON file(s) found. Batch rejected.")
        for p in non_json_files[: min(10, len(non_json_files))]:
            print(f"  - {p}")
        if len(non_json_files) > 10:
            print(f"  ... and {len(non_json_files) - 10} more")
        raise SystemExit(1)

    json_files = sorted(all_files)
    if not json_files:
        print(f"{RED}ERROR{RST}: No JSON files found in {folder}")
        raise SystemExit(1)

    # 2) Fail immediately if any filename is not develop-N.json or bugfix-N.json
    name_pattern = re.compile(r"^(develop|bugfix)-\d+\.json$")
    bad_names = [p for p in json_files if not name_pattern.match(p.name)]
    if bad_names:
        print(f"{RED}ERROR{RST}: Invalid filename(s). Batch rejected.")
        print("Expected pattern: develop-N.json or bugfix-N.json")
        for p in bad_names[: min(10, len(bad_names))]:
            print(f"  - {p.name}")
        if len(bad_names) > 10:
            print(f"  ... and {len(bad_names) - 10} more")
        raise SystemExit(1)

    # 3) Content checks
    summary = []
    for f in json_files:
        risk = validate_file(str(f))
        summary.append((str(f), risk))

    print(f"\n{BOLD}Batch Summary ({len(summary)} files){RST}")
    print("-" * 50)
    for path, risk in sorted(summary, key=lambda x: -x[1]):
        col = GRN if risk == 0 else YEL if risk < 40 else RED
        print(f"  {col}{risk:3d}/100{RST}  {path}")

    high_risk = sum(1 for _, r in summary if r >= 60)
    print(f"\nHigh-risk submissions: {high_risk}/{len(summary)}")


def main():
    parser = argparse.ArgumentParser(
        description="Session trajectory validator for QA teams"
    )
    parser.add_argument(
        "target", help="Path to trajectory.json or folder for batch mode"
    )
    parser.add_argument(
        "--batch", action="store_true", help="Scan entire folder recursively"
    )
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    if args.batch or os.path.isdir(args.target):
        batch_validate(args.target)
    else:
        data = load_json(args.target)
        results = run_checks(data)
        risk = score(results)
        if args.json:
            print(
                json.dumps(
                    {"file": args.target, "risk_score": risk, "checks": results},
                    indent=2,
                )
            )
        else:
            print_report(args.target, results, risk)
        sys.exit(1 if risk >= 60 else 0)


if __name__ == "__main__":
    main()
