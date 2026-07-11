"""
Stage 1 of the pipeline: research.py

For every app in data/apps.csv:
  1. Look it up in Composio's own toolkit registry (dogfooding Composio's SDK).
     If Composio has already built this integration, that's an authoritative,
     structured ground truth for auth scheme / category / tool count.
  2. Fetch the app's public docs page (requests + BeautifulSoup).
  3. Ask an LLM to extract the structured fields the assignment asks for,
     using the fetched docs text (and the Composio data, if any) as context.
  4. Score a confidence for the record and save it.

Records with confidence below VERIFY_THRESHOLD get picked up by verify.py.
"""

import csv
import json
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
APPS_CSV = ROOT / "data" / "apps.csv"
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"

VERIFY_THRESHOLD = 0.7
REQUEST_TIMEOUT = 12
LLM_MODEL = "gemini-flash-lite-latest"

FIELDS = [
    "name", "category", "description", "authentication", "self_serve",
    "api_surface", "api_breadth", "mcp", "buildable", "blocker",
    "evidence_urls", "notes",
]


def slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\(.*?\)", "", s)          # drop parenthetical aliases
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def slug_candidates(name: str) -> list[str]:
    base = slugify(name)
    candidates = {base, base.replace("_", "-"), base.split("_")[0]}
    return [c for c in candidates if c]


def lookup_composio(client, name: str) -> dict | None:
    """Check whether this app already exists as a Composio toolkit.
    Returns None if not found or if the lookup fails for any reason —
    this is a bonus ground-truth signal, not a required dependency."""
    if client is None:
        return None
    for slug in slug_candidates(name):
        try:
            toolkit = client.toolkits.get(slug=slug)
        except Exception:
            continue
        if toolkit is None:
            continue
        try:
            return {
                "slug": slug,
                "name": getattr(toolkit, "name", slug),
                "auth_schemes": list(getattr(toolkit, "composio_managed_auth_schemes", []) or []),
                "categories": [c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
                                for c in (getattr(getattr(toolkit, "meta", None), "categories", []) or [])],
                "tools_count": getattr(getattr(toolkit, "meta", None), "tools_count", None),
                "description": getattr(getattr(toolkit, "meta", None), "description", None),
            }
        except Exception:
            return {"slug": slug, "raw_repr": str(toolkit)[:500]}
    return None


def fetch_docs(url: str) -> dict:
    """Best-effort fetch of a docs/marketing page. Returns status + visible text."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; integration-intelligence-research/1.0)"},
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = re.sub(r"\n{2,}", "\n", soup.get_text("\n")).strip()
        return {"url": url, "status": resp.status_code, "text": text[:8000], "ok": resp.status_code == 200}
    except Exception as e:
        return {"url": url, "status": None, "text": "", "ok": False, "error": str(e)}


EXTRACTION_PROMPT = """You are researching a SaaS application for an AI agent-tooling feasibility study.

App: {name}
Category (given): {category}
Docs page fetched: {url} (fetch_ok={fetch_ok})
Docs text (may be partial or empty if fetch failed):
---
{docs_text}
---
{composio_context}

Based on the above (and your own knowledge of this well-known product if the docs text is thin),
return ONLY a JSON object with exactly these fields:

- "description": one-line plain description of what the app does.
- "authentication": array of auth methods it supports, from ["OAuth2","API key","Basic","Token","Other","Unknown"].
- "self_serve": one of ["self_serve_free","self_serve_trial","gated_paid_plan","gated_admin_approval","gated_partnership","unknown"].
- "api_surface": one of ["REST","GraphQL","REST+GraphQL","none_public","unclear"].
- "api_breadth": one of ["narrow","moderate","broad","unknown"] — rough sense of how many endpoints/resources are documented.
- "mcp": true/false — whether this app has a known official or community MCP server.
- "buildable": true/false — could this realistically be an agent toolkit today.
- "blocker": short string, empty "" if buildable is true. Otherwise the MAIN blocker (e.g. "no public API", "partner-gated auth", "requires paid plan").
- "evidence_urls": array with the docs URL(s) you relied on (use the fetched URL; add others only if you're confident they're real).
- "notes": short free-text caveats, e.g. if you are relying on prior knowledge rather than the fetched page.
- "self_confidence": your own confidence in this extraction, a number from 0 to 1.

