#!/usr/bin/env python3
"""Run the question bank against the help tool and score the retrieval.

    python scripts/test/qa_run.py [--bank scripts/test/questions_2506.json]
                             [--queries scripts/test/queries_2506.json]
                             [--out <report.md>] [--base auto|local|online] [--limit 5]

The bank holds the Chinese question and the page that answers it (the green
book). The queries file holds, per question, the official English terms a
following agent would search for (first = primary, the rest = fallbacks).

Output:
  * an answers sheet for scripts/test/qa_check.py (final_href + first 3 candidates)
  * a markdown report with one row per question and the original page links
  * a green-book integrity check (does every expected page still exist)

The runner measures the tool only: it cannot judge a translation it was handed.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
HELP = SKILL_ROOT / "scripts" / "help.py"


def load_help():
    spec = importlib.util.spec_from_file_location("help_cli", HELP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_search(mod, query, limit, base):
    """Return (rc, rows) for one English query through the real CLI."""
    if not query.strip():
        return 2, []
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = mod.main(["search", query, "--limit", str(limit), "--json", "--base", base])
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    if rc != 0:
        return rc, []
    try:
        return rc, json.loads(out.getvalue())
    except json.JSONDecodeError:
        return 5, []


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", default=str(SKILL_ROOT / "scripts" / "test" / "questions_2506.json"))
    ap.add_argument("--queries", required=True)
    ap.add_argument("--out", default=None, help="markdown report path")
    ap.add_argument("--answers", default=None, help="answers sheet for qa_check.py")
    ap.add_argument("--db", default=str(SKILL_ROOT / "data" / "2506" / "tc_help_2506.db"))
    ap.add_argument("--base", choices=("auto", "local", "online"), default="auto")
    ap.add_argument("--limit", type=int, default=5, help="candidates kept per query")
    args = ap.parse_args()

    bank = json.loads(Path(args.bank).read_text(encoding="utf-8"))
    qs = {q["id"]: q for q in json.loads(Path(args.queries).read_text(encoding="utf-8"))}
    mod = load_help()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    meta = mod.meta(conn)
    base_choice = mod.choose_base(args.base)

    def page_of(href):
        if not href:
            return None
        return conn.execute("""SELECT p.title, p.href, p.source_url, t.title topic, s.title section,
                                      c.name category, t.location, t.dir_name
                               FROM pages p JOIN topics t ON t.id=p.topic_id
                               JOIN categories c ON c.id=t.category_id
                               LEFT JOIN sections s ON s.id=p.section_id
                               WHERE p.href=? LIMIT 1""", (href,)).fetchone()

    def want_of(href):
        return page_of(href)

    # ── green-book integrity ──
    stale = []
    for q in bank["questions"]:
        for href in [q.get("expect_href"), *(q.get("accept") or [])]:
            if href is None:
                continue
            row = page_of(href)
            if row is None:
                stale.append(f"  [{q['id']}] href missing: {href}")
            elif href == q["expect_href"] and row["title"].strip().lower() != q["expect_title"].strip().lower():
                stale.append(f"  [{q['id']}] title drifted: {href} -> {row['title']!r} "
                             f"(bank says {q['expect_title']!r})")

    # ── run ──
    def is_expected(q, h):
        return (h["href"] in q.get("_acc", set())
                or h["title"].strip().lower() == q["expect_title"].strip().lower())

    answers, rows = [], []
    for q in bank["questions"]:
        entry = qs.get(q["id"])
        if entry is None:
            continue
        q["_acc"] = set([q.get("expect_href")] + (q.get("accept") or []))
        queries = entry.get("queries") or []
        used = []
        for query in queries:
            rc, hits = run_search(mod, query, args.limit, args.base)
            used.append((query, rc, hits))
        # What a following agent actually answers from: the first term set that returned pages.
        picked = next((hits for _, _, hits in used if hits), [])
        final = picked[0] if picked else None
        answers.append({
            "id": q["id"],
            "final_href": final["href"] if final else "",
            "final_title": final["title"] if final else "",
            "candidates": [c["href"] for c in picked[:3]],
            "rounds": len([1 for _, _, hits in used if hits]) if final else len(used),
            "confidence": "high" if final else "none",
            "notes": entry.get("notes") or ("no page found for these terms" if not final else ""),
        })
        if q["kind"] == "positive":
            hit1 = bool(final) and is_expected(q, final)
            hit3 = hit1 or any(is_expected(q, h) for h in picked[:3])
            # 换词重试能不能把它捞进前三（每组检索词各自的前三）
            hit3_any = hit3 or any(is_expected(q, h)
                                   for _, _, hits in used for h in hits[:3])
            verdict = "HIT1" if hit1 else ("TOP3" if hit3 else
                                           ("换词可救" if hit3_any else
                                            ("MISS" if final else "零命中")))
        else:
            hit1 = hit3 = hit3_any = False
            verdict = f"边界·回了 {len(picked)} 页"
        rows.append({"q": q, "used": used, "picked": picked, "final": final,
                     "verdict": verdict, "hit1": hit1, "hit3": hit3, "hit3_any": hit3_any,
                     "style": entry.get("terms_style") or "official",
                     "note": entry.get("notes") or ""})

    def md_link(title, url):
        return f"[{title}]({url})" if url else f"{title}（无网页链接：库内 PDF/Excel 抽的页）"

    no_link = conn.execute("""SELECT COUNT(*) n FROM pages p JOIN topics t ON t.id=p.topic_id
                              WHERE p.dup_of IS NULL AND (p.source_url IS NULL OR p.source_url='')
                                AND (t.location IS NULL OR t.location='')""").fetchone()["n"]
    online_only = conn.execute("""SELECT COUNT(*) n FROM pages p JOIN topics t ON t.id=p.topic_id
                                  WHERE p.dup_of IS NULL AND p.source_url IS NOT NULL
                                    AND p.source_url<>''""").fetchone()["n"]

    # ── report ──
    positives = [r for r in rows if r["q"]["kind"] == "positive"]
    boundaries = [r for r in rows if r["q"]["kind"] != "positive"]
    lines = [f"# tc-help 全量评估（{len(positives)} 正题 + {len(boundaries)} 边界题）\n",
             f"- 题库 `{Path(args.bank).name}` ｜ 检索词 `{Path(args.queries).name}` ｜ "
             f"库 `tc_help_2506.db`（{meta.get('pages')} 页，建于 {meta.get('built_at')}）",
             f"- 链接基座 `{base_choice}`（本机 5000 帮助镜像 / docs.sw.siemens.com 官网）。",
             f"- 链接覆盖：{no_link} 页只有库内文本没有网页原文（PDF 抽的页）；"
             f"{online_only} 页的原文链接只能走官网（《Siemens Web Framework》那本，本机镜像没收录，"
             "官网直连会跳登录）。其余都能点开。",
             "- 判定口径同 `scripts/test/qa_check.py`：HIT1 = 第一组检索词的第一命中就是该答的页"
             "（同标题的跨书副本算命中）；TOP3 = 该答的页在这组检索词的前三候选里；"
             "换词可救 = 第一组没进前三、但备用检索词能把它带进前三。\n",
             "| # | 分类 | 词语风格 | 判定 | 第一命中页 | 该答的页（绿本） |",
             "| :- | :--- | :--- | :--- | :--- | :--- |"]
    for r in rows:
        q = r["q"]
        first = md_link(r["picked"][0]["title"], r["picked"][0]["url"]) if r["picked"] else "（零命中）"
        want = want_of(q["expect_href"]) if q["kind"] == "positive" else None
        want_txt = (md_link(want["title"], mod.page_url(want, meta, base_choice))
                    if want else "—")
        lines.append(f"| {q['id']} | {q['category']} | {r['style']} | {r['verdict']} "
                     f"| {first} | {want_txt} |")
    lines.append("\n---\n")
    for r in rows:
        q, picked = r["q"], r["picked"]
        lines.append(f"## [{q['id']:>2}] {r['verdict']} · {q['category']}")
        lines.append(f"- 问题：{q['question']}")
        for j, (query, rc, hits) in enumerate(r["used"]):
            lines.append(f"- 第 {j + 1} 组检索词：`{query}` → {len(hits)} 页"
                         + (f"（rc={rc}：{'中文/无检索词被拒' if rc == 2 else '零命中' if rc == 3 else '命令出错'}）"
                            if rc else ""))
            for i, h in enumerate(hits[:3], 1):
                mark = " ← 实际会答这一页" if (i == 1 and hits is picked) else ""
                hit_mark = " ★该答的页" if q["kind"] == "positive" and is_expected(q, h) else ""
                lines.append(f"  {i}. {h['topic']} > {h.get('section') or '-'} > "
                             f"{md_link(h['title'], h['url'])}{mark}{hit_mark}")
        if q["kind"] == "positive":
            want = want_of(q["expect_href"])
            if want:
                lines.append(f"- 该答的页（绿本）：{want['topic']} > "
                             f"{md_link(want['title'], mod.page_url(want, meta, base_choice))}")
        else:
            beh = q.get("expect_behavior") or "不给帮助页，转对应车道"
            if picked:
                lines.append(f"- 正确行为：{beh}；工具实回 {len(picked)} 页，"
                             f"第一页是“{picked[0]['title']}”（不许当成答案）")
            else:
                lines.append(f"- 正确行为：{beh}；工具零命中")
        if r["note"]:
            lines.append(f"- 备注：{r['note']}")
        lines.append("")

    report = "\n".join(lines)
    if args.answers:
        Path(args.answers).write_text(json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")

    print(f"bank: {len(bank['questions'])} questions, run: {len(rows)}")
    if stale:
        print(f"green-book drift ({len(stale)}):")
        print("\n".join(stale))
    else:
        print("green-book drift: none (every expected page exists, titles match)")
    def tally(sub, key):
        return f"{sum(r[key] for r in sub)}/{len(sub)}" if sub else "0/0"

    print(f"positives: hit@1 {tally(positives, 'hit1')}  "
          f"hit@3 {tally(positives, 'hit3')}  hit@3(换词可救) {tally(positives, 'hit3_any')}")
    for style in sorted({r["style"] for r in positives}):
        sub = [r for r in positives if r["style"] == style]
        print(f"  terms_style={style:<13} hit@1 {tally(sub, 'hit1')}  hit@3 {tally(sub, 'hit3')}"
              f"  换词可救 {tally(sub, 'hit3_any')}")
    not_hit1 = [f"{r['q']['id']}:{r['verdict']}" for r in positives if not r["hit1"]]
    if not_hit1:
        print("  positives not hit@1: " + " ".join(not_hit1))
    zero = [r["q"]["id"] for r in positives if not r["picked"]]
    if zero:
        print(f"  正题零命中（两组检索词都查不到）: {zero}")
    print(f"report: {args.out}\nanswers: {args.answers}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
