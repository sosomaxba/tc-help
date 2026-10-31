#!/usr/bin/env python3
"""Grade a blind-test answer sheet against scripts/test/questions_<version>.json.

    python scripts/test/qa_check.py --answers <answers.json> [--key PATH] [--db PATH]

Answer sheet format (one object per question):

    [{"id": 1, "final_href": "...", "final_title": "...",
      "candidates": ["href", ...], "rounds": 2, "confidence": "high",
      "notes": "..."}]

Positive questions score hit@1 (final page is the expected page) and
hit@3 (expected page is among the first three candidates).
Boundary questions pass when the answer declines to return a help page and
points at the right lane (or says the question is out of scope).
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--key", default=str(SKILL_ROOT / "scripts" / "test" / "questions_2506.json"))
    ap.add_argument("--db", default=str(SKILL_ROOT / "data" / "2506" / "tc_help_2506.db"))
    args = ap.parse_args()

    key = load(args.key)["questions"]
    answers = {a["id"]: a for a in load(args.answers)}
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    def lookup(href):
        if not href:
            return None
        return conn.execute("""SELECT p.title, t.title FROM pages p JOIN topics t ON t.id=p.topic_id
                               WHERE p.href=? LIMIT 1""", (href,)).fetchone()

    hit1 = hit3 = 0
    positives = [q for q in key if q["kind"] == "positive"]
    boundaries = [q for q in key if q["kind"] != "positive"]
    rows = []
    for q in positives:
        a = answers.get(q["id"], {})
        final = (a.get("final_href") or "").strip()
        accepted = set(q.get("accept") or [])
        accepted.add(q["expect_href"])
        cands = [str(c).strip() for c in (a.get("candidates") or [])][:3]
        # Copies of one page live in several books with different hrefs but the same title.
        same_title = (a.get("final_title") or "").strip().lower() == q["expect_title"].strip().lower()
        h1 = final in accepted or same_title
        h3 = h1 or bool(accepted & set(cands))
        hit1 += h1
        hit3 += h3
        picked = lookup(final)
        rows.append((q["id"], "HIT" if h1 else ("top3" if h3 else "MISS"), q["category"],
                     (picked[0][:38] if picked else (final[:38] or "-")), q["expect_title"][:38]))

    bpass = 0
    brows = []
    for q in boundaries:
        a = answers.get(q["id"], {})
        final = (a.get("final_href") or "").strip()
        notes = (a.get("notes") or "").strip()
        ok = not final and len(notes) >= 8
        bpass += ok
        brows.append((q["id"], "PASS" if ok else "FAIL", q["category"],
                      (final[:40] or "(未给页)"), notes[:60]))

    print(f"positive questions: {len(positives)}  hit@1 = {hit1}/{len(positives)} = {hit1/len(positives):.0%}  "
          f"hit@3 = {hit3}/{len(positives)} = {hit3/len(positives):.0%}")
    for r in rows:
        print(f"  [{r[0]:>2}] {r[1]:<4} {r[2][:22]:<24} got={r[3]:<40} want={r[4]}")
    print(f"\nboundary questions: {len(boundaries)}  pass = {bpass}/{len(boundaries)}")
    for r in brows:
        print(f"  [{r[0]:>2}] {r[1]:<4} {r[2][:12]:<14} final={r[3]:<42} {r[4]}")

    total_ok = hit1 + bpass
    print(f"\nscore: {total_ok}/{len(key)} = {total_ok/len(key):.0%} "
          f"(hit@1 positives + boundary pass)")
    return 0 if hit3 >= 0.9 * len(positives) and bpass == len(boundaries) else 1


if __name__ == "__main__":
    sys.exit(main())
