#!/usr/bin/env python3
"""Build the tc-help corpus database from a Teamcenter help package.

    python scripts/data/build_db.py --release-root <帮助包根目录> \
        --out data/2506/tc_help_2506.db --version 2506 [--swf-dir <dir>]

The output database holds the English original page text only: no translations,
no summaries. Every run asserts the corpus is CM-free and prints coverage.
Standard library only, so the script can travel with the skill to other releases.
"""

import argparse
import html
import json
import re
import sqlite3
import subprocess
import sys
import zipfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}"
PL_PREFIX = re.compile(r"(PL\d{17,})\.([A-Za-z0-9_.\-]+)")
PL_MAIN_PREFIX = "PL2024"      # the product help release; PL2025* is the SDK release
MIRROR_BASE = "http://127.0.0.1:5000/documentation/external"
# Books that exist only as a file next to the DITA tree (no toc/topics.json).
ARTIFACT_DIRS = {
    "dep_ref_arch": "tc_ref_architecture",
    "troubleshooting": "troubleshooting",
    "foundation": "tc_dc",
    "tc_dc_sheet": "tc_dc",
    "azure1": "azure", "azure2": "azure", "azure3": "azure",
}
# Filenames that do not share words with their book title.
ARTIFACT_PICK = {"foundation": "Teamcenter_Foundation_2506.html"}

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Build-time annotations some helpers inject above the original text.
ANNOTATION = re.compile(r"^>\s*(?:[\u6765\u6e90]|Source)\s*[:：]\s*https?://\S+\s*$")


def log(msg):
    print(msg, flush=True)


# ── landing page ─────────────────────────────────────────────────────

def parse_landing(svg_path):
    """Return (categories, loose_links).

    categories: [{svg_id, name, topics:[{title, doc_id, location_hint}]}]
    loose_links: topic links that sit outside every category box.

    The SVG comes from the licensed local help package, not from the network.
    """
    if svg_path.stat().st_size > 50 * 1024 * 1024:
        sys.exit(f"refusing to parse oversized landing page: {svg_path}")
    parser = ET.XMLParser()
    root = ET.fromstring(svg_path.read_bytes(), parser=parser)
    categories = []
    boxed_a = set()

    for g in root.iter(SVG + "g"):
        rect = g.find(SVG + "rect")
        box_id = rect.get("id") if rect is not None else None
        if not box_id or not box_id.endswith("box"):
            continue
        name = None
        for tspan in g.iter(SVG + "tspan"):
            if tspan.get("id") == "catTitle" and (tspan.text or "").strip():
                name = " ".join((tspan.text or "").split())
                break
        topics = []
        for a in g.iter(SVG + "a"):
            href = a.get(XLINK + "href", "")
            if not href or href.startswith("#"):
                continue
            m = PL_PREFIX.search(href)
            if not m:
                continue
            title = anchor_title(a)
            if not title:
                continue
            boxed_a.add(a)
            topics.append({"title": title, "doc_id": m.group(2), "prefix": m.group(1)})
        if name:
            categories.append({"svg_id": box_id, "name": name, "topics": topics})

    loose = []
    for a in root.iter(SVG + "a"):
        if a in boxed_a:
            continue
        href = a.get(XLINK + "href", "")
        m = PL_PREFIX.search(href)
        if not m:
            continue
        title = anchor_title(a)
        if title:
            loose.append({"title": title, "doc_id": m.group(2), "prefix": m.group(1)})
    return categories, loose


# ── topic directories ───────────────────────────────────────────────

def anchor_title(node):
    """Visible text of an <a>; category boxes use <tspan>, header links use <text>."""
    chunks = [t.text or "" for t in node.iter()
              if t.tag in (SVG + "tspan", SVG + "text")]
    return " ".join(" ".join(chunks).split())


def index_topic_dirs(release_root):
    """{(collection, topic_dir_name): path} for every dir holding toc.json."""
    index = {}
    for top in sorted(release_root.iterdir()):
        if not top.is_dir() or top.name == "HomePage":
            continue
        if (top / "toc.json").exists():
            index[(top.name, top.name)] = top
            continue
        for sub in sorted(top.iterdir()):
            if sub.is_dir() and (sub / "toc.json").exists():
                index[(top.name, sub.name)] = sub
    return index


def walk_sections(toc_data):
    sections = []
    for node in toc_data.get("topics", []):
        children = node.get("children") or []
        if not children:
            if node.get("href"):
                sections.append({"title": node.get("title", ""), "href": node.get("href", ""),
                                 "ids": [node["id"]] if node.get("id") else []})
            continue
        for child in children:
            ids = []
            collect_ids(child, ids)
            sections.append({"title": child.get("title", ""), "href": child.get("href", ""), "ids": ids})
    return sections


def collect_ids(node, out):
    if node.get("href") and node.get("id"):
        out.append(node["id"])
    for child in node.get("children") or []:
        collect_ids(child, out)


def clean_text(text):
    """Drop injected build annotations; keep the original wording otherwise."""
    if not text:
        return ""
    kept = [ln for ln in text.splitlines() if not ANNOTATION.match(ln.strip())]
    return "\n".join(kept).strip()


