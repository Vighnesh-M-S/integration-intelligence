"""
Stage 2 of the pipeline: verify.py

Only touches apps research.py flagged as needs_verification=True (confidence
< VERIFY_THRESHOLD, or the Composio registry disagreed with the LLM's guess).

For each one, it gets a SECOND, independently-sourced opinion:
  - renders the docs page with Playwright (a real browser) instead of a bare
    requests.get(), which catches JS-rendered docs sites requests/BS4 missed
  - re-runs the LLM extraction against that fresh text
  - diffs the new answer against the original field by field

This is the "browser-use" verification loop the assignment asks for, distinct
from research.py's static-fetch path. Where the two still disagree after this,
the record is left flagged for a human to resolve (see human_review_sample.csv).
"""

import json
from pathlib import Path

from dotenv import load_dotenv

from research import (
    FIELDS, PROCESSED_DIR, build_clients, compute_confidence,
    extract_with_llm, slugify,
)

load_dotenv()

ROOT = Path(__file__).parent
VERIFICATION_OUT = ROOT / "data" / "processed" / "verification.json"


def fetch_docs_with_browser(url: str) -> dict:
    from playwright.sync_api import sync_playwright

    if not url.startswith("http"):
        url = "https://" + url
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent="Mozilla/5.0 (compatible; integration-intelligence-verify/1.0)")
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)  # let client-side rendered docs settle
            text = page.inner_text("body")
            browser.close()
        return {"url": url, "status": 200, "text": text[:8000], "ok": True}
    except Exception as e:
        return {"url": url, "status": None, "text": "", "ok": False, "error": str(e)}


def diff_fields(original: dict, verified: dict) -> list[str]:
    # "category" (and "name") are given inputs, not something the LLM extracts —
    # extract_with_llm() never returns them, so comparing/merging them here would
    # always show a spurious diff and, worse, null the real category out on merge.
    changed = []
    for field in FIELDS:
        if field in ("name", "category"):
            continue
        if original.get(field) != verified.get(field):
            changed.append(field)
    return changed


def main():
    composio_client, llm_client = build_clients()

    records = sorted(PROCESSED_DIR.glob("*.json"))
    to_verify = []
    for path in records:
        if path.name == "verification.json":
            continue
        record = json.loads(path.read_text())
        if record.get("needs_verification"):
            to_verify.append((path, record))

    print(f"[verify] {len(to_verify)} apps flagged for verification out of {len(records) - 1}")

    verification_log = []
    for path, record in to_verify:
        name = record["name"]
        print(f"[verify] re-checking {name}")

        browser_docs = fetch_docs_with_browser(record["hint_url"])
        verified_extraction = extract_with_llm(
            llm_client, name, record["category"], browser_docs, record.get("composio_registry_match")
        )
        verified_confidence = compute_confidence(browser_docs, record.get("composio_registry_match"), verified_extraction)

        original_fields = {k: record.get(k) for k in FIELDS if k not in ("name", "category")}
        verified_fields = {k: verified_extraction.get(k) for k in FIELDS if k not in ("name", "category")}
        changed = diff_fields(record, verified_extraction)

        entry = {
            "id": record["id"],
            "name": name,
            "method": "playwright_recheck",
            "original": original_fields,
            "original_confidence": record["confidence"],
            "verified": verified_fields,
            "verified_confidence": verified_confidence,
            "changed_fields": changed,
            "reason": (
                "browser-rendered re-fetch + independent LLM pass; "
                + (f"changed {len(changed)} field(s)" if changed else "confirmed original answer")
            ),
        }
        verification_log.append(entry)

        # Only overwrite the working record if the re-check is more confident.
        if verified_confidence >= record["confidence"]:
            record.update(verified_fields)
            record["confidence"] = verified_confidence
        record["needs_verification"] = verified_confidence < 0.7 and bool(changed)
        record["verified"] = True
        record["verification_method"] = "playwright_recheck"
        path.write_text(json.dumps(record, indent=2))

    VERIFICATION_OUT.write_text(json.dumps(verification_log, indent=2))
    print(f"[verify] wrote {len(verification_log)} verification results to {VERIFICATION_OUT}")


if __name__ == "__main__":
    main()
