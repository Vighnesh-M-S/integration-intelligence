"""
Stage 3 of the pipeline: analyze.py

Two things live here, on purpose kept in one file since neither is complex
enough to earn its own script:

1. `--sample`: writes data/human_review_sample.csv — ~20 apps (every app that
   went through verify.py, plus a random control group that never needed
   verification) with the agent's first-pass and final answers side by side,
   and blank human_* columns. This is the actual human-in-the-loop checkpoint
   the assignment asks for: a person reads the real docs and fills this in
   by hand.

2. default: reads every data/processed/*.json record and computes the
   pattern-mining the assignment wants (auth distribution, self-serve vs
   gated, blockers, MCP coverage, easy wins, buildability by category), plus,
   if data/human_review_sample.csv has been filled in, the measured
   first-pass vs post-verification accuracy. Writes data/summary.json.
"""

import argparse
import csv
import json
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
PROCESSED_DIR = ROOT / "data" / "processed"
SAMPLE_CSV = ROOT / "data" / "human_review_sample.csv"
SUMMARY_JSON = ROOT / "data" / "summary.json"
VERIFICATION_JSON = PROCESSED_DIR / "verification.json"

ACCURACY_FIELDS = ["authentication", "self_serve", "buildable"]
# Two pools, sampled separately and capped independently, since the number of
# apps verify.py actually flags varies run to run and can exceed a single
# fixed total (it did: 34 out of 100 here). Sampling from both pools lets the
# grading set answer two different questions: did the verification loop help
# on apps the agent was unsure about, and how often was the agent wrong even
# when it was confident.
VERIFIED_SAMPLE_SIZE = 12
CONTROL_SAMPLE_SIZE = 10


def load_records():
    records = []
    for path in sorted(PROCESSED_DIR.glob("*.json")):
        if path.name == "verification.json":
            continue
        records.append(json.loads(path.read_text()))
    return records


# ---------------------------------------------------------------- sampling --