def html_to_text(path):
    s = path.read_text(encoding="utf-8", errors="replace")
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?is)</(p|div|h1|h2|h3|h4|tr|table|ul|ol|pre)>", "\n", s)
    s = re.sub(r"(?is)<t[dh][^>]*>", " | ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    lines = [" ".join(ln.split()) for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ── database ────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE categories (id INTEGER PRIMARY KEY, svg_id TEXT, name TEXT);
CREATE TABLE topics (id INTEGER PRIMARY KEY, category_id INTEGER, title TEXT,
                     dir_name TEXT, location TEXT, page_count INTEGER DEFAULT 0, note TEXT);
CREATE TABLE sections (id INTEGER PRIMARY KEY, topic_id INTEGER, title TEXT, href TEXT);
CREATE TABLE pages (id INTEGER PRIMARY KEY, topic_id INTEGER, section_id INTEGER, title TEXT,
                    title_key TEXT, href TEXT, type TEXT, text TEXT, source_url TEXT, dup_of INTEGER);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_pages_topic ON pages(topic_id);
CREATE INDEX idx_pages_section ON pages(section_id);
CREATE INDEX idx_pages_href ON pages(href);
CREATE INDEX idx_pages_dup ON pages(dup_of);
CREATE INDEX idx_sections_topic ON sections(topic_id);
CREATE INDEX idx_pages_title_key ON pages(title_key);
CREATE VIRTUAL TABLE pages_fts USING fts5(title, text, content='pages', content_rowid='id',
                                          tokenize="unicode61 remove_diacritics 2");
"""

# Curated lookup rows live beside the page corpus; they can be refreshed on their
# own (--indexes-only) without re-ingesting 24k pages.
INDEX_SCHEMA = """
CREATE TABLE handlers (name TEXT PRIMARY KEY, kind TEXT, placement TEXT, args TEXT,
                       summary TEXT, page_id INTEGER, topic_id INTEGER, href TEXT);
CREATE TABLE utilities (name TEXT PRIMARY KEY, args TEXT, summary TEXT,
                        page_id INTEGER, topic_id INTEGER, href TEXT);
"""

# Preference definitions come from the Administration Data Report that ships as a
# developer reference next to the product help; they are loaded on their own too.
PREFS_SCHEMA = """
CREATE TABLE prefs (name TEXT PRIMARY KEY, category TEXT, description TEXT, data_type TEXT,
                    scope TEXT, is_array TEXT, env_enabled TEXT, default_value TEXT, page TEXT);
CREATE INDEX idx_prefs_category ON prefs(category);
"""

# Saved Queries from the same report: one row per query, clauses kept verbatim.
QUERIES_SCHEMA = """
CREATE TABLE saved_queries (name TEXT PRIMARY KEY, description TEXT, query_class TEXT,
                            query_type TEXT, clauses TEXT, indented TEXT, owner TEXT,
                            group_id TEXT, page TEXT);
CREATE INDEX idx_sq_class ON saved_queries(query_class);
"""


def resolve_doc(doc_id, dirs):
    """Map a landing-page doc id to (collection, topic_dir_name, path)."""
    if "." in doc_id:                      # e.g. ewi_2312_en_US_help.EWI_DeploymentGuide
        coll, name = doc_id.split(".", 1)
        path = dirs.get((coll, name))
        return (coll, name, path) if path else (None, None, None)
    for (coll, name), path in dirs.items():
        if name == doc_id:
            return coll, name, path
    return None, None, None


def book_title(topic_dir):
    try:
        data = json.loads((topic_dir / "topics.json").read_text(encoding="utf-8"))
        return " ".join((data.get("title") or "").split())
    except (OSError, ValueError):
        return ""


def build(args):
    t0 = time.time()
    root = Path(args.release_root)
    out = Path(args.out)
    svg_path = root / "HomePage" / "landing_page.svg"
    if not svg_path.exists():
        sys.exit(f"landing page not found: {svg_path}")

    categories, loose = parse_landing(svg_path)
    if loose:
        categories.append({"svg_id": "loose", "name": "Release Notes", "topics": loose})
    dirs = index_topic_dirs(root)
    log(f"landing: {len(categories)} categories, "
        f"{sum(len(c['topics']) for c in categories)} links; {len(dirs)} book dirs")

    # ── plan: one row per unique book, first landing category wins ──
    order, first_cat, extra_cats = [], {}, {}
    sdk_skipped = []
    linked = set()
    for cat in categories:
        for t in cat["topics"]:
            if not t["prefix"].startswith(PL_MAIN_PREFIX) and t["doc_id"] != "swf":
                sdk_skipped.append(t["title"])
                continue
            coll, name, path = resolve_doc(t["doc_id"], dirs)
            if path is not None:
                linked.add((coll, name))
            key = (coll, name) if path is not None else ("~missing", t["doc_id"])
            if key not in first_cat:
                first_cat[key] = t["title"]
                order.append({"key": key, "title": t["title"], "cat": cat["name"],
                              "collection": coll, "dir_name": name, "path": path, "note": ""})
            else:
                extra_cats.setdefault(key, set()).add(cat["name"])
    for row in order:
        others = sorted(extra_cats.get(row["key"], set()) - {row["cat"]})
        if others:
            row["note"] = "also linked from " + ", ".join(others)

    # Books present in the package but missing from the landing page (e.g. What's New).
    orphan_rows = []
    for (coll, name), path in dirs.items():
        if (coll, name) in linked or name == "common":
            continue
        orphan_rows.append({"key": (coll, name), "title": book_title(path) or name,
                            "cat": "Release Notes", "collection": coll, "dir_name": name,
                            "path": path, "note": "in the package, not linked from the landing page"})
    if orphan_rows:
        log(f"orphan books picked up: {len(orphan_rows)} -> "
            + "; ".join(r["title"] for r in orphan_rows))

    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(out))
    conn.executescript(SCHEMA + INDEX_SCHEMA + PREFS_SCHEMA + QUERIES_SCHEMA)

    cat_ids = {}
    for cat in categories + ([{"svg_id": "releases", "name": "Release Notes"}] if orphan_rows else []):
        if cat["name"] in cat_ids:
            continue
        cur = conn.execute("INSERT INTO categories(svg_id,name) VALUES (?,?)",
                           (cat["svg_id"], cat["name"]))
        cat_ids[cat["name"]] = cur.lastrowid

    stats = {"topics": 0, "empty": [], "pages": 0, "orphan_pages": 0, "skipped_no_text": 0}
    hashes = {}
    topic_id = 0

    for row in order + orphan_rows:
        topic_id += 1
        stats["topics"] += 1
        dir_name = row["dir_name"] or row["key"][1]
        is_swf = dir_name == "swf" and row["path"] is None
        location = "awc_swf" if is_swf else (row["collection"] or "")
        note = row["note"]
        conn.execute("INSERT INTO topics(id,category_id,title,dir_name,location,note) VALUES (?,?,?,?,?,?)",
                     (topic_id, cat_ids[row["cat"]], row["title"], dir_name, location, note))

        topic_pages = 0
        if is_swf:
            topic_pages = ingest_swf(conn, args.swf_dir, topic_id, args) if args.swf_dir else 0
            if not topic_pages:
                note = "SWF pages not available on this machine"
        elif row["path"] is not None:
            topic_pages, orphan, skipped = ingest_topic(conn, row["path"], topic_id, location,
                                                        dir_name, args, hashes)
            stats["orphan_pages"] += orphan
            stats["skipped_no_text"] += skipped
        else:
            # Some books ship only as PDF (or a single HTML/Excel file), e.g.
            # Deployment Reference Architecture. Ingest their text when a
            # build-time extractor is available; otherwise say so plainly.
            topic_pages, artifact_note = ingest_artifact(conn, root, topic_id, row["title"],
                                                         dir_name, args)
            note = (note + "; " if note else "") + artifact_note
            if not topic_pages:
                stats["empty"].append(row["title"])

        conn.execute("UPDATE topics SET page_count=?, note=? WHERE id=?", (topic_pages, note, topic_id))
        stats["pages"] += topic_pages
        conn.commit()

    stats["redacted"] = redact_secrets(conn)
    conn.execute("INSERT INTO pages_fts(rowid,title,text) SELECT id,title,text FROM pages")
    conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('optimize')")
    stats["handlers"], stats["utilities"] = ingest_indexes(conn, args.indexes)
    if args.release_root:
        ingest_extra_handlers(conn, args.release_root)
    stats["handlers"] = conn.execute("SELECT COUNT(*) FROM handlers").fetchone()[0]
    stats["prefs"] = ingest_prefs(conn, args.admin_report)
    stats["queries"] = ingest_queries(conn, args.admin_report)
    stats["deduped"] = dedupe_handler_copies(conn)
    conn.commit()
    conn.execute("VACUUM")

    total, dups = conn.execute("SELECT COUNT(*), SUM(dup_of IS NOT NULL) FROM pages").fetchone()
    text_chars = conn.execute("SELECT SUM(LENGTH(text)) FROM pages").fetchone()[0] or 0
    cjk_rows = sum(1 for _, text in conn.execute("SELECT id, text FROM pages")
                   if text and CJK.search(text))
    meta = {
        "version": args.version, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "release_source": f"{MIRROR_BASE}/{args.release_sku or root.parent.name}/en-US", "product": args.product,
        "release_sku": args.release_sku or root.parent.name,
        "categories": str(len(cat_ids)), "topics": str(stats["topics"]),
        "pages": str(total), "duplicates": str(dups or 0), "text_chars": str(text_chars),
        "sdk_docs_skipped": str(len(sdk_skipped)),
        "handlers": str(stats.get("handlers", 0)),
        "utilities": str(stats.get("utilities", 0)),
        "prefs": str(stats.get("prefs", 0)),
        "queries": str(stats.get("queries", 0)),
    }
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta.items())
    conn.commit()
    conn.close()

    if cjk_rows:
        sys.exit(f"FAIL: {cjk_rows} pages still contain CJK text; inspect before shipping")

    log("")
    log(f"books       {stats['topics']}  (no local content: {len(stats['empty'])})")
    log(f"pages       {total:,}  duplicates marked: {dups or 0}")
    log(f"orphans     {stats['orphan_pages']} pages outside the book TOC")
    log(f"skipped     {stats['skipped_no_text']} entries without text, "
        f"{len(sdk_skipped)} SDK books (development docs, not part of this corpus)")
    log(f"text        {text_chars/1048576:.1f} MB chars")
    log(f"redacted    {stats['redacted']} pages with sample credentials masked")
    log(f"deduped     {stats.get('deduped', 0)} cross-book handler copies marked")
    log(f"db size     {out.stat().st_size/1048576:.1f} MB")
    log(f"elapsed     {time.time()-t0:.1f}s -> {out}")
    if stats["empty"]:
        log("no local content: " + "; ".join(stats["empty"]))


SECTION = re.compile(r"(?is)<section[^>]*>\s*<h2[^>]*>(.*?)</h2>(.*?)</section>")
DT = re.compile(r"(?is)<dt[^>]*>(.*?)</dt>")
PARA = re.compile(r"(?is)<p[^>]*>(.*?)</p>")


def parse_handler_html(path):
    """One handler page -> name/description/placement/arguments.

    These pages share the DITA section shape (Description, Syntax, Arguments,
    Placement, ...); the corpus text is flattened, so parse the source HTML.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", raw)
    fields = {"name": _plain(h1.group(1)) if h1 else "", "description": "",
              "placement": "", "args": []}
    for m in SECTION.finditer(raw):
        title = _plain(m.group(1)).lower()
        body = m.group(2)
        if title == "description":
            paras = [_plain(x) for x in PARA.findall(body)]
            fields["description"] = " ".join(x for x in paras if x)[:2000]
        elif title == "placement":
            fields["placement"] = _plain(body)[:1000]
        elif title == "arguments":
            for dt in DT.findall(body):
                arg = _plain(dt).split("=")[0].split(" ")[0].strip()
                if arg.startswith("-") and arg not in fields["args"]:
                    fields["args"].append(arg)
    return fields


def ingest_extra_handlers(conn, release_root):
    """Add handler pages the curated TSV does not cover (IPP and similar).

    The TSV only indexes the Workflow Designer handler chapters; other books
    carry handler pages too. Parse those from the same help package so the
    handler lookup covers them with placement/arguments.
    """
    if not release_root:
        log("extra handl skipped (no --release-root)")
        return 0, 0
    root = Path(release_root)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""SELECT p.id AS page_id, p.topic_id, p.title, p.href,
                                  t.location, t.dir_name, s.title AS section
                           FROM pages p JOIN topics t ON t.id=p.topic_id
                           JOIN sections s ON s.id=p.section_id
                           WHERE s.title IN ('Action handlers','Rule handlers')
                             AND p.dup_of IS NULL
                             AND p.id NOT IN (SELECT page_id FROM handlers)""").fetchall()
    added = skipped = 0
    for row in rows:
        src = root / (row["location"] or "") / (row["dir_name"] or "") / row["href"]
        if not src.is_file():
            skipped += 1
            continue
        fields = parse_handler_html(src)
        name = fields["name"] or row["title"]
        kind = "Rule Handler" if "rule" in (row["section"] or "").lower() else "Action Handler"
        cur = conn.execute(
            """INSERT OR IGNORE INTO handlers
               (name, kind, placement, args, summary, page_id, topic_id, href)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, kind, fields["placement"], ", ".join(fields["args"]),
             fields["description"], row["page_id"], row["topic_id"], row["href"]))
        added += cur.rowcount
    log(f"extra handl {added} handler pages parsed"
        + (f", {skipped} source pages missing" if skipped else ""))
    return added, skipped