Return raw JSON only, no markdown fences, no commentary.
"""


def extract_with_llm(llm_client, app_name: str, category: str, docs: dict, composio_data: dict | None) -> dict:
    composio_context = ""
    if composio_data:
        composio_context = (
            "Composio's own toolkit registry already has this app integrated. "
            f"Composio-reported auth schemes: {composio_data.get('auth_schemes')}. "
            f"Composio-reported tool count: {composio_data.get('tools_count')}. "
            "Treat this as a strong signal for the authentication field."
        )

    prompt = EXTRACTION_PROMPT.format(
        name=app_name, category=category, url=docs.get("url"),
        fetch_ok=docs.get("ok"), docs_text=docs.get("text") or "(fetch failed / empty)",
        composio_context=composio_context,
    )

    response = None
    for attempt in range(3):
        try:
            response = llm_client.models.generate_content(model=LLM_MODEL, contents=prompt)
            break
        except Exception as e:
            if attempt == 2:
                raise
            print(f"[research]   LLM call failed ({e.__class__.__name__}), retrying in {2 ** attempt}s...")
            time.sleep(2 ** attempt)
    raw_text = (response.text or "").strip()
    raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {"notes": f"LLM output was not valid JSON: {raw_text[:300]}", "self_confidence": 0.0}


def compute_confidence(docs: dict, composio_data: dict | None, extracted: dict) -> float:
    """Programmatic confidence score — deliberately does not just trust the
    model's own self-rating, since LLMs are overconfident. Combines:
    - whether we got real docs text to ground the extraction
    - whether Composio's registry corroborates the LLM's auth answer
    - the model's own self-reported confidence, weighted down
    """
    score = 0.35
    if docs.get("ok") and len(docs.get("text", "")) > 500:
        score += 0.2
    if composio_data:
        score += 0.15
        llm_auth = set(a.lower() for a in extracted.get("authentication", []) if isinstance(a, str))
        composio_auth = set(a.lower() for a in composio_data.get("auth_schemes", []) if isinstance(a, str))
        if composio_auth and llm_auth:
            if composio_auth & llm_auth:
                score += 0.15
            else:
                score -= 0.2  # real disagreement between two sources -> needs verification
    self_conf = extracted.get("self_confidence")
    if isinstance(self_conf, (int, float)):
        score += 0.15 * self_conf
    return round(max(0.0, min(1.0, score)), 2)


def build_clients():
    composio_client = None
    try:
        from composio import Composio
        if os.getenv("COMPOSIO_API_KEY"):
            composio_client = Composio(api_key=os.getenv("COMPOSIO_API_KEY"))
    except Exception as e:
        print(f"[research] Composio client unavailable, continuing without registry cross-check: {e}")

    from google import genai
    llm_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return composio_client, llm_client


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                         help="only process the first N not-yet-done apps (for smoke testing before a full run)")
    parser.add_argument("--ids", type=str, default=None,
                         help="comma-separated app ids to process, e.g. --ids 1,21,41,61,81")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    composio_client, llm_client = build_clients()

    with open(APPS_CSV) as f:
        apps = list(csv.DictReader(f))

    if args.ids:
        wanted = set(args.ids.split(","))
        apps = [a for a in apps if a["id"] in wanted]
    if args.limit:
        pending = [a for a in apps if not (PROCESSED_DIR / f"{a['id']}_{slugify(a['name'])}.json").exists()]
        apps = pending[: args.limit]

    for app in apps:
        app_id, name, category, hint_url = app["id"], app["name"], app["category"], app["hint_url"]
        out_path = PROCESSED_DIR / f"{app_id}_{slugify(name)}.json"
        if out_path.exists():
            print(f"[research] skip (already done): {name}")
            continue

        print(f"[research] {app_id}/100 {name}")
        composio_data = lookup_composio(composio_client, name)
        docs = fetch_docs(hint_url)
        extracted = extract_with_llm(llm_client, name, category, docs, composio_data)
        confidence = compute_confidence(docs, composio_data, extracted)

        record = {
            "id": app_id,
            "name": name,
            "category": category,
            "hint_url": hint_url,
            "composio_registry_match": composio_data,
            **{k: extracted.get(k) for k in FIELDS if k not in ("name", "category")},
            "confidence": confidence,
            "needs_verification": confidence < VERIFY_THRESHOLD,
            "source": "composio_registry+llm" if composio_data else "llm_from_docs",
            "pipeline_stage": "research",
        }
        if not record.get("evidence_urls"):
            record["evidence_urls"] = [docs.get("url")]

        with open(RAW_DIR / f"{app_id}_{slugify(name)}.json", "w") as f:
            json.dump({"docs": docs, "composio_raw": composio_data, "llm_raw": extracted}, f, indent=2)
        with open(out_path, "w") as f:
            json.dump(record, f, indent=2)

        time.sleep(0.3)  # be polite to docs sites

    print("[research] done.")


if __name__ == "__main__":
    main()