def write_human_sample(records):
    verification_log = json.loads(VERIFICATION_JSON.read_text()) if VERIFICATION_JSON.exists() else []
    verified_by_id = {v["id"]: v for v in verification_log}

    verified_ids = set(verified_by_id)
    control_pool = [r for r in records if r["id"] not in verified_ids]
    random.seed(42)

    verified_records = [r for r in records if r["id"] in verified_ids]
    verified_sample_ids = {
        r["id"] for r in random.sample(verified_records, min(VERIFIED_SAMPLE_SIZE, len(verified_records)))
    }
    control_sample_ids = {
        r["id"] for r in random.sample(control_pool, min(CONTROL_SAMPLE_SIZE, len(control_pool)))
    }
    sample_ids = verified_sample_ids | control_sample_ids

    rows = []
    for r in records:
        if r["id"] not in sample_ids:
            continue
        v = verified_by_id.get(r["id"])
        first_pass = v["original"] if v else {k: r.get(k) for k in ACCURACY_FIELDS}
        final = v["verified"] if v else {k: r.get(k) for k in ACCURACY_FIELDS}
        rows.append({
            "id": r["id"],
            "name": r["name"],
            "hint_url": r["hint_url"],
            "was_verified": bool(v),
            "agent_first_pass_authentication": first_pass.get("authentication"),
            "agent_first_pass_self_serve": first_pass.get("self_serve"),
            "agent_first_pass_buildable": first_pass.get("buildable"),
            "agent_final_authentication": final.get("authentication"),
            "agent_final_self_serve": final.get("self_serve"),
            "agent_final_buildable": final.get("buildable"),
            "human_authentication": "",
            "human_self_serve": "",
            "human_buildable": "",
            "human_notes": "",
        })

    with open(SAMPLE_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[analyze] wrote {len(rows)}-app human review sample to {SAMPLE_CSV}")
    print("[analyze] fill in the human_* columns by reading each app's real docs, then re-run `python analyze.py`.")


# --------------------------------------------------------------- accuracy --

def compute_accuracy():
    if not SAMPLE_CSV.exists():
        return None
    with open(SAMPLE_CSV) as f:
        rows = list(csv.DictReader(f))
    graded = [r for r in rows if r.get("human_authentication", "").strip()]
    if not graded:
        return None

    def matches(a, b):
        return str(a).strip().lower() == str(b).strip().lower()

    first_pass_hits, final_hits, total_checks = 0, 0, 0
    by_field = {f: {"first_pass_hits": 0, "final_hits": 0, "total": 0} for f in ACCURACY_FIELDS}
    misses = []
    for r in graded:
        for field in ACCURACY_FIELDS:
            human_val = r.get(f"human_{field}")
            if not human_val:
                continue
            total_checks += 1
            fp_ok = matches(r.get(f"agent_first_pass_{field}"), human_val)
            fin_ok = matches(r.get(f"agent_final_{field}"), human_val)
            first_pass_hits += fp_ok
            final_hits += fin_ok
            by_field[field]["total"] += 1
            by_field[field]["first_pass_hits"] += fp_ok
            by_field[field]["final_hits"] += fin_ok
            if not fin_ok:
                misses.append({"app": r["name"], "field": field,
                                "agent_said": r.get(f"agent_final_{field}"), "human_said": human_val})

    field_breakdown = {
        f: {
            "first_pass_accuracy": round(v["first_pass_hits"] / v["total"], 3) if v["total"] else None,
            "final_accuracy": round(v["final_hits"] / v["total"], 3) if v["total"] else None,
            "total": v["total"],
        }
        for f, v in by_field.items()
    }

    return {
        "sample_size_apps": len(graded),
        "fields_checked_per_app": ACCURACY_FIELDS,
        "total_field_checks": total_checks,
        "first_pass_accuracy": round(first_pass_hits / total_checks, 3) if total_checks else None,
        "final_accuracy": round(final_hits / total_checks, 3) if total_checks else None,
        "accuracy_by_field": field_breakdown,
        "remaining_misses": misses,
    }


# --------------------------------------------------------------- patterns --

# The LLM phrases the same root cause differently app to app (e.g. "no public
# API" vs "no public API documentation available" vs "no public API (local
# CLI tool)"). Bucket by keyword so "most common blocker" reflects the real
# root cause instead of being split across near-duplicate strings.
BLOCKER_BUCKETS = [
    ("no public api", "No public API"),
    ("partnership", "Partnership-gated"),
    ("contact sales", "Partnership-gated"),
    ("paid plan", "Requires paid plan"),
    ("subscription", "Requires paid plan"),
    ("approval", "Requires admin approval"),
    ("login", "Requires authenticated login / no API"),
]


def normalize_blocker(raw: str) -> str:
    text = (raw or "").strip().lower()
    for keyword, bucket in BLOCKER_BUCKETS:
        if keyword in text:
            return bucket
    return (raw or "").strip()


def compute_patterns(records):
    total = len(records)
    auth_counter = Counter()
    for r in records:
        for a in (r.get("authentication") or []):
            auth_counter[a] += 1

    self_serve_counter = Counter(r.get("self_serve") for r in records)
    blocker_counter = Counter(
        normalize_blocker(r.get("blocker")) for r in records if not r.get("buildable") and r.get("blocker")
    )
    mcp_count = sum(1 for r in records if r.get("mcp"))
    buildable_count = sum(1 for r in records if r.get("buildable"))

    by_category = {}
    for r in records:
        cat = r["category"]
        by_category.setdefault(cat, {"total": 0, "self_serve": 0, "gated": 0, "buildable": 0})
        by_category[cat]["total"] += 1
        if r.get("buildable"):
            by_category[cat]["buildable"] += 1
        if str(r.get("self_serve", "")).startswith("self_serve"):
            by_category[cat]["self_serve"] += 1
        else:
            by_category[cat]["gated"] += 1

    easy_wins = sorted(
        [r for r in records if r.get("buildable") and str(r.get("self_serve", "")).startswith("self_serve")],
        key=lambda r: r["name"],
    )
    needs_outreach = sorted(
        [r for r in records if not r.get("buildable") or "partnership" in str(r.get("self_serve", ""))],
        key=lambda r: r["name"],
    )

    return {
        "total_apps": total,
        "auth_distribution": dict(auth_counter.most_common()),
        "self_serve_distribution": dict(self_serve_counter.most_common()),
        "top_blockers": dict(blocker_counter.most_common(10)),
        "mcp_coverage": {"count": mcp_count, "pct": round(mcp_count / total, 3)},
        "buildable_today": {"count": buildable_count, "pct": round(buildable_count / total, 3)},
        "by_category": by_category,
        "easy_wins": [r["name"] for r in easy_wins],
        "needs_outreach": [r["name"] for r in needs_outreach],
        "low_confidence_count": sum(1 for r in records if r.get("confidence", 1) < 0.7),
        "composio_registry_hits": sum(1 for r in records if r.get("composio_registry_match")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="write the human review sample CSV and exit")
    args = parser.parse_args()

    records = load_records()
    if not records:
        print("[analyze] no processed records found — run research.py first.")
        return

    if args.sample:
        write_human_sample(records)
        return

    summary = {
        "patterns": compute_patterns(records),
        "verification_accuracy": compute_accuracy(),
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))
    print(f"[analyze] wrote {SUMMARY_JSON}")
    if summary["verification_accuracy"] is None:
        print("[analyze] no graded human_review_sample.csv found yet — accuracy section will be empty in the report.")


if __name__ == "__main__":
    main()