def dedupe_handler_copies(conn):
    """Mark the cross-book copy of a structured handler page.

    The ingest-time dedupe uses a 0.98 token-Jaccard bar. Handler pages that
    differ only by the book name sit at ~0.968, so they survived as separate
    results. Once the handler index is loaded, the canonical page is known, and
    the other book's near-identical page can be marked without touching text.
    """
    conn.row_factory = sqlite3.Row
    marked = 0
    for row in conn.execute("""SELECT h.page_id AS canon_id, p.title, p.text, p.topic_id
                               FROM handlers h JOIN pages p ON p.id=h.page_id""").fetchall():
        tokens = text_tokens(row["text"])
        if not tokens:
            continue
        for other in conn.execute("""SELECT id, text FROM pages
                                     WHERE title=? AND id<>? AND topic_id<>? AND dup_of IS NULL""",
                                  (row["title"], row["canon_id"], row["topic_id"])).fetchall():
            other_tokens = text_tokens(other["text"])
            if other_tokens and len(tokens & other_tokens) / len(tokens | other_tokens) >= 0.95:
                conn.execute("UPDATE pages SET dup_of=? WHERE id=?", (row["canon_id"], other["id"]))
                marked += 1
    return marked


def ingest_topic(conn, topic_dir, topic_id, location, dir_name, args, hashes):
    """Pages for one book. Returns (page_count, orphan_pages, skipped_no_text)."""
    toc = json.loads((topic_dir / "toc.json").read_text(encoding="utf-8"))
    data = json.loads((topic_dir / "topics.json").read_text(encoding="utf-8"))
    entries = [e for e in data.get("topics", []) if e.get("href")]
    by_id = {e["id"]: e for e in entries if e.get("id")}
    used = set()
    pages = 0
    skipped = 0

    def add_page(entry, section_id):
        nonlocal pages, skipped
        text = clean_text(entry.get("searchtext", "") or "")
        if not text:
            skipped += 1
            return
        href = entry.get("href", "")
        title = entry.get("title", "")
        tokens = text_tokens(text)
        # A copy must be near-verbatim. Platform variants (Windows / Linux /
        # Deployment Center) share the same title and opening but diverge later;
        # merging them would hide real content, so the bar is 0.98.
        dup_of = None
        if len(tokens) >= 30:
            for cid, ctokens in hashes.get(title.strip().lower(), ()):
                if len(tokens & ctokens) / len(tokens | ctokens) >= 0.98:
                    dup_of = cid
                    break
        # Copies keep title/href for book navigation but not the body text.
        stored = "" if dup_of else text
        cur = conn.execute(
            "INSERT INTO pages(topic_id,section_id,title,title_key,href,type,text,source_url,dup_of) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (topic_id, section_id, title, title_key_of(title), href, entry.get("type", ""),
             stored, None, dup_of))
        if dup_of is None and len(tokens) >= 30:
            hashes.setdefault(title.strip().lower(), []).append((cur.lastrowid, tokens))
        pages += 1

    for sec in walk_sections(toc):
        cur = conn.execute("INSERT INTO sections(topic_id,title,href) VALUES (?,?,?)",
                           (topic_id, sec["title"], sec["href"]))
        section_id = cur.lastrowid
        for pid in sec["ids"]:
            entry = by_id.get(pid)
            if entry is None or pid in used:
                continue
            used.add(pid)
            add_page(entry, section_id)

    leftovers = [e for e in entries if e.get("id") not in used and
                 (e.get("id") or e.get("href")) not in used]
    if leftovers:
        cur = conn.execute("INSERT INTO sections(topic_id,title,href) VALUES (?,?,?)",
                           (topic_id, "Other pages (not in the book TOC)", ""))
        section_id = cur.lastrowid
        for entry in leftovers:
            add_page(entry, section_id)
    return pages, len(leftovers), skipped


