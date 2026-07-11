"""
Interactive human verification tool.

Walks through data/human_review_sample.csv one app at a time: shows what the
agent concluded, optionally opens the real docs page in your browser, asks
y/n whether each field is correct (typing the right value if not), and
writes the human_* columns back into the CSV as you go — so progress is
never lost if you stop partway through.

Run: python human_verify.py
Resume anytime: already-graded rows (non-empty human_authentication) are
skipped automatically. Ctrl-C at any prompt saves what's graded so far.
"""

import ast
import csv
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
SAMPLE_CSV = ROOT / "data" / "human_review_sample.csv"

SELF_SERVE_OPTIONS = [
    "self_serve_free", "self_serve_trial", "gated_paid_plan",
    "gated_admin_approval", "gated_partnership", "unknown",
]


def load_rows():
    with open(SAMPLE_CSV, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


def save_rows(rows, fieldnames):
    with open(SAMPLE_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def pretty_auth(raw: str) -> str:
    try:
        return ", ".join(ast.literal_eval(raw))
    except Exception:
        return raw


def ask_yn(prompt: str) -> bool:
    while True:
        ans = input(f"{prompt} [y/n] ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please type y or n")


def ask_self_serve() -> str:
    print("  What's the correct self-serve status?")
    for i, opt in enumerate(SELF_SERVE_OPTIONS, 1):
        print(f"    {i}) {opt}")
    while True:
        choice = input("  > ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(SELF_SERVE_OPTIONS):
            return SELF_SERVE_OPTIONS[int(choice) - 1]
        print("  please enter a number from the list")


def ask_auth() -> str:
    raw = input("  Correct auth method(s), comma-separated (e.g. OAuth2, API key): ").strip()
    methods = [m.strip() for m in raw.split(",") if m.strip()]
    return str(methods)


def grade_one(row: dict) -> dict:
    print("\n" + "=" * 60)
    print(f"App: {row['name']}")
    url = row["hint_url"]
    full_url = url if url.startswith("http") else f"https://{url}"
    print(f"Docs: {full_url}")
    print("-" * 60)
    print("Agent's first-pass answer:")
    print(f"  Authentication : {pretty_auth(row['agent_first_pass_authentication'])}")
    print(f"  Self-serve     : {row['agent_first_pass_self_serve']}")
    print(f"  Buildable      : {row['agent_first_pass_buildable']}")
    if row["was_verified"] == "True":
        print("Agent's final answer (after browser-use re-check):")
        print(f"  Authentication : {pretty_auth(row['agent_final_authentication'])}")
        print(f"  Self-serve     : {row['agent_final_self_serve']}")
        print(f"  Buildable      : {row['agent_final_buildable']}")
    print("-" * 60)

    if ask_yn("Open docs in browser now?"):
        webbrowser.open(full_url)

    auth_ok = ask_yn("Is the authentication correct?")
    row["human_authentication"] = row["agent_final_authentication"] if auth_ok else ask_auth()

    ss_ok = ask_yn("Is the self-serve status correct?")
    row["human_self_serve"] = row["agent_final_self_serve"] if ss_ok else ask_self_serve()

    build_ok = ask_yn("Is the buildable verdict correct?")
    row["human_buildable"] = row["agent_final_buildable"] if build_ok else str(not (row["agent_final_buildable"] == "True"))

    notes = input("Notes (optional): ").strip()
    row["human_notes"] = notes
    return row


def main():
    rows, fieldnames = load_rows()
    pending = [r for r in rows if not r.get("human_authentication", "").strip()]
    done = len(rows) - len(pending)
    print(f"{len(rows)} apps in sample, {done} already graded, {len(pending)} left.")

    graded_this_session = 0
    try:
        for row in pending:
            grade_one(row)
            save_rows(rows, fieldnames)  # persist after every app
            graded_this_session += 1
    except KeyboardInterrupt:
        print(f"\n\nStopped early. Graded {graded_this_session} apps this session, progress saved.")
        sys.exit(0)

    print(f"\nDone. Graded {graded_this_session} apps this session ({done + graded_this_session}/{len(rows)} total).")
    print("Now run: python analyze.py")


if __name__ == "__main__":
    main()
