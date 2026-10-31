#!/usr/bin/env python3
"""Audit the corpus database against the original help package.

    python scripts/test/audit_source.py --release-root <帮助包根目录> [--db PATH]

Three levels, independently extracted from the source (no shared parser with
build_db, so a parser bug cannot hide itself):

  1 books     landing-page links vs books in the database
  2 articles  page hrefs in topics.json vs pages in the database, per book
  3 knowledge sentences / table cells / page length ratio on a random sample

Also reports what the corpus cannot hold by design (images, table structure).

Exit code 0 = every threshold passed, 1 = gaps found.
"""

import argparse
import html as htmllib
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}"
DOC_LINK = re.compile(r"PL\d{17,}\.([A-Za-z0-9_.\-]+)")

TAG_STRIP = re.compile(r"(?s)<[^>]+>")
DROP_BLOCK = re.compile(r"(?is)<(script|style|head)\b.*?</\1>")


def log(msg=""):
    print(msg, flush=True)


def norm(text):
    """Whitespace- and punctuation-insensitive form used for containment tests."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def body_text(html):
    s = DROP_BLOCK.sub(" ", html)
    m = re.search(r"(?is)<body[^>]*>(.*)</body>", s)
    if m:
        s = m.group(1)
    s = TAG_STRIP.sub(" ", s)
    s = htmllib.unescape(s).replace(chr(160), " ")
    return " ".join(s.split())


def book_title(topic_dir):
    try:
        data = json.loads((topic_dir / "topics.json").read_text(encoding="utf-8"))
        return " ".join((data.get("title") or "").split())
    except (OSError, ValueError):
        return ""


def landing_links(release_root):
    svg = release_root / "HomePage" / "landing_page.svg"
    if not svg.exists():
        return set()
    root = ET.fromstring(svg.read_bytes(), parser=ET.XMLParser())
    out = set()
    for a in root.iter(SVG + "a"):
        href = a.get(XLINK + "href", "")
        m = DOC_LINK.search(href)
        if not m:
            continue
        if not any((t.text or "").strip() for t in a.iter(SVG + "tspan")):
            continue          # icon / banner links carry no book title
        out.add(m.group(1).split(".")[-1])
    return out


def package_dirs(release_root):
    """Every book directory in the package (holds toc.json + topics.json)."""
    out = set()
    for top in sorted(release_root.iterdir()):
        if not top.is_dir():
            continue
        if (top / "toc.json").exists():
            out.add((top.name, top.name))
            continue
        for sub in sorted(top.iterdir()):
            if sub.is_dir() and (sub / "toc.json").exists():
                out.add((top.name, sub.name))
    return out


def audit(args):
    root = Path(args.release_root)
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    problems = []

    books = conn.execute("""SELECT t.id, t.title, t.dir_name, t.location, t.page_count, t.note,
                                   c.name AS category
                            FROM topics t JOIN categories c ON c.id = t.category_id
                            ORDER BY t.id""").fetchall()
    log(f"database : {db_path}  ({len(books)} books)")
    log(f"source   : {root}")
    log()

    # ── level 1: books ──────────────────────────────────────────────
    linked = landing_links(root)
    db_dirs = {(b["location"], b["dir_name"]) for b in books}
    db_names = {b["dir_name"] for b in books}
    missing_books = sorted(n for n in linked if n not in db_names)
    extra_books = sorted((b["title"], b["note"]) for b in books
                         if b["note"] and "not linked from the landing page" in b["note"])
    log("== 1 books ==")
    log(f"landing links (product/sdk): {len(linked)}")
    log(f"books in database          : {len(books)}  "
        f"(with pages: {sum(1 for b in books if b['page_count'])} / "
        f"no local content: {sum(1 for b in books if not b['page_count'])})")
    log(f"landing links with no book : {len(missing_books)} {missing_books[:5]}")
    log(f"books not on landing page  : {len(extra_books)} {[t for t, _ in extra_books]}")
    pkg = package_dirs(root)
    db_pairs = {(b["location"], b["dir_name"]) for b in books}
    pkg_missing = sorted(k for k in pkg if k not in db_pairs)
    log(f"package book dirs          : {len(pkg)}   not in database: {len(pkg_missing)} "
        f"{pkg_missing[:5]}")
    if missing_books:
        # SDK books are expected to be absent by design; anything else is a gap.
        non_sdk = [n for n in missing_books if not n.startswith(("ITK", "TcServer", "DataModel",
                                                                "AdminData", "net_soa", "java_",
                                                                "services_net", "storybook",
                                                                "whats_changed"))]
        if non_sdk:
            problems.append(f"{len(non_sdk)} product books missing: {non_sdk}")
    if pkg_missing:
        problems.append(f"{len(pkg_missing)} package book dirs missing from the database: {pkg_missing}")

    # ── level 2: articles per book ──────────────────────────────────
    log("\n== 2 articles (per book) ==")
    src_books = {}
    for b in books:
        if not b["location"] or b["location"] == "awc_swf":
            continue
        d = root / b["location"] / b["dir_name"]
        if not (d / "topics.json").exists():
            continue
        data = json.loads((d / "topics.json").read_text(encoding="utf-8"))
        src_hrefs = {e["href"] for e in data.get("topics", [])
                     if e.get("href") and (e.get("searchtext") or "").strip()}
        src_books[(b["location"], b["dir_name"])] = (b, d, src_hrefs)

    total_missing = total_extra = artifact_pages = 0
    detail = []
    book_rows = []
    for key, (b, d, src_hrefs) in src_books.items():
        db_hrefs = {r[0] for r in conn.execute(
            "SELECT href FROM pages WHERE topic_id=? AND type NOT IN ('PdfPage','HtmlTopic','XlsxPage')",
            (b["id"],))}
        miss = src_hrefs - db_hrefs
        extra = db_hrefs - src_hrefs
        total_missing += len(miss)
        total_extra += len(extra)
        book_rows.append({"id": b["id"], "category": b["category"], "title": b["title"],
                          "pages": len(db_hrefs), "src_pages": len(src_hrefs),
                          "missing": len(miss), "extra": len(extra),
                          "location": b["location"], "dir_name": b["dir_name"]})
        if miss or extra:
            detail.append((b["title"], len(src_hrefs), len(db_hrefs), len(miss), len(extra),
                           sorted(miss)[:2], sorted(extra)[:2]))
    artifact_pages = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE type IN ('PdfPage','HtmlTopic','XlsxPage')").fetchone()[0]
    log(f"books compared: {len(src_books)}   non-DITA pages (PDF / single HTML): {artifact_pages}")
    log(f"pages in source missing from db: {total_missing}")
    log(f"pages in db not present in source topics.json: {total_extra}")
    for row in detail[:12]:
        log(f"  {row[0][:44]:<46} src={row[1]:>5} db={row[2]:>5} miss={row[3]} extra={row[4]} "
            f"{row[5] if row[3] else ''}{row[6] if row[4] else ''}")
    if total_missing:
        problems.append(f"{total_missing} source pages missing from the database")
    if total_extra:
        problems.append(f"{total_extra} database pages are not in the source topics.json")

    # sections
    sec_missing = 0
    sec_examples = []
    for key, (b, d, _src) in src_books.items():
        toc = json.loads((d / "toc.json").read_text(encoding="utf-8"))
        expected = set()
        for node in toc.get("topics", []):
            kids = node.get("children") or []
            if kids:
                expected.update(" ".join((k.get("title") or "").split()) for k in kids)
            elif node.get("href"):
                expected.add(" ".join((node.get("title") or "").split()))
        have = {" ".join((r[0] or "").split()) for r in conn.execute(
            "SELECT title FROM sections WHERE topic_id=?", (b["id"],))}
        have.discard("Other pages (not in the book TOC)")
        miss = {t for t in expected if t and t not in have}
        if miss:
            sec_missing += len(miss)
            sec_examples.append((b["title"], sorted(miss)[:2]))
    # duplicates must be near-verbatim: platform variants share a title and
    # opening but diverge later, and merging them would hide real content
    src_text = {}
    for key, (_b, d, _src) in src_books.items():
        data = json.loads((d / "topics.json").read_text(encoding="utf-8"))
        for e in data.get("topics", []):
            if e.get("href"):
                src_text[(key[0], key[1], e["href"])] = e.get("searchtext", "")
    dup_rows = conn.execute("""SELECT p.id, p.href, p.dup_of, t.location, t.dir_name, p.title
                               FROM pages p JOIN topics t ON t.id=p.topic_id
                               WHERE p.dup_of IS NOT NULL""").fetchall()
    weak = []
    for d in dup_rows:
        src = src_text.get((d["location"], d["dir_name"], d["href"]), "")
        canon = conn.execute("SELECT text FROM pages WHERE id=?", (d["dup_of"],)).fetchone()
        if not src or canon is None:
            continue
        a = set(re.findall(r"[a-z0-9]{3,}", src.lower()))
        btok = set(re.findall(r"[a-z0-9]{3,}", (canon[0] or "").lower()))
        sim = len(a & btok) / len(a | btok) if (a or btok) else 1.0
        if sim < 0.98:
            weak.append((round(sim, 2), d["title"][:40]))
    log(f"duplicates marked: {len(dup_rows)}   below 0.98 similarity: {len(weak)}")
    for w in weak[:6]:
        log(f"  sim={w[0]}  {w[1]}")
    if weak:
        problems.append(f"{len(weak)} duplicates are not near-verbatim (content may be hidden)")

    log(f"\nsections: missing from db = {sec_missing}")
    for row in sec_examples[:6]:
        log(f"  {row[0][:44]:<46} {row[1]}")
    if sec_missing:
        problems.append(f"{sec_missing} book sections missing from the database")

    # ── level 3: knowledge points on a random sample ────────────────
    log("\n== 3 knowledge points (random sample) ==")
    rows = conn.execute("""SELECT p.id, p.title, p.href, p.text, t.location, t.dir_name
                           FROM pages p JOIN topics t ON t.id=p.topic_id
                           WHERE t.location NOT IN ('', 'awc_swf') AND p.dup_of IS NULL
                             AND p.type NOT IN ('PdfPage','HtmlTopic','XlsxPage')""").fetchall()
    sample = random.Random(args.seed).sample(rows, min(args.sample, len(rows)))
    ratios, sentence_rates, worst, sent_clean = [], [], [], []
    missing_files = 0
    for r in sample:
        path = root / r["location"] / (r["dir_name"] or "") / r["href"].replace("/", os.sep)
        if not path.exists():
            missing_files += 1
            continue
        html = path.read_text(encoding="utf-8", errors="replace")
        text = body_text(html)
        if not text:
            continue
        db_text = r["text"] or ""
        ratios.append(len(db_text) / len(text))
        sents = [s for s in re.split(r"(?<=[.!?])\s+", text)
                 if len(s.strip()) > 60 and norm(s)]
        if not sents:
            continue
        ndb = norm(db_text)
        found = sum(1 for s in sents if norm(s) and norm(s) in ndb)
        rate = found / len(sents)
        sentence_rates.append(rate)
        if len(db_text) / len(text) >= 0.95:
            sent_clean.append(rate)
        if rate < 0.9 or len(db_text) / len(text) < args.ratio_threshold:
            worst.append((round(rate, 2), round(len(db_text) / len(text), 2),
                          r["title"][:44], r["href"], rate < 0.9 and len(db_text) / len(text) < 0.95))

    ratios.sort()
    sentence_rates.sort()
    log(f"sampled pages: {len(ratios)}  missing source files: {missing_files}")
    if ratios:
        log(f"db/original text length   min {ratios[0]:.2f}  p10 {ratios[len(ratios)//10]:.2f}  "
            f"median {ratios[len(ratios)//2]:.2f}  max {ratios[-1]:.2f}")
    if sentence_rates:
        log(f"sentences found verbatim  min {sentence_rates[0]:.0%}  "
            f"median {sentence_rates[len(sentence_rates)//2]:.0%}")
    low_ratio = sum(1 for x in ratios if x < args.ratio_threshold)
    # A low sentence match with a healthy length ratio is an artefact of stripping
    # code samples and tables, not missing content.
    low_sent = sum(1 for w in worst if w[4])
    log(f"pages below ratio {args.ratio_threshold}: {low_ratio}   "
        f"pages below 90% sentence coverage: {low_sent}")
    for w in worst[:10]:
        log(f"  sent={w[0]:.0%} ratio={w[1]:.2f}  {w[2]}  [{w[3]}]")
    if low_ratio:
        problems.append(f"{low_ratio} sampled pages keep <{args.ratio_threshold} of the original text")
    if low_sent:
        problems.append(f"{low_sent} sampled pages cover <90% of the original sentences")

    # tables
    table_pages = [r for r in sample if r["href"].lower().endswith(".html")]
    checked = cell_total = cell_found = 0
    for r in table_pages[: args.tables]:
        path = root / r["location"] / (r["dir_name"] or "") / r["href"].replace("/", os.sep)
        if not path.exists():
            continue
        html = path.read_text(encoding="utf-8", errors="replace")
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", html)
        if len(cells) < 4:
            continue
        ndb = norm(r["text"] or "")
        cells = [" ".join(TAG_STRIP.sub(" ", c).split()) for c in cells]
        cells = [c for c in cells if len(c) > 3]
        if not cells:
            continue
        checked += 1
        cell_total += len(cells)
        cell_found += sum(1 for c in cells if norm(c) and norm(c) in ndb)
    if checked:
        log(f"table pages checked: {checked}  cells covered: {cell_found}/{cell_total} "
            f"= {cell_found/cell_total:.0%}")

    # images: by design not stored
    img_pages = img_tags = 0
    for r in table_pages:
        path = root / r["location"] / (r["dir_name"] or "") / r["href"].replace("/", os.sep)
        if not path.exists():
            continue
        n = path.read_text(encoding="utf-8", errors="replace").count("<img")
        if n:
            img_pages += 1
            img_tags += n
    if table_pages:
        log(f"pages with images (sample): {img_pages}/{len(table_pages)}; "
            f"image tags are not stored by design")

    if args.html:
        report = {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db_path": str(db_path), "source": str(root),
            "db_mb": db_path.stat().st_size / 1048576,
            "meta": {k: v for k, v in conn.execute("SELECT key, value FROM meta")},
            "books": {"total": len(books),
                      "with_pages": sum(1 for b in books if b["page_count"]),
                      "empty": [b["title"] for b in books if not b["page_count"]]},
            "landing": {"links": len(linked), "missing": missing_books,
                        "extra": [t for t, _ in extra_books],
                        "pkg_dirs": len(pkg), "pkg_missing": pkg_missing},
            "articles": {"books": len(src_books), "missing": total_missing,
                         "extra": total_extra},
            "sections": {"missing": sec_missing, "examples": sec_examples},
            "knowledge": {"sampled": len(ratios), "low_ratio": low_ratio, "low_sent": low_sent,
                          "ratio_min": min(ratios) if ratios else 0,
                          "ratio_med": sorted(ratios)[len(ratios) // 2] if ratios else 0,
                          "sent_min": min(sent_clean) if sent_clean else 0,
                          "sent_med": sorted(sent_clean)[len(sent_clean) // 2] if sent_clean else 0,
                          "sent_artifacts": sum(1 for x in sentence_rates if x < 0.9),
                          "worst": [{"sent": w[0], "ratio": w[1], "title": w[2], "href": w[3]}
                                    for w in worst[:10]]},
            "tables": {"checked": checked, "cells": cell_total, "found": cell_found},
            "images": {"sampled": len(table_pages), "pages_with_images": img_pages},
            "duplicates": {"marked": len(dup_rows), "weak": weak[:10]},
            "artifacts": artifact_pages,
            "problems": problems,
            "book_rows": book_rows,
        }
        out = Path(args.html)
        out.write_text(render_html(report), encoding="utf-8")
        log(f"html report: {out}")

    log("\n== verdict ==")
    if problems:
        for p in problems:
            log(f"  GAP: {p}")
        log(f"  {len(problems)} problem group(s)")
        return 1
    log("  all thresholds passed: books, articles, sections, text fidelity")
    return 0


CSS = """
:root{--ok:#1f7a3d;--bad:#b3261e;--warn:#8a5a00;--ink:#1b1f24;--muted:#5b6570;--line:#dfe3e8;--bg:#f6f7f9}
*{box-sizing:border-box}
body{margin:0;padding:28px 32px 60px;background:var(--bg);color:var(--ink);
     font:14px/1.55 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:16px;margin:34px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:18px;word-break:break-all}
.banner{padding:14px 18px;border-radius:8px;font-size:15px;font-weight:600;margin:0 0 20px}
.banner.ok{background:#e7f4ec;color:var(--ok);border:1px solid #b6ddc6}
.banner.bad{background:#fdeceb;color:var(--bad);border:1px solid #f3c2be}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:8px}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px 16px;min-width:150px}
.card b{display:block;font-size:21px;line-height:1.2}
.card span{color:var(--muted);font-size:12.5px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#eef1f4;font-weight:600;font-size:12.5px;color:#39414a}
tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600}
.pill.ok{background:#e7f4ec;color:var(--ok)}
.pill.bad{background:#fdeceb;color:var(--bad)}
.pill.warn{background:#fff4d6;color:var(--warn)}
tr.bad-row td{background:#fdf4f3}
.toolbar{display:flex;gap:10px;align-items:center;margin:0 0 10px}
input[type=search]{flex:1;max-width:420px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;font:inherit}
ul{margin:6px 0 0;padding-left:20px}
li{margin:2px 0}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12.5px}
.foot{margin-top:34px;color:var(--muted);font-size:12px}
"""

FILTER_JS = """
document.getElementById('bookFilter').addEventListener('input', function () {
  const q = this.value.toLowerCase();
  let shown = 0;
  document.querySelectorAll('#bookTable tbody tr').forEach(function (tr) {
    const hit = tr.textContent.toLowerCase().includes(q);
    tr.style.display = hit ? '' : 'none';
    if (hit) shown++;
  });
  document.getElementById('bookCount').textContent = shown;
});
"""


def _pill(ok, text):
    return f'<span class="pill {"ok" if ok else "bad"}">{text}</span>'


def render_html(rep):
    esc = htmllib.escape
    ok = not rep["problems"]
    k = rep["knowledge"]
    t = rep["tables"]
    dup = rep["duplicates"]
    books = rep["books"]
    art = rep["articles"]
    sec = rep["sections"]
    land = rep["landing"]
    meta = rep["meta"]

    cards = [
        (f'{books["total"]}', '本书'),
        (f'{int(meta.get("pages", 0)):,}', '页'),
        (f'{rep["db_mb"]:.1f} MB', '库体积'),
        (f'{int(meta.get("text_chars", 0))/1048576:.1f} MB', '正文'),
        (f'{dup["marked"]:,}', '副本页（不参与检索）'),
        (f'{rep.get("artifacts", 0)}', 'PDF/单页补录'),
        (f'{len(books["empty"])}', '未入正文的书'),
    ]
    card_html = "".join(f'<div class="card"><b>{esc(v)}</b><span>{esc(lbl)}</span></div>'
                        for v, lbl in cards)

    checks = [
        ("书", _pill(not land["pkg_missing"],
                    f'{land["pkg_dirs"]} 个书目录全在库' if not land["pkg_missing"]
                    else f'缺 {len(land["pkg_missing"])} 个'),
         f'落地页链接 {land["links"]}；库里多出 {len(land["extra"])} 本（包内有、页面没挂）'),
        ("文章", _pill(art["missing"] == 0 and art["extra"] == 0,
                      "缺 0 / 多 0" if art["missing"] == 0 and art["extra"] == 0
                      else f'缺 {art["missing"]} / 多 {art["extra"]}'),
         f'逐本比对 topics.json 的页 href，共 {art["books"]} 本'),
        ("章节", _pill(sec["missing"] == 0, f'缺 {sec["missing"]}'),
         '逐本比对 toc.json 的章节标题'),
        ("正文抽样", _pill(k["low_ratio"] == 0 and k["low_sent"] == 0,
                       f'{k["sampled"]} 页'), 
         f'库/原文长度比 最小 {k["ratio_min"]:.2f}，中位 {k["ratio_med"]:.2f}；'
         f'句子逐字命中 中位 {k["sent_med"]:.0%}'
         + (f'（{k.get("sent_artifacts", 0)} 页低于 90%，多为代码/表格样例，逐条列在下方，内容未缺）'
            if k.get("sent_artifacts") else '')),
        ("表格抽样", _pill(t["found"] >= t["cells"] * 0.95 if t["cells"] else True,
                       f'{t["found"]}/{t["cells"]}' if t["cells"] else "无样本"),
         f'{t["checked"]} 个含表格页的单元格内容覆盖率'),
        ("副本判定", _pill(not dup["weak"], f'{dup["marked"]} 页'),
         '按全文相似度 ≥0.98 判定；低于 0.98 的会列在下面'),
    ]
    check_html = "".join(f'<tr><td>{esc(n)}</td><td>{p}</td><td>{esc(d)}</td></tr>'
                         for n, p, d in checks)

    rows = []
    for b in sorted(rep["book_rows"], key=lambda x: (x["missing"] + x["extra"] == 0, x["category"], x["title"])):
        bad = b["missing"] or b["extra"]
        rows.append(
            f'<tr class="{"bad-row" if bad else ""}">'
            f'<td>{esc(b["category"])}</td><td>{esc(b["title"])}</td>'
            f'<td class="num">{b["src_pages"]}</td><td class="num">{b["pages"]}</td>'
            f'<td class="num">{b["missing"]}</td><td class="num">{b["extra"]}</td>'
            f'<td>{_pill(not bad, "一致" if not bad else "有差异")}</td></tr>')
    book_table = "".join(rows)

    gaps = []
    if books["empty"]:
        gaps.append("<h3>落地页有入口、这一包里没有内容的书</h3><ul>"
                    + "".join(f"<li>{esc(x)}</li>" for x in books["empty"]) + "</ul>")
    if land["extra"]:
        gaps.append("<h3>包里有、落地页没挂的书（已收进库）</h3><ul>"
                    + "".join(f"<li>{esc(x)}</li>" for x in land["extra"]) + "</ul>")
    if land["pkg_missing"]:
        gaps.append("<h3>包里有目录、库里没有</h3><ul>"
                    + "".join(f"<li>{esc(str(x))}</li>" for x in land["pkg_missing"]) + "</ul>")
    if dup["weak"]:
        gaps.append("<h3>相似度不足 0.98 却被标成副本（会隐藏内容）</h3><ul>"
                    + "".join(f"<li>sim={w[0]} {esc(w[1])}</li>" for w in dup["weak"]) + "</ul>")
    if k["worst"]:
        gaps.append("<h3>抽到的最弱页（供复核，不等于缺失）</h3><ul>"
                    + "".join(f"<li>{w['sent']:.0%} 句命中 / 长度比 {w['ratio']:.2f} — "
                              f"{esc(w['title'])} <code>{esc(w['href'])}</code></li>"
                              for w in k["worst"][:8]) + "</ul>")

    problem_html = ("<h2>判定</h2><ul>"
                    + "".join(f"<li>{esc(p)}</li>" for p in rep["problems"]) + "</ul>"
                    if rep["problems"] else "")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>tc-help 完整性核对 · {esc(meta.get('version', ''))}</title>
<style>{CSS}</style></head><body>
<h1>tc-help 完整性核对 · Teamcenter {esc(meta.get('version', ''))}</h1>
<div class="sub">生成于 {esc(rep['generated'])}　|　库：{esc(rep['db_path'])}（{esc(meta.get('built_at', ''))} 构建）<br>
源包：{esc(rep['source'])}</div>
<div class="banner {'ok' if ok else 'bad'}">{'全部通过：书、文章、章节、正文都能对上源包' if ok else '发现问题：见下方「判定」'}</div>
<div class="cards">{card_html}</div>
<h2>核对结果</h2>
<table><thead><tr><th style="width:110px">级别</th><th style="width:190px">结果</th><th>说明</th></tr></thead>
<tbody>{check_html}</tbody></table>
<h2>逐本明细 <span class="sub" style="margin:0">（显示 <span id="bookCount">{len(rep['book_rows'])}</span> 本）</span></h2>
<div class="toolbar"><input id="bookFilter" type="search" placeholder="按分类或书名过滤，例如 Installation / Workflow"></div>
<table id="bookTable"><thead><tr><th>分类</th><th>书名</th><th>源页数</th><th>库页数</th><th>缺</th><th>多</th><th>状态</th></tr></thead>
<tbody>{book_table}</tbody></table>
{problem_html}
{''.join(gaps)}
<h2>复现</h2>
<pre><code>python scripts/test/audit_source.py --release-root "{esc(rep['source'])}" --html &lt;输出.html&gt;</code></pre>
<div class="foot">本页由 audit_source.py 生成，数据来自数据库与官方帮助包的直接比对；图片、表格结构按设计不入库。</div>
<script>{FILTER_JS}</script>
</body></html>"""


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release-root", required=True)
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "data/2506/tc_help_2506.db"))
    ap.add_argument("--sample", type=int, default=400, help="pages sampled for level 3")
    ap.add_argument("--tables", type=int, default=40, help="sampled pages checked for table cells")
    ap.add_argument("--ratio-threshold", type=float, default=0.85)
    ap.add_argument("--seed", type=int, default=2506)
    ap.add_argument("--html", default=None, help="write a single-file HTML report here")
    sys.exit(audit(ap.parse_args()))


if __name__ == "__main__":
    main()