def ingest_pdf(conn, path, topic_id, book_title, args):
    """Text of a PDF-only book, page by page. Needs an extractor at build time only."""
    exe = args.pdf_extractor
    try:
        proc = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0:
        return 0
    text = proc.stdout.decode("utf-8", errors="replace")
    pages = 0
    boilerplate = re.compile(r"(?i)unpublished work|trade secrets|all rights reserved")
    for idx, chunk in enumerate(text.split("\f"), 1):
        body = clean_text(chunk)
        if len(body) < 200:
            continue
        # Covers and legal notices are not answer material.
        if boilerplate.search(body[:300]):
            continue
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        title = first[:90] if first and len(first) <= 90 else f"{book_title} p{idx}"
        conn.execute(
            "INSERT INTO pages(topic_id,section_id,title,title_key,href,type,text,source_url,dup_of)"
            " VALUES (?,?,?,?,?,?,?,?,NULL)",
            (topic_id, None, title, title_key_of(title), f"pdf/{path.name}/p{idx}",
             "PdfPage", body, None))
        pages += 1
    if pages:
        cur = conn.execute("INSERT INTO sections(topic_id,title,href) VALUES (?,?,?)",
                           (topic_id, "PDF pages", f"pdf/{path.name}"))
        conn.execute("UPDATE pages SET section_id=? WHERE topic_id=? AND section_id IS NULL",
                     (cur.lastrowid, topic_id))
    return pages


