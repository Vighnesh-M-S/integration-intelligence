"""
Stage 4 of the pipeline: generate_report.py

Reads data/summary.json (from analyze.py) and every data/processed/*.json
record, computes the small amount of presentation math (bar widths, sort
order, badge classes), and renders templates/report.html.j2 into
report/index.html — a single self-contained page with no external requests,
so it works as a static deploy target (GitHub Pages / Netlify / anywhere).
"""

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).parent
PROCESSED_DIR = ROOT / "data" / "processed"
SUMMARY_JSON = ROOT / "data" / "summary.json"
TEMPLATE_DIR = ROOT / "templates"
OUT_PATH = ROOT / "report" / "index.html"

# Fixed categorical order (never re-cycled per app) so colors stay stable
# regardless of which auth methods happen to appear.
AUTH_COLOR_ORDER = {
    "OAuth2": "series-1", "API key": "series-2", "Token": "series-3",
    "Basic": "series-4", "Other": "series-5", "Unknown": "series-6",
}


def load_records():
    records = []
    for path in sorted(PROCESSED_DIR.glob("*.json")):
        if path.name == "verification.json":
            continue
        records.append(json.loads(path.read_text()))
    records.sort(key=lambda r: int(r["id"]))
    return records


def bars(counter: dict, color_map: dict | None = None, default_color="series-1"):
    total = sum(counter.values()) or 1
    out = []
    for i, (label, count) in enumerate(counter.items()):
        color = (color_map or {}).get(label, default_color)
        out.append({"label": label or "(none)", "count": count,
                     "pct": round(100 * count / total, 1), "color": color})
    return out


def category_bars(by_category: dict):
    out = []
    for cat, stats in sorted(by_category.items()):
        total = stats["total"] or 1
        out.append({
            "category": cat, "total": total,
            "self_serve_pct": round(100 * stats["self_serve"] / total, 1),
            "gated_pct": round(100 * stats["gated"] / total, 1),
            "buildable_pct": round(100 * stats["buildable"] / total, 1),
            "blocked_pct": round(100 * (total - stats["buildable"]) / total, 1),
            "self_serve": stats["self_serve"], "gated": stats["gated"],
            "buildable": stats["buildable"], "blocked": total - stats["buildable"],
        })
    return out


def badge_class(value: str) -> str:
    v = (value or "").lower()
    if "self_serve" in v or v == "true":
        return "badge-good"
    if "gated" in v or v == "false":
        return "badge-critical"
    return "badge-muted"


def main():
    if not SUMMARY_JSON.exists():
        raise SystemExit("data/summary.json not found — run `python analyze.py` first.")

    summary = json.loads(SUMMARY_JSON.read_text())
    patterns = summary["patterns"]
    accuracy = summary.get("verification_accuracy")
    records = load_records()

    for r in records:
        r["auth_str"] = ", ".join(r.get("authentication") or []) or "Unknown"
        r["self_serve_badge"] = badge_class(r.get("self_serve"))
        r["buildable_badge"] = badge_class(str(r.get("buildable")))
        r["confidence_pct"] = round((r.get("confidence") or 0) * 100)
        evidence = (r.get("evidence_urls") or [None])[0]
        r["evidence_url"] = evidence if (evidence and evidence.startswith("http")) else f"https://{evidence}" if evidence else None

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("report.html.j2")
    html = template.render(
        total_apps=patterns["total_apps"],
        auth_bars=bars(patterns["auth_distribution"], AUTH_COLOR_ORDER),
        self_serve_bars=bars(patterns["self_serve_distribution"]),
        category_bars=category_bars(patterns["by_category"]),
        top_blockers=list(patterns["top_blockers"].items())[:6],
        mcp_coverage=patterns["mcp_coverage"],
        buildable_today=patterns["buildable_today"],
        easy_wins=patterns["easy_wins"],
        needs_outreach=patterns["needs_outreach"],
        low_confidence_count=patterns["low_confidence_count"],
        composio_registry_hits=patterns["composio_registry_hits"],
        accuracy=accuracy,
        records=records,
        records_json=json.dumps(records),
        summary_json=json.dumps(summary),
    )
    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(html)
    print(f"[generate_report] wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