SML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def ingest_xlsx(conn, path, topic_id, book_title, rows_per_page=40):
    """Rows of a spreadsheet book, chunked so results stay readable."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        return 0
    lines = []
    with zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(SML + "si"):
                shared.append("".join(t.text or "" for t in si.iter(SML + "t")))
        sheet_names = [n for n in zf.namelist()
                       if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        for sheet in sorted(sheet_names):
            root = ET.fromstring(zf.read(sheet))
            for row in root.findall(".//" + SML + "row"):
                cells = []
                for c in row.findall(SML + "c"):
                    val = None
                    if c.get("t") == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter(SML + "t"))
                    else:
                        v = c.find(SML + "v")
                        if v is not None and v.text:
                            val = v.text
                            if c.get("t") == "s":
                                try:
                                    val = shared[int(val)]
                                except (ValueError, IndexError):
                                    pass
                    if val:
                        cells.append(" ".join(str(val).split()))
                line = " | ".join(x for x in cells if x)
                if line:
                    lines.append(line)
    if not lines:
        return 0
    pages = 0
    for start in range(0, len(lines), rows_per_page):
        chunk = lines[start:start + rows_per_page]
        title = f"{book_title} - rows {start + 1}-{start + len(chunk)}"
        conn.execute(
            "INSERT INTO pages(topic_id,section_id,title,title_key,href,type,text,source_url,dup_of)"
            " VALUES (?,?,?,?,?,?,?,?,NULL)",
            (topic_id, None, title, title_key_of(title),
             f"xlsx/{path.name}/r{start + 1}", "XlsxPage", chr(10).join(chunk), None))
        pages += 1
    if pages:
        cur = conn.execute("INSERT INTO sections(topic_id,title,href) VALUES (?,?,?)",
                           (topic_id, "Workbook rows", f"xlsx/{path.name}"))
        conn.execute("UPDATE pages SET section_id=? WHERE topic_id=? AND section_id IS NULL",
                     (cur.lastrowid, topic_id))
    return pages


def ingest_artifact(conn, root, topic_id, book_title, dir_name, args):
    """Books that ship only as PDF / single HTML / spreadsheet."""
    cand_dir = ARTIFACT_DIRS.get(dir_name or "")
    folder = root / cand_dir if cand_dir else None
    if folder is None or not folder.is_dir():
        return 0, "landing page links it, but this package has no content for it"
    files = [p for p in sorted(folder.iterdir()) if p.is_file()]
    if not files:
        return 0, "landing page links it, but this package has no content for it"

    def words(s):
        return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())
                if w not in {"the", "and", "for", "with", "teamcenter", "guide"}}

    want = words(book_title)
    fixed = ARTIFACT_PICK.get(dir_name or "")
    if fixed and (folder / fixed).is_file():
        best = folder / fixed
    else:
        best = max(files, key=lambda p: len(want & words(p.stem)))
    suffix = best.suffix.lower()

    if suffix == ".pdf":
        pages = ingest_pdf(conn, best, topic_id, book_title, args)
        if pages:
            return pages, f"PDF text indexed ({best.name}, {pages} pages)"
        return 0, f"PDF only, not indexed ({best.name})"
    if suffix in (".xlsx", ".xlsm"):
        pages = ingest_xlsx(conn, best, topic_id, book_title)
        if pages:
            return pages, f"spreadsheet rows indexed ({best.name}, {pages} pages)"
        return 0, f"spreadsheet without readable rows ({best.name})"
    if suffix in (".html", ".htm"):
        body = html_to_text(best)
        if body:
            conn.execute(
                "INSERT INTO pages(topic_id,section_id,title,title_key,href,type,text,source_url,dup_of)"
                " VALUES (?,?,?,?,?,?,?,?,NULL)",
                (topic_id, None, book_title, title_key_of(book_title),
                 f"file/{best.name}", "HtmlTopic", body, None))
            return 1, f"single HTML page indexed ({best.name})"
        return 0, f"HTML without text ({best.name})"
    return 0, f"only a spreadsheet, not indexed ({best.name})"


def ingest_swf(conn, swf_dir, topic_id, args):
    src = Path(swf_dir)
    if not src.is_dir():
        return 0
    files = sorted(src.glob("*.html"))
    if not files:
        return 0
    cur = conn.execute("INSERT INTO sections(topic_id,title,href) VALUES (?,?,?)",
                       (topic_id, "All topics", ""))
    section_id = cur.lastrowid
    pages = 0
    for f in files:
        text = html_to_text(f)
        if not text:
            continue
        title = f.stem.replace("_", " ")
        m = re.search(r"(?is)<title>(.*?)</title>", f.read_text(encoding="utf-8", errors="replace"))
        if m and m.group(1).strip():
            title = " ".join(m.group(1).split())
        # No online address is derivable for these pages, so source_url stays NULL;
        # help.py labels SWF pages instead of inventing a docs.sw.siemens.com link.
        conn.execute("INSERT INTO pages(topic_id,section_id,title,title_key,href,type,text,source_url,dup_of)"
                     " VALUES (?,?,?,?,?,?,?,NULL,NULL)",
                     (topic_id, section_id, title, title_key_of(title), f"swf/{f.name}",
                      "SwfTopic", text))
        pages += 1
    return pages


API_KEY = re.compile(r"(?i)(--api-key\s+)[A-Za-z0-9+/=_-]{20,}")


# Tags only: a clause like "Identifier<-altid_of.idfr_id" is text, not markup,
# so a bare "<anything>" strip would eat it (it silently did, for 47 queries).
TAG = re.compile(r"</?[A-Za-z][^>]*>|<!--.*?-->", re.S)


def _plain(fragment):
    return " ".join(html.unescape(TAG.sub(" ", fragment or "")).split())


def parse_report_page(path):
    """One Administration Data Report page -> property dict.

    The report marks every property with the same two CSS classes, so the labels
    (Name/Description/Query Clauses/Overrides/...) come out without a full HTML
    parser; table-valued properties keep their rows. Long values are split over
    several blocks, so blocks are joined.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    fields = {}
    for section in re.split(r'<DIV id="PropertySection"', raw)[1:]:
        label = re.search(r'class="SiemensPropertyLabel"[^>]*>(.*?)</DIV>', section, re.S)
        if not label:
            continue
        key = _plain(label.group(1)).rstrip(":")
        if 'class="tableRow"' in section:
            heads = [_plain(x) for x in
                     re.findall(r'class="tableHeaderCell"[^>]*>(.*?)</DIV>', section, re.S)]
            cells = [_plain(x) for x in
                     re.findall(r'class="tableCell"[^>]*>(.*?)</DIV>', section, re.S)]
            width = len(heads) or 3
            fields[key] = [cells[i:i + width] for i in range(0, len(cells), width)]
        else:
            blocks = [_plain(x) for x in
                      re.findall(r'class="SiemensPropertyValueBlock"[^>]*>(.*?)</DIV>', section, re.S)]
            fields[key] = " ".join(x for x in blocks if x)
    return fields


def _report_page(path, folder):
    try:
        sku = path.parents[3].name
    except IndexError:
        sku = ""
    return f"{sku}/en-US/AdminDataReport/{folder}/{path.name}"


def ingest_prefs(conn, report_dir):
    """Load every preference definition from the Administration Data Report."""
    if not report_dir:
        log("prefs       skipped (no --admin-report); preference lookup unavailable")
        return 0
    folder = Path(report_dir) / "Preferences"
    files = sorted(folder.glob("Fnd0PreferenceDefinition_*.html"))
    if not files:
        sys.exit(f"no preference pages under {folder}")
    conn.row_factory = sqlite3.Row
    rows = missing = 0
    for path in files:
        fields = parse_report_page(path)
        name = fields.get("Name")
        if not name:
            missing += 1
            continue
        default = ""
        for row in fields.get("Overrides") or []:
            if row and row[0].lower() == "default":
                default = row[-1]
                break
        page = _report_page(path, "Preferences")
        conn.execute(
            """INSERT OR REPLACE INTO prefs
               (name, category, description, data_type, scope, is_array, env_enabled, default_value, page)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (name, fields.get("Preference Category", ""), fields.get("Description", ""),
             fields.get("Data Type", ""), fields.get("Protection Scope", ""),
             fields.get("Is Array", ""), fields.get("Is Environment Enabled", ""), default, page))
        rows += 1
    log(f"prefs       {rows} definitions"
        + (f", {missing} pages without a name" if missing else ""))
    return rows


def ingest_queries(conn, report_dir):
    """Load the out-of-the-box saved queries from the same report."""
    if not report_dir:
        log("queries     skipped (no --admin-report); saved-query lookup unavailable")
        return 0
    folder = Path(report_dir) / "SavedQueries"
    files = sorted(folder.glob("ImanQuery_*.html"))
    if not files:
        sys.exit(f"no saved query pages under {folder}")
    conn.row_factory = sqlite3.Row
    rows = missing = 0
    for path in files:
        fields = parse_report_page(path)
        name = fields.get("Name")
        if not name:
            missing += 1
            continue
        conn.execute(
            """INSERT OR REPLACE INTO saved_queries
               (name, description, query_class, query_type, clauses, indented, owner, group_id, page)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (name, fields.get("Description", ""), fields.get("Query Class", ""),
             fields.get("Query Type", ""), fields.get("Query Clauses", ""),
             fields.get("Show Indented Results", ""), fields.get("Owner", ""),
             fields.get("Group ID", ""), _report_page(path, "SavedQueries")))
        rows += 1
    log(f"queries     {rows} saved queries"
        + (f", {missing} pages without a name" if missing else ""))
    return rows


def ingest_indexes(conn, indexes_dir):
    """Load the curated handler and utility indexes into two lookup tables.

    The TSVs are generated from this same help package by the generator scripts
    beside them; they carry placement, key arguments and Chinese keywords that
    the page prose does not. Each row is tied to its corpus page through
    Doc_Path, so a lookup can cite the original page.
    """
    import csv

    conn.row_factory = sqlite3.Row
    if not indexes_dir:
        log("indexes     skipped (no --indexes); handler/util lookups unavailable")
        return 0, 0
    index_dir = Path(indexes_dir)
    specs = [
        ("Workflow_Handlers_Index.tsv", "handlers", {
            "name": "Handler_Name", "kind": "Type", "placement": "Placement",
            "args": "Key_Arguments", "summary": "Summary", "doc": "Doc_Path"}),
        ("Utilities_Index.tsv", "utilities", {
            "name": "Tool_Name", "args": "Key_Arguments",
            "summary": "Summary", "doc": "Doc_Path"}),
    ]
    counts = []
    for filename, table, cols in specs:
        path = index_dir / filename
        if not path.exists():
            sys.exit(f"index file not found: {path}")
        rows = list(csv.DictReader(path.open(encoding="utf-8"), delimiter="\t"))
        matched = missing = 0
        for r in rows:
            parts = (r.get(cols["doc"]) or "").split("/")
            if len(parts) < 6:
                missing += 1
                continue
            location, dir_name, href = parts[3], parts[4], "/".join(parts[5:])
            page = conn.execute(
                """SELECT p.id, p.topic_id FROM pages p JOIN topics t ON t.id=p.topic_id
                   WHERE p.href=? AND t.location=? AND t.dir_name=?""",
                (href, location, dir_name)).fetchone()
            if page is None:
                missing += 1
                continue
            matched += 1
            if table == "handlers":
                conn.execute(
                    """INSERT OR REPLACE INTO handlers
                       (name, kind, placement, args, summary, page_id, topic_id, href)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (r[cols["name"]].strip(), r[cols["kind"]].strip(), r[cols["placement"]].strip(),
                     r[cols["args"]].strip(), r[cols["summary"]].strip(),
                     page["id"], page["topic_id"], href))
            else:
                conn.execute(
                    """INSERT OR REPLACE INTO utilities
                       (name, args, summary, page_id, topic_id, href)
                       VALUES (?,?,?,?,?,?)""",
                    (r[cols["name"]].strip(), r[cols["args"]].strip(),
                     r[cols["summary"]].strip(), page["id"], page["topic_id"], href))
        log(f"{table:<11} {matched} rows linked to pages"
            + (f", {missing} without a page" if missing else ""))
        counts.append(matched)
    return counts[0], counts[1]


def redact_secrets(conn):
    """Help pages carry sample credentials (a NuGet --api-key example, for one).
    Keep the sentence, mask the value, so the corpus can be published."""
    hits = 0
    for pid, text in conn.execute(
            "SELECT id, text FROM pages WHERE text LIKE '%--api-key%'").fetchall():
        new = API_KEY.sub(r"\1[REDACTED]", text or "")
        if new != text:
            conn.execute("UPDATE pages SET text=? WHERE id=?", (new, pid))
            hits += 1
    return hits


def title_key_of(title):
    """Case- and separator-insensitive form used to match identifiers like EPM-set-property."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def text_tokens(text):
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def refresh_indexes(db_path, indexes_dir):
    """Rebuild only the handler/utility tables inside an existing database."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("DROP TABLE IF EXISTS handlers; DROP TABLE IF EXISTS utilities;" + INDEX_SCHEMA)
    handlers, utilities = ingest_indexes(conn, indexes_dir)
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                     (("handlers", str(handlers)), ("utilities", str(utilities))))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    log(f"indexes     {handlers} handlers, {utilities} utilities -> {db_path}")


def refresh_prefs(db_path, report_dir):
    """Rebuild only the preference table inside an existing database."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("DROP TABLE IF EXISTS prefs;" + PREFS_SCHEMA)
    prefs = ingest_prefs(conn, report_dir)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('prefs', ?)", (str(prefs),))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    log(f"prefs       {prefs} definitions -> {db_path}")


def refresh_queries(db_path, report_dir):
    """Rebuild only the saved query table inside an existing database."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("DROP TABLE IF EXISTS saved_queries;" + QUERIES_SCHEMA)
    queries = ingest_queries(conn, report_dir)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('queries', ?)", (str(queries),))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    log(f"queries     {queries} saved queries -> {db_path}")


def refresh_dedupe(db_path):
    """Mark cross-book handler copies in an existing database."""
    conn = sqlite3.connect(str(db_path))
    marked = dedupe_handler_copies(conn)
    dups = conn.execute("SELECT SUM(dup_of IS NOT NULL) FROM pages").fetchone()[0] or 0
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('duplicates', ?)", (str(dups),))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    log(f"dedupe      {marked} cross-book handler copies marked, {dups} copies total -> {db_path}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release-root", help="help package root (holds HomePage/ and the book folders)")
    ap.add_argument("--out", help="output .db path")
    ap.add_argument("--indexes-only", metavar="DB",
                    help="only refresh handler/utility tables inside an existing database")
    ap.add_argument("--prefs-only", metavar="DB",
                    help="only refresh the preference table inside an existing database")
    ap.add_argument("--queries-only", metavar="DB",
                    help="only refresh the saved query table inside an existing database")
    ap.add_argument("--dedupe-only", metavar="DB",
                    help="only mark cross-book handler copies inside an existing database")
    ap.add_argument("--extra-handlers-only", metavar="DB",
                    help="only add handler pages missing from the curated index")
    ap.add_argument("--admin-report", default=None,
                    help="folder of the Administration Data Report (holds Preferences/)")
    ap.add_argument("--version", default="2506")
    ap.add_argument("--release-sku", default=None, help="release folder name, e.g. PL20241125556497283")
    ap.add_argument("--product", default="282219420", help="Siemens product id used in doc URLs")
    ap.add_argument("--swf-dir", default=None, help="optional folder of SWF html pages")
    ap.add_argument("--indexes", default=None,
                    help="folder holding Workflow_Handlers_Index.tsv and Utilities_Index.tsv")
    ap.add_argument("--pdf-extractor", default="pdftotext",
                    help="build-time PDF text extractor (default pdftotext); missing means PDF books stay unindexed")
    args = ap.parse_args()
    if args.indexes_only:
        if not args.indexes:
            ap.error("--indexes-only needs --indexes <folder>")
        refresh_indexes(args.indexes_only, args.indexes)
        return
    if args.prefs_only:
        if not args.admin_report:
            ap.error("--prefs-only needs --admin-report <folder>")
        refresh_prefs(args.prefs_only, args.admin_report)
        return
    if args.queries_only:
        if not args.admin_report:
            ap.error("--queries-only needs --admin-report <folder>")
        refresh_queries(args.queries_only, args.admin_report)
        return
    if args.dedupe_only:
        refresh_dedupe(args.dedupe_only)
        return
    if args.extra_handlers_only:
        if not args.release_root:
            ap.error("--extra-handlers-only needs --release-root <root>")
        conn = sqlite3.connect(str(args.extra_handlers_only))
        added, skipped = ingest_extra_handlers(conn, args.release_root)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('handlers', ?)",
                     (str(conn.execute("SELECT COUNT(*) FROM handlers").fetchone()[0]),))
        dedupe_handler_copies(conn)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('duplicates', ?)",
                     (str(conn.execute("SELECT SUM(dup_of IS NOT NULL) FROM pages").fetchone()[0] or 0),))
        conn.commit()
        conn.close()
        log(f"extra handl {added} added ({skipped} skipped) -> {args.extra_handlers_only}")
        return
    if not args.release_root or not args.out:
        ap.error("--release-root and --out are required for a full build")
    build(args)


if __name__ == "__main__":
    main()
