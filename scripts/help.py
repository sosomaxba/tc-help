#!/usr/bin/env python3
"""tc-help: query the local Teamcenter product-help corpus.

    python scripts/help.py map [--cat <id|name>]
    python scripts/help.py search "<english terms>" [--cat <id>] [--topic <id>] [--book <name>]
                                   [--client awc|rc|admin] [--group] [--cite] [--limit 10] [--json]
    python scripts/help.py handler "<name or English words>" [--limit 5] [--cite] [--json]
    python scripts/help.py util "<name or English words>" [--limit 5] [--cite] [--json]
    python scripts/help.py pref "<name or English words>" [--category <text>] [--limit 10] [--json]
    python scripts/help.py savedquery "<name or English words>" [--class <text>] [--limit 10] [--json]
    python scripts/help.py topic <id> [--grep <text>] [--limit 60]
    python scripts/help.py page <href|exact page title> [--grep <text>] [--max-chars 12000] [--cite] [--json]
    python scripts/help.py --selftest

All queries must be English: this tool does not translate.

Exit codes: 0 ok, 2 usage / non-English query, 3 not found, 4 database missing.
"""

import argparse
import contextlib
import io
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERSION = "2506"
LOCAL_BASE = "http://127.0.0.1:5000/documentation/external"
ONLINE_BASE = "https://docs.sw.siemens.com/documentation/external"

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
STOPWORDS = set("""a an the of to in on for with and or is are be was were how do does did
i we you my me our your it this that these those what which when where who why from as at by
can could should would about into over""".split())
# Words every row carries in its type/category; matching them would make any
# query with "handler" or "tool" return the whole table.
GENERIC = set("handler handlers action rule tool utility".split())
SNIPPET = 160
FETCH_CAP = 400
_BASE_CACHE = {}


def die(message, code):
    print(message, file=sys.stderr)
    sys.exit(code)


# ── database ────────────────────────────────────────────────────────

def find_db(explicit=None, version=None):
    if explicit:
        return Path(explicit)
    env = os.environ.get("TC_HELP_DB")
    if env:
        return Path(env)
    data = SKILL_ROOT / "data"
    if version:
        return data / version / f"tc_help_{version}.db"
    candidates = sorted(data.glob("*/tc_help_*.db")) if data.is_dir() else []
    if len(candidates) > 1:
        vers = ", ".join(p.parent.name for p in candidates)
        die(f"several databases installed ({vers}); choose one with --version <ver> or --db <path>", 2)
    return candidates[0] if candidates else data / DEFAULT_VERSION / f"tc_help_{DEFAULT_VERSION}.db"


def connect(db_path):
    if not db_path.exists():
        die(f"database not found: {db_path}\n"
            f"Build it with scripts/data/build_db.py (see the README build section), "
            f"or point TC_HELP_DB at an existing file.", 4)
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT key FROM meta LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        die(f"not a tc-help database: {db_path}", 4)
    return conn


def meta(conn):
    return {k: v for k, v in conn.execute("SELECT key, value FROM meta")}


# ── query building ──────────────────────────────────────────────────

def query_terms(query):
    """Split into (kind, text): quoted text stays one phrase, the rest are words.
    Phrases matter for UI labels ("check out", "list of values")."""
    terms = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', query or ""):
        if m.group(1) is not None:
            phrase = " ".join(m.group(1).split())
            if phrase:
                terms.append(("phrase", phrase))
        else:
            word = m.group(2)
            if word:
                terms.append(("word", word))
    return terms


def build_match(query):
    """Turn free English terms into an FTS5 AND expression with quoted tokens."""
    if CJK.search(query):
        die("query must be English: translate the question into official English terms first\n"
            "(this tool intentionally has no Chinese dictionary)", 2)
    parts = []
    for kind, tok in query_terms(query):
        if kind == "phrase":
            parts.append('"' + tok.replace('"', '""') + '"')
            continue
        prefix = tok.endswith("*")
        core = tok.rstrip("*")
        if not core:
            continue
        # Identifiers are written EPM-set-property, epm_set_property or EPM set property.
        for piece in re.split(r"[-_]+", core):
            piece = piece.strip()
            if not piece or (piece.lower() in STOPWORDS and not prefix):
                continue
            parts.append(f'"{piece}"' + ("*" if prefix else ""))
    if not parts:
        die("query has no searchable terms after removing common words; use content words", 2)
    return " ".join(parts)


def resolve_cat(conn, value):
    if value is None:
        return None
    value = str(value)
    if not value.strip():
        die("--cat needs a category id or name; run `map` to see the list", 2)
    rows = conn.execute("SELECT id, name FROM categories").fetchall()
    for r in rows:
        if str(r["id"]) == value or r["name"].lower() == value.lower():
            return r["id"]
    matches = [r for r in rows if value.lower() in r["name"].lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        shown = ", ".join(f"[{r['id']}] {r['name']}" for r in matches[:6])
        die(f"--cat {value!r} matches {len(matches)} categories ({shown}); pass the id", 2)
    die(f"no category matches {value!r}; run `map` to see the list", 2)


# ── original page URLs ──────────────────────────────────────────────

def local_alive(host="127.0.0.1", port=5000, timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def page_url(row, m, base):
    """Same path on two hosts: the local mirror and the Siemens docs site.
    The docs site serves these files behind a login; the doc-center page
    (/en-US/doc/...) is only a wrapper that iframes this tree."""
    if not row["location"] or not row["href"]:
        return None
    prefix = LOCAL_BASE if base == "local" else ONLINE_BASE
    return f"{prefix}/{m.get('release_sku', '')}/en-US/{row['location']}/{row['dir_name'] or ''}/{row['href']}"


def package_path(row, m):
    if not row["location"] or not row["href"]:
        return None
    return f"{m.get('release_sku', '')}/en-US/{row['location']}/{row['dir_name'] or ''}/{row['href']}"


def page_source(row, m, base):
    """Where the original page can be read right now.

    local   -> URL of the local help mirror
    online  -> same path on docs.sw.siemens.com (needs a Siemens login)
    package -> path under <help install>/documentation/external
    PDF/Excel/single-page books only ship as files, so they get a page or row
    locator either way. SWF pages are not part of the product help package."""
    locator = page_locator(row)
    if locator:
        return locator
    if row["location"] == "awc_swf":
        return "SWF help (Active Workspace) — no page address in this corpus"
    if base == "package":
        return package_path(row, m)
    return page_url(row, m, base)


def page_locator(row):
    """PDF/Excel/single-page artifacts ship inside the help package, so the
    source is a page or row range, not a URL."""
    href = row["href"] or ""
    if href.startswith("pdf/") and "/p" in href:
        name, _, pageno = href[4:].rpartition("/p")
        return f"PDF {name} p.{pageno}"
    if href.startswith("xlsx/") and "/r" in href:
        name = href[5:].split("/")[0]
        block = (row["title"] or "").rsplit(" - ", 1)[-1]
        return f"Excel {name} {block}".strip()
    if href.startswith("file/"):
        return f"single-page HTML {href[5:]}"
    return None


def choose_base(requested):
    if requested in ("local", "online", "package"):
        return requested
    if "auto" not in _BASE_CACHE:
        _BASE_CACHE["auto"] = "local" if local_alive() else "online"
    return _BASE_CACHE["auto"]


# ── commands ────────────────────────────────────────────────────────

def cmd_map(conn, args):
    if args.md:
        m = meta(conn)
        books = int(m.get("topics", 0))
        with_pages = conn.execute("SELECT COUNT(*) FROM topics WHERE page_count > 0").fetchone()[0]
        print("# Teamcenter help corpus map\n")
        print(f"Generated from the database; {m.get('pages', '?')} pages "
              f"in {books} books ({with_pages} with content).\n")
        for cat in conn.execute("SELECT id, name FROM categories ORDER BY id").fetchall():
            books = conn.execute("""SELECT id, title, page_count, note FROM topics
                                    WHERE category_id=? ORDER BY id""", (cat["id"],)).fetchall()
            total = sum(b["page_count"] for b in books)
            print(f"\n## {cat['name']} ({len(books)} books, {total:,} pages)\n")
            for b in books:
                extra = []
                if not b["page_count"]:
                    extra.append("no local content")
                if b["note"] and "also linked" in b["note"]:
                    extra.append(b["note"])
                tail = f" — {'; '.join(extra)}" if extra else ""
                print(f"- [{b['id']}] {b['title']} ({b['page_count']:,}p){tail}")
        return 0
    if args.cat is None:
        rows = conn.execute("""SELECT c.id, c.name, COUNT(t.id) books, COALESCE(SUM(t.page_count),0) pages
                               FROM categories c LEFT JOIN topics t ON t.category_id=c.id
                               GROUP BY c.id ORDER BY c.id""").fetchall()
        for r in rows:
            print(f"[{r['id']:>2}] {r['name']:<36} {r['books']:>3} books {r['pages']:>6,} pages")
        return 0
    cat_id = resolve_cat(conn, args.cat)
    rows = conn.execute("""SELECT id, title, page_count, note FROM topics
                           WHERE category_id=? ORDER BY id""", (cat_id,)).fetchall()
    for r in rows:
        mark = "" if r["page_count"] else "  (no local content)"
        print(f"[{r['id']:>3}] {r['title']:<58} {r['page_count']:>5,}p{mark}")
        if r["note"] and "also linked" in r["note"]:
            print(f"      {r['note']}")
    return 0


def client_of(book):
    """Which client a book documents, so duplicate answers can be told apart."""
    low = (book or "").lower()
    if "active workspace" in low:
        return "AWC"
    if "rich client" in low:
        return "RC"
    if "deployment" in low or "administration" in low:
        return "Admin"
    return ""


def group_keys(row):
    """One page can be indexed from several books: copies carry dup_of pointing at
    the canonical row, so fold them there. Platform variants stay separate — they
    have their own full text and merging them would hide the difference."""
    return (("page", row["dup_of"] or row["id"]),)


def cmd_search(conn, args):
    if args.limit < 1:
        die("--limit must be 1 or more", 2)
    m = meta(conn)
    base = choose_base(args.base)
    query_text = " ".join(args.query)
    match = build_match(query_text)
    # Global searches drop copies (the canonical row already carries them). Inside
    # one book the copy IS the book's page, so keep it and read the canonical text.
    scoped = bool(args.topic or args.book)
    filters = []
    fparams = []
    if args.cat:
        filters.append("t.category_id = ?")
        fparams.append(resolve_cat(conn, args.cat))
    if args.topic:
        filters.append("p.topic_id = ?")
        fparams.append(int(args.topic))
    if args.book:
        if str(args.book).isdigit():
            filters.append("p.topic_id = ?")
            fparams.append(int(args.book))
        else:
            pat = str(args.book).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append("t.title LIKE ? ESCAPE '\\'")
            fparams.append(f"%{pat}%")
    if getattr(args, "client", None):
        # Same book-title rule as client_of(), so a filtered search cannot show
        # a row the unfiltered one would label differently.
        if args.client == "awc":
            filters.append("t.title LIKE '%Active Workspace%'")
        elif args.client == "rc":
            filters.append("t.title LIKE '%Rich Client%'")
        else:
            filters.append("(t.title LIKE '%Administration%' OR t.title LIKE '%Deployment%')")
    scope_sql = " AND ".join(["1=1"] + filters)

    def select_rows(extra_where, extra_params):
        where_sql = " AND ".join(["pages_fts MATCH ?"] + filters + extra_where)
        sql = f"""SELECT c.name category, t.id topic_id, t.title topic, s.title section,
                         p.id, p.title, p.href, p.dup_of, p.source_url,
                         CASE WHEN p.dup_of IS NULL THEN p.text ELSE cp.text END AS text,
                         t.location AS location, t.dir_name AS dir_name
                  FROM pages_fts
                  JOIN pages p ON p.id = pages_fts.rowid
                  JOIN topics t ON t.id = p.topic_id
                  JOIN categories c ON c.id = t.category_id
                  LEFT JOIN sections s ON s.id = p.section_id
                  LEFT JOIN pages cp ON cp.id = p.dup_of
                  WHERE {where_sql}
                  ORDER BY bm25(pages_fts, 1.0, 1.0)
                  LIMIT ?"""
        return conn.execute(sql, [match] + fparams + extra_params + [min(args.limit * 4, FETCH_CAP)]).fetchall()

    try:
        where_sql = " AND ".join(["pages_fts MATCH ?"] + filters)
        total = conn.execute(f"""SELECT COUNT(DISTINCT COALESCE(p.dup_of, p.id)) FROM pages_fts
                                 JOIN pages p ON p.id = pages_fts.rowid
                                 JOIN topics t ON t.id = p.topic_id
                                 WHERE {where_sql}""", [match] + fparams).fetchone()[0]
        rows = select_rows([], [])

        # An exact page/identifier title wins over BM25: handler and utility names
        # ("EPM-set-property") are common words, and ranking alone buries them.
        key = re.sub(r"[^a-z0-9]+", "", query_text.lower())
        boosted = []
        if len(key) >= 6:
            cond = " AND ".join(["p.title_key = ?", scope_sql] + ([] if scoped else ["p.dup_of IS NULL"]))
            boosted = conn.execute(f"""SELECT c.name category, t.id topic_id, t.title topic,
                                              s.title section, p.id, p.title, p.href, p.dup_of, p.source_url,
                                              CASE WHEN p.dup_of IS NULL THEN p.text ELSE cp.text END AS text,
                                              t.location AS location, t.dir_name AS dir_name
                                       FROM pages p
                                       JOIN topics t ON t.id = p.topic_id
                                       JOIN categories c ON c.id = t.category_id
                                       LEFT JOIN sections s ON s.id = p.section_id
                                       LEFT JOIN pages cp ON cp.id = p.dup_of
                                       WHERE {cond}
                                       LIMIT 5""", [key] + fparams).fetchall()
            if boosted:
                seen_ids = {r["id"] for r in boosted}
                rows = boosted + [r for r in rows if r["id"] not in seen_ids]
    except sqlite3.OperationalError as exc:
        die(f"query syntax error: {exc}", 2)
    if not rows:
        print(f"no match for: {query_text}  (0 pages)")
        near = fuzzy_names(conn, args.query, limit=3)
        if near:
            print("close: " + "; ".join(near))
        print("next: different official wording (verb -> noun), a prefix (`workflo*`), "
              "`search \"<term>\" --cat <id>`, or `map` to pick the book and `topic` its contents")
        print("note: SDK/API reference books are not in this corpus; API contracts are out of scope")
        return 3

    out, seen = [], {}
    for r in rows:
        keys = group_keys(r)
        hit = next((seen[k] for k in keys if k in seen), None)
        if hit is not None:
            if r["topic"] != hit["topic"]:
                hit["also_in"].append(r["topic"])
            continue
        d = dict(r)
        d["page_title"] = r["title"]
        d["client"] = client_of(r["topic"])
        d["also_in"] = []
        d["source"] = page_source(r, m, base)
        d["url"] = d["source"] if d["source"].startswith("http") else None
        d["chars"] = len(r["text"] or "")
        d["snippet"] = snippet(r["text"], args.query)
        d["excerpts"] = excerpts(r["text"], args.query)
        d.pop("text", None)
        d.pop("dup_of", None)
        for k in keys:
            seen[k] = d
        out.append(d)
        if len(out) >= args.limit:
            break
    if out:
        # one query for every kept page instead of one per row (dup_of has no index)
        ids = [d["id"] for d in out]
        copies = {}
        for dup_of, title in conn.execute(
                f"""SELECT p.dup_of, t.title FROM pages p JOIN topics t ON t.id = p.topic_id
                    WHERE p.dup_of IN ({','.join('?' * len(ids))})""", ids):
            copies.setdefault(dup_of, []).append(title)
        for d in out:
            d["also_in"] = [x for x in dict.fromkeys(d["also_in"] + copies.get(d["id"], []))
                            if x != d["topic"]]
    if args.json:
        for d in out:
            d["total_hits"] = total
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    ordered = sorted(out, key=lambda d: (d["category"], d["topic"], d["title"])) if args.group else out
    last_group = None
    for i, d in enumerate(ordered, 1):
        if args.group and (d["category"], d["topic"]) != last_group:
            last_group = (d["category"], d["topic"])
            print(f"## {d['category']} > {d['topic']}")
        tag = f"[{d['client']}] " if d["client"] else ""
        print(f"[{i}] {tag}{d['category']} > {d['topic']} > {d['section'] or '-'}")
        print(f"    {d['title']}  ({d['chars']:,} chars)")
        print(f"    href: {d['href']}")
        if d["excerpts"]:
            for ex in d["excerpts"][:2]:
                print(f"    ...{ex}...")
        else:
            print(f"    ...{d['snippet']}...")
        if d["also_in"]:
            names = list(dict.fromkeys(d["also_in"]))
            more = f" (+{len(names) - 4} more)" if len(names) > 4 else ""
            print(f"    also in: {', '.join(names[:4])}{more}")
        if d["source"]:
            print(f"    src:  {d['source']}")
        if args.cite:
            first, second = cite_lines(d)
            print(f"    引用: {first}")
            print(f"    原文: {second}")
        print()
    if base == "package":
        print("note: src is a path under <help install>/documentation/external; "
              "start the 127.0.0.1:5000 help mirror for clickable links")
    elif base == "online":
        print("note: docs.sw.siemens.com serves these pages after a Siemens account login")
    print(f"({total} matching pages, showing {len(out)}; cite every hit with its src: line)")
    return 0


def snippet(text, terms):
    flat = " ".join((text or "").split())
    low = flat.lower()
    pos = -1
    for t in needles(terms):
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return flat[:SNIPPET]
    start = max(0, pos - SNIPPET // 3)
    return flat[start:start + SNIPPET]


# ── hit excerpts ────────────────────────────────────────────────────

def needles(terms):
    out = []
    raw = " ".join(terms) if isinstance(terms, (list, tuple)) else str(terms)
    for kind, t in query_terms(raw):
        t = t.strip().lower()
        if (kind == "phrase" and len(t) >= 2) or (kind == "word" and len(t) >= 3):
            if t not in out:
                out.append(t)
    return out


def excerpts(text, terms, limit=2, width=240):
    """Windows of the flattened page around the first hits, so a long page can
    answer without reading all of it. The corpus keeps no paragraph breaks."""
    flat = " ".join((text or "").split())
    low = flat.lower()
    found, pos = [], 0
    for _ in range(limit):
        best = -1
        for n in needles(terms):
            p = low.find(n, pos)
            if p >= 0 and (best < 0 or p < best):
                best = p
        if best < 0:
            break
        start = max(0, best - width // 3)
        found.append(flat[start:start + width])
        pos = best + width
    return found


# ── lookup scoring ─────────────────────────────────────────────────

def score_ascii(query, name, blob, contains=70, all_tokens=False):
    """Exact name > prefix > name contains > token hits in name plus blob.

    One rule for the handler/utility index, preferences and saved queries;
    callers only vary the "name contains" weight and whether every token must hit.
    """
    key = re.sub(r"[^a-z0-9]+", "", query.lower())
    name_key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if key and name_key == key:
        return 100
    if key and name_key.startswith(key):
        return 90
    if key and key in name_key:
        return contains
    toks = [t for t in re.split(r"[^a-z0-9]+", query.lower())
            if len(t) >= 3 and t not in STOPWORDS and t not in GENERIC]
    if not toks:
        return 0
    text = (name or "").lower() + " " + (blob or "").lower()
    hits = sum(1 for t in toks if t in text)
    if not hits:
        return 0
    if all_tokens:
        return 60 if hits == len(toks) else 0
    # One filler word ("such") must not carry a multi-word query; keep short
    # noise names from returning the whole table with rc=0.
    if len(toks) >= 2 and hits < 2:
        return 0
    ratio = hits / len(toks)
    return 50 * ratio if ratio >= 0.5 else 0


def cite_lines(d):
    """Two lines the answer should copy: book > section > page, then the source."""
    book = d.get("topic") or d.get("category") or ""
    trail = " > ".join(x for x in (d.get("section"), d.get("page_title") or d.get("name")) if x)
    return f"《{book}》> {trail}" if trail else f"《{book}》", d.get("source") or ""


def fuzzy_names(conn, query, limit=3):
    """Nearest titles/names for a zero-hit query; a wrong spelling should point
    somewhere instead of only saying 'no match'."""
    import difflib
    q = " ".join(query).strip()
    titles = [r[0] for r in conn.execute(
        "SELECT DISTINCT title FROM pages WHERE dup_of IS NULL AND length(title) > 4")]
    names = [r[0] for r in conn.execute(
        "SELECT name FROM handlers UNION SELECT name FROM utilities")]
    hits = difflib.get_close_matches(q, titles, n=limit, cutoff=0.6)
    if len(hits) < limit:
        hits += [n for n in difflib.get_close_matches(q.lower(), names, n=limit, cutoff=0.55)
                 if n not in hits]
    return hits[:limit]


# ── handler / utility lookup ───────────────────────────────────────

LOOKUP_SQL = {
    "handler": """SELECT h.name, h.kind, h.placement, h.args, h.summary,
                         h.href, p.title AS page_title, LENGTH(p.text) AS chars,
                         t.title AS topic, t.id AS topic_id, t.location AS location,
                         t.dir_name AS dir_name, s.title AS section, c.name AS category
                  FROM handlers h JOIN pages p ON p.id=h.page_id
                  JOIN topics t ON t.id=h.topic_id JOIN categories c ON c.id=t.category_id
                  LEFT JOIN sections s ON s.id=p.section_id""",
    "util": """SELECT u.name, NULL AS kind, NULL AS placement, u.args, u.summary,
                      u.href, p.title AS page_title, LENGTH(p.text) AS chars,
                      t.title AS topic, t.id AS topic_id, t.location AS location,
                      t.dir_name AS dir_name, s.title AS section, c.name AS category
               FROM utilities u JOIN pages p ON p.id=u.page_id
               JOIN topics t ON t.id=u.topic_id JOIN categories c ON c.id=t.category_id
               LEFT JOIN sections s ON s.id=p.section_id""",
}


def rank_lookup(rows, query):
    import difflib
    q = " ".join(query)
    names = [r["name"] for r in rows]
    near = difflib.get_close_matches(q, names, n=1, cutoff=0.8)
    close = near[0] if near else ""
    scored = []
    for row in rows:
        d = dict(row)
        blob = " ".join(str(d.get(k) or "") for k in ("args", "summary", "kind"))
        score = score_ascii(q, d["name"], blob, contains=80)
        # A near-miss spelling ("EPM-set-proprty") should surface the real
        # name instead of a pile of partial token hits.
        if close and d["name"] == close and score < 95:
            score = 95
        if score > 0:
            d["_score"] = score
            scored.append((score, d))
    scored.sort(key=lambda x: (-x[0], 0 if x[1]["name"].startswith("__") else 1,
                               len(x[1]["name"]), x[1]["name"].lower()))
    return [d for _, d in scored]


def cmd_lookup(conn, args, kind):
    if args.limit < 1:
        die("--limit must be 1 or more", 2)
    query_text = " ".join(args.query)
    if CJK.search(query_text):
        die("query must be English: translate the question into official English terms first\n"
            f"({kind} names and descriptions are English)", 2)
    m = meta(conn)
    base = choose_base(args.base)
    rows = [dict(r) for r in conn.execute(LOOKUP_SQL[kind])]
    found = rank_lookup(rows, args.query)
    if not found:
        print(f"no {kind} matches: {query_text}")
        near = fuzzy_names(conn, args.query, limit=3)
        if near:
            print("close: " + "; ".join(near))
        print(f"next: pass the {kind} name, English words from its description, "
              "or run `search` for the surrounding concept")
        return 3
    shown = found[:args.limit]
    # An exact name is the answer; do not dilute it with token-overlap rows.
    if shown[0]["_score"] >= 100:
        found = [d for d in found if d["_score"] >= 100]
        shown = found[:args.limit]
    for d in shown:
        d["source"] = page_source(d, m, base)
    if args.json:
        out = []
        for d in shown:
            item = {"name": d["name"], "kind": d.get("kind") or "Command-line utility",
                    "args": d["args"], "summary": d["summary"], "placement": d.get("placement"),
                    "href": d["href"], "page": d["page_title"], "chars": d["chars"],
                    "book": d["topic"], "section": d["section"], "category": d["category"],
                    "source": d["source"],
                    "url": d["source"] if (d["source"] or "").startswith("http") else None}
            if args.cite:
                item["cite"] = list(cite_lines(d))
            out.append(item)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    for i, d in enumerate(shown, 1):
        head = d.get("kind") or ("Command-line utility" if kind == "util" else "")
        print(f"[{i}] {d['name']}" + (f"   ({head})" if head else ""))
        if d.get("placement"):
            print(f"    放置  {d['placement']}")
        if d.get("args"):
            print(f"    参数  {d['args']}")
            if "…" in d["args"]:
                print("          (源报告里参数表到此截断，完整参数看原文页)")
        if d.get("summary"):
            print(f"    摘要  {d['summary']}")
        print(f"    页    {d['href']}  ({d['chars']:,} 字)")
        if d["source"]:
            print(f"    src:  {d['source']}")
        if args.cite:
            first, second = cite_lines(d)
            print(f"    引用  {first}")
            print(f"    原文: {second}")
        print()
    print(f"({len(found)} {kind} matches, showing {len(shown)})")
    return 0



def cmd_topic(conn, args):
    if args.limit < 1:
        die("--limit must be 1 or more", 2)
    row = conn.execute("SELECT * FROM topics WHERE id=?", (args.topic_id,)).fetchone()
    if row is None:
        die(f"no such book id: {args.topic_id}", 3)
    cat = conn.execute("SELECT name FROM categories WHERE id=?", (row["category_id"],)).fetchone()
    print(f"{cat['name']} > {row['title']}  ({row['page_count']:,} pages)")
    print("hrefs are library paths, not links; run `page <href>` for the text and its src: line")
    if row["note"]:
        print(f"note: {row['note']}")
    sections = conn.execute("SELECT id, title FROM sections WHERE topic_id=? ORDER BY id",
                            (args.topic_id,)).fetchall()
    needle = (args.grep or "").lower()
    printed = 0
    for sec in sections:
        pages = conn.execute("SELECT title, href FROM pages WHERE section_id=? ORDER BY id",
                             (sec["id"],)).fetchall()
        if needle:
            pages = [p for p in pages if needle in (p["title"] or "").lower()]
            if needle not in (sec["title"] or "").lower() and not pages:
                continue
        print(f"\n## {sec['title']}")
        for p in pages:
            if printed >= args.limit:
                print(f"... (stopped at {args.limit}; raise --limit to see more)")
                return 0
            print(f"   {p['title']}   [{p['href']}]")
            printed += 1
    if needle and not printed:
        print(f"no section or page in this book matches {args.grep!r}")
        return 3
    return 0


def cmd_page(conn, args):
    m = meta(conn)
    base = choose_base(args.base)
    ref = args.ref
    row = conn.execute("""SELECT p.*, t.title topic, t.id topic_id, c.name category, s.title section,
                                 t.location AS location, t.dir_name AS dir_name
                          FROM pages p JOIN topics t ON t.id=p.topic_id
                          JOIN categories c ON c.id=t.category_id
                          LEFT JOIN sections s ON s.id=p.section_id
                          WHERE p.href=?""", (ref,)).fetchone()
    if row is None:
        norm = " ".join(ref.replace("\xa0", " ").split())
        rows = conn.execute("""SELECT p.id, p.title, p.href, t.title topic, c.name category
                               FROM pages p JOIN topics t ON t.id=p.topic_id
                               JOIN categories c ON c.id=t.category_id
                               WHERE replace(p.title, char(160), ' ') = ? COLLATE NOCASE
                               LIMIT 10""", (norm,)).fetchall()
        if not rows:
            print(f"not found: {ref}")
            print("pass an exact page title or the href printed by `search`")
            return 3
        # The corpus is built from several books: the same page can be indexed
        # under one href more than once. Only distinct hrefs are real ambiguity.
        distinct = {}
        for r in rows:
            distinct.setdefault(r["href"], r)
        if len(distinct) > 1:
            stats = conn.execute("""SELECT COUNT(*), COUNT(DISTINCT href) FROM pages
                                    WHERE replace(title, char(160), ' ') = ? COLLATE NOCASE""",
                                 (norm,)).fetchone()
            print(f"{stats[0]} pages share that title ({stats[1]} distinct hrefs); pass one href:")
            for href, r in list(distinct.items())[:10]:
                print(f"  {r['category']} > {r['topic']} > {r['title']}   [{href}]")
            return 3
        row = conn.execute("""SELECT p.*, t.title topic, t.id topic_id, c.name category, s.title section,
                                     t.location AS location, t.dir_name AS dir_name
                              FROM pages p JOIN topics t ON t.id=p.topic_id
                              JOIN categories c ON c.id=t.category_id
                              LEFT JOIN sections s ON s.id=p.section_id
                              WHERE p.href=? ORDER BY p.dup_of IS NOT NULL LIMIT 1""",
                           (next(iter(distinct)),)).fetchone()

    if row["dup_of"]:
        canon = conn.execute("""SELECT p.*, t.title topic, c.name category, s.title section,
                                       t.location AS location, t.dir_name AS dir_name
                                FROM pages p JOIN topics t ON t.id=p.topic_id
                                JOIN categories c ON c.id=t.category_id
                                LEFT JOIN sections s ON s.id=p.section_id
                                WHERE p.id=?""", (row["dup_of"],)).fetchone()
        print(f"this page is a copy: the body lives in {canon['category']} > {canon['topic']}")
        row = canon

    if args.max_chars < 0:
        die("--max-chars must be 0 (whole page) or a positive number", 2)
    source = page_source(row, m, base)
    url = source if source.startswith("http") else None
    text = row["text"] or ""
    site = {"category": row["category"], "topic": row["topic"], "section": row["section"],
            "page_title": row["title"], "source": source}
    matched = []
    if args.grep:
        matched = excerpts(text, [args.grep], limit=5, width=400)
        if not matched:
            print(f"no match for {args.grep!r} in {row['title']}")
            return 3
    shown = "\n\n".join(matched) if matched else (text[:args.max_chars] if args.max_chars else text)
    if args.json:
        doc = {"category": row["category"], "topic": row["topic"],
               "section": row["section"], "title": row["title"], "href": row["href"],
               "url": url, "source": source,
               "chars_total": len(text), "truncated": len(shown) < len(text),
               "text": shown}
        if matched:
            doc["matched"] = matched
        if args.cite:
            doc["cite"] = list(cite_lines(site))
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    print(f"{row['category']} > {row['topic']} > {row['section'] or '-'}")
    print(f"{row['title']}")
    print(f"href: {row['href']}")
    if source:
        print(f"src:  {source}")
        if base == "package" and not url:
            print("      (path under <help install>/documentation/external; "
                  "start the 127.0.0.1:5000 help mirror for a clickable link)")
    if args.cite:
        first, second = cite_lines(site)
        print(f"引用: {first}")
        print(f"原文: {second}")
    print("-" * 60)
    print(shown)
    if args.grep:
        print(f"\n[{len(matched)} matches for {args.grep!r}; whole page is {len(text):,} chars]")
    elif len(shown) < len(text):
        print(f"\n[truncated: {args.max_chars} of {len(text):,} chars; use --max-chars 0 for all]")
    return 0


def pref_source(page, base):
    if not page:
        return None
    if base == "package":
        return page
    prefix = LOCAL_BASE if base == "local" else ONLINE_BASE
    return f"{prefix}/{page}"


def pref_warnings(description):
    """Surface the two warning phrases that otherwise sit in the middle of a
    long description: deprecated and internal-only preferences."""
    low = (description or "").lower()
    out = []
    if "deprecated" in low:
        sentence = ""
        for part in re.split(r"(?<=[.!?])\s+", description or ""):
            if "deprecated" in part.lower():
                sentence = " ".join(part.split())[:300]
                break
        out.append("已弃用：" + (sentence or "说明里标注 deprecated"))
    if "internal preference" in low:
        out.append("内部项：说明里标注 This is an internal preference")
    return out


def cmd_pref(conn, args):
    """Preference definitions come from the Administration Data Report."""
    query = " ".join(args.query or [])
    if not query and not args.category:
        die("pref needs a name/description query, or --category <text>", 2)
    if query and CJK.search(query):
        die("preference names and descriptions are English; translate the question first", 2)
    m = meta(conn)
    base = choose_base(args.base)
    rows = [dict(r) for r in conn.execute("SELECT * FROM prefs")]
    if args.category:
        needle = args.category.lower()
        found = [r for r in rows if needle in (r["category"] or "").lower()]
        found.sort(key=lambda r: r["name"].lower())
    else:
        scored = []
        for r in rows:
            blob = f"{r['description'] or ''} {r['category'] or ''}"
            score = score_ascii(query, r["name"], blob, contains=70, all_tokens=True)
            if score:
                scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], 0 if x[1]["name"].startswith("__") else 1,
                                   len(x[1]["name"]), x[1]["name"].lower()))
        found = [r for _, r in scored]
    if not found:
        print(f"no preference matches: {query or args.category}")
        print("next: pass the preference name, or English words from its description; "
              "`pref --category Active Workspace` lists a category")
        return 3
    shown = found[:args.limit]
    for d in shown:
        d["source"] = pref_source(d["page"], base)
    if args.json:
        out = []
        for d in shown:
            item = {k: d[k] for k in ("name", "category", "description", "data_type", "scope",
                                      "is_array", "env_enabled", "default_value")}
            item["source"] = d["source"]
            item["url"] = d["source"] if (d["source"] or "").startswith("http") else None
            item["warnings"] = pref_warnings(d["description"])
            if args.cite:
                item["cite"] = [f"《Administration Data Documentation》> Preferences > {d['name']}",
                                d["source"] or ""]
            out.append(item)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    for i, d in enumerate(shown, 1):
        print(f"[{i}] {d['name']}")
        for warning in pref_warnings(d["description"]):
            print(f"    ⚠ {warning}")
        if d["category"]:
            print(f"    分类  {d['category']}")
        flags = " | ".join(x for x in (
            d["data_type"], f"保护范围 {d['scope']}" if d["scope"] else "",
            f"数组 {d['is_array']}" if d["is_array"] else "") if x)
        if flags:
            print(f"    类型  {flags}")
        if d["default_value"]:
            print(f"    默认  {d['default_value']}")
        if d["description"]:
            print(f"    说明  {d['description']}")
        if d["source"]:
            print(f"    src:  {d['source']}")
        if args.cite:
            print(f"    引用  《Administration Data Documentation》> Preferences > {d['name']}")
            print(f"    原文: {d['source'] or ''}")
        print()
    print(f"({len(found)} preference matches, showing {len(shown)})")
    print("note: this is the Administration Data Report snapshot; what your site has "
          "configured → tc-biz")
    return 0


def cmd_savedquery(conn, args):
    """Out-of-the-box saved queries from the Administration Data Report."""
    query = " ".join(args.query or [])
    if not query and not args.query_class:
        die("savedquery needs a name/keyword query, or --class <text>", 2)
    if query and CJK.search(query):
        die("saved query names and clauses are English; translate the question first", 2)
    m = meta(conn)
    base = choose_base(args.base)
    rows = [dict(r) for r in conn.execute("SELECT * FROM saved_queries")]
    if args.query_class:
        needle = args.query_class.strip().lower()
        found = [r for r in rows if (r["query_class"] or "").lower() == needle]
        found.sort(key=lambda r: r["name"].lower())
        if not found:
            similar = sorted({r["query_class"] for r in rows
                              if needle in (r["query_class"] or "").lower()})
            print(f"no saved query has query class {args.query_class!r}")
            if similar:
                print("close classes: " + ", ".join(similar[:8]))
            return 3
    else:
        scored = []
        for r in rows:
            blob = " ".join((r["description"] or "", r["clauses"] or "",
                             r["query_class"] or ""))
            score = score_ascii(query, r["name"], blob, contains=70, all_tokens=True)
            if score:
                scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], 0 if x[1]["name"].startswith("__") else 1,
                                   len(x[1]["name"]), x[1]["name"].lower()))
        found = [r for _, r in scored]
    if not found:
        print(f"no saved query matches: {query or args.query_class}")
        print("next: pass the query name, or English words from its description/clauses; "
              "`savedquery --class Item` lists by query class")
        return 3
    shown = found[:args.limit]
    for d in shown:
        d["source"] = pref_source(d["page"], base)
    if args.json:
        out = []
        for d in shown:
            item = {k: d[k] for k in ("name", "description", "query_class", "query_type",
                                      "clauses", "indented", "owner", "group_id")}
            item["source"] = d["source"]
            item["url"] = d["source"] if (d["source"] or "").startswith("http") else None
            if args.cite:
                item["cite"] = [f"《Administration Data Documentation》> Saved Queries > {d['name']}",
                                d["source"] or ""]
            out.append(item)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    for i, d in enumerate(shown, 1):
        print(f"[{i}] {d['name']}")
        if d["query_class"]:
            print(f"    查询类  {d['query_class']}")
        flags = " | ".join(x for x in (
            f"类型 {d['query_type']}" if d["query_type"] else "",
            f"缩进 {d['indented']}" if d["indented"] else "") if x)
        if flags:
            print(f"    参数    {flags}")
        if d["description"]:
            print(f"    说明    {d['description']}")
        if d["clauses"]:
            print(f"    条件    {d['clauses']}")
        if d["source"]:
            print(f"    src:    {d['source']}")
        if args.cite:
            print(f"    引用  《Administration Data Documentation》> Saved Queries > {d['name']}")
            print(f"    原文: {d['source'] or ''}")
        print()
    print(f"({len(found)} saved query matches, showing {len(shown)})")
    print("note: this is the Administration Data Report snapshot; whether your site has "
          "this query configured → tc-biz")
    return 0


# ── selftest ────────────────────────────────────────────────────────

def selftest(db_path):
    checks = []

    def check(name, fn):
        try:
            detail = fn() or ""
            checks.append((True, name, detail))
        except AssertionError as exc:
            checks.append((False, name, str(exc)))
        except Exception as exc:  # noqa: BLE001 - report, do not crash the run
            checks.append((False, name, f"{type(exc).__name__}: {exc}"))

    conn = connect(db_path)
    m = meta(conn)

    def run(args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(args)
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def t_meta():
        assert m.get("version") == "2506", f"version {m.get('version')}"
        return f"version {m['version']}, {int(m['pages']):,} pages"

    def t_counts():
        books = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        with_pages = conn.execute("SELECT COUNT(*) FROM topics WHERE page_count>0").fetchone()[0]
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        assert pages == int(m["pages"]), "meta page count drifted from pages table"
        assert (books, with_pages, pages) == (203, 202, 24334), (
            f"corpus is {books} books ({with_pages} with content), {pages} pages; "
            "expected 203/202/24334 - repin these and regenerate domain-map, "
            "README and corpus.md if the rebuild is intentional")
        return f"{books} books ({with_pages} with content), {pages:,} pages"

    def t_cjk():
        specs = {
            "pages": ("title", "text"), "topics": ("title", "note"),
            "sections": ("title",), "categories": ("name",),
            "handlers": ("name", "kind", "placement", "args", "summary"),
            "utilities": ("name", "args", "summary"),
            "prefs": ("name", "category", "description", "data_type", "scope",
                      "default_value"),
            "saved_queries": ("name", "description", "query_class", "clauses",
                              "owner", "group_id"),
            "meta": ("value",),
        }
        bad = []
        for table, cols in specs.items():
            for col in cols:
                for (value,) in conn.execute(f"SELECT {col} FROM {table}"):
                    if value and CJK.search(str(value)):
                        bad.append(f"{table}.{col}")
                        break
        assert not bad, f"CJK found in {bad}; the corpus must stay English-only"
        return "all text columns are English-only"

    def t_reject_cjk():
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                build_match("怎么改所有者")
            except SystemExit:
                return "Chinese query rejected"
        raise AssertionError("Chinese query was accepted")

    def t_reject_stopwords():
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                build_match("how do i the")
            except SystemExit:
                return "stopword-only query rejected"
        raise AssertionError("stopword-only query was accepted")

    def t_known_page():
        rc, out, _ = run(["search", "change ownership object", "--limit", "5"])
        assert rc == 0, f"search exited {rc}"
        assert "ownership" in out.lower(), f"no ownership hit: {out[:200]}"
        first = out.splitlines()[1].strip() if len(out.splitlines()) > 1 else "?"
        return "change ownership -> " + first[:48]

    def t_prefix():
        n = conn.execute("SELECT COUNT(*) FROM pages_fts WHERE pages_fts MATCH ?",
                         (build_match("workflo*"),)).fetchone()[0]
        assert n > 0, "prefix query returned nothing"
        return f"workflo* -> {n} pages"

    def t_copies():
        marked = conn.execute("SELECT COUNT(*) FROM pages WHERE dup_of IS NOT NULL").fetchone()[0]
        orphans = conn.execute("SELECT COUNT(*) FROM pages p WHERE p.dup_of IS NOT NULL "
                               "AND NOT EXISTS (SELECT 1 FROM pages c WHERE c.id=p.dup_of)").fetchone()[0]
        assert marked > 0, "no duplicates were marked at build time"
        assert orphans == 0, f"{orphans} copy rows point at a missing canonical page"
        return f"{marked:,} copies marked, 0 dangling"

    def t_dedupe():
        # A copy living in another book under a different href must not come back
        # as a second result: search returns its canonical page once.
        row = conn.execute("""SELECT p.href AS copy_href, cp.href AS canon_href, p.title
                              FROM pages p JOIN pages cp ON cp.id = p.dup_of
                              WHERE p.href <> cp.href AND length(p.title) > 20
                                AND NOT EXISTS (SELECT 1 FROM pages x
                                                WHERE x.href = p.href AND x.dup_of IS NULL)
                              ORDER BY p.id LIMIT 1""").fetchone()
        assert row is not None, "no cross-book copy sample found"
        rc, out, _ = run(["search", f'"{row["title"]}"', "--limit", "10"])
        assert rc == 0, f"search exited {rc}"
        assert row["canon_href"] in out, "canonical page missing from results"
        assert row["copy_href"] not in out, "copy came back as its own result"
        return f"copy folded into canonical ({row['title'][:32]})"

    def t_page_by_href():
        href = conn.execute("SELECT href FROM pages WHERE dup_of IS NULL LIMIT 1").fetchone()[0]
        rc, out, _ = run(["page", href, "--max-chars", "200"])
        assert rc == 0, f"page exited {rc}"
        assert href in out, "href not echoed back"
        return f"page reads by href ({href[:32]})"

    def t_page_by_title():
        title = conn.execute("""SELECT title FROM pages
                                WHERE dup_of IS NULL AND length(title) BETWEEN 10 AND 80
                                  AND title NOT LIKE '%"%' AND title NOT LIKE '%[%'
                                GROUP BY title HAVING COUNT(*)=1 LIMIT 1""").fetchone()[0]
        rc, out, _ = run(["page", title, "--max-chars", "200"])
        assert rc == 0, f"page exited {rc}"
        return f"page reads by title ({title[:32]})"

    def t_url():
        row = conn.execute("""SELECT p.*, t.location AS location, t.dir_name AS dir_name
                              FROM pages p JOIN topics t ON t.id=p.topic_id
                              WHERE t.location='tc_help' AND p.dup_of IS NULL LIMIT 1""").fetchone()
        url = page_source(row, m, "local")
        assert url.startswith(f"{LOCAL_BASE}/"), f"bad local url {url}"
        online = page_source(row, m, "online")
        assert online.startswith(f"{ONLINE_BASE}/{m['release_sku']}/en-US/tc_help/"), online
        assert online.replace(ONLINE_BASE, "") == url.replace(LOCAL_BASE, ""), "hosts must share the path"
        rel = page_source(row, m, "package")
        assert rel.startswith(f"{m['release_sku']}/en-US/tc_help/"), f"bad package path {rel}"
        assert not rel.startswith("http"), rel
        rc, out, _ = run(["search", "change ownership", "--limit", "2", "--base", "local"])
        assert rc == 0, f"search exited {rc}"
        assert "    src:  http://127.0.0.1:5000/" in out, "search printed no src: line"
        assert "cite every hit with its src: line" in out, "search printed no citation reminder"
        return "local + public URL share one path, search shows src"

    def t_no_online_link():
        href = conn.execute("SELECT href FROM pages WHERE source_url IS NOT NULL LIMIT 1").fetchone()[0]
        rc, out, _ = run(["page", href, "--max-chars", "40", "--base", "local"])
        assert rc == 0, f"swf page exited {rc}"
        assert "docs.sw.siemens.com" not in out, "still printing a fabricated docs.sw link"
        assert "SWF help" in out, out[:200]
        return "no invented vendor links"

    def t_no_url_source():
        kinds = []
        for prefix, label in (("pdf/", "PDF "), ("xlsx/", "Excel "), ("file/", "single-page HTML ")):
            href = conn.execute("SELECT href FROM pages WHERE href LIKE ? LIMIT 1",
                                (prefix + "%",)).fetchone()
            if href is None:
                continue
            rc, out, _ = run(["page", href[0], "--max-chars", "80"])
            assert rc == 0, f"page {href[0]} exited {rc}"
            assert f"src:  {label}" in out, f"{prefix} page printed no locator:\n{out}"
            kinds.append(prefix.rstrip("/"))
        assert kinds, "no PDF/Excel/single-page pages in the database"
        topic = conn.execute("SELECT id FROM topics WHERE title LIKE '%Feature Mapping%' LIMIT 1").fetchone()
        if topic:
            rc, out, _ = run(["topic", str(topic[0])])
            assert rc == 0, f"topic exited {rc}"
            assert "hrefs are library paths" in out, "topic printed no page-href hint"
            rc, out, _ = run(["search", "workflow", "--topic", str(topic[0]), "--limit", "5", "--json"])
            assert rc == 0, f"scoped search exited {rc}"
            rows = json.loads(out)
            bare = [r["href"] for r in rows if not r.get("source")]
            assert not bare, f"{len(bare)} results carry no source: {bare[:3]}"
            assert any(not r["url"] for r in rows), "scoped search found no artifact page to test"
            kinds.append("search")
        return "source line for " + ", ".join(kinds)

    def t_map():
        cats = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        assert cats == 20, f"{cats} categories"
        return f"{cats} categories"

    def t_map_doc():
        doc = SKILL_ROOT / "references" / "domain-map.md"
        assert doc.exists(), "references/domain-map.md is missing"
        rc, out, _ = run(["map", "--md"])
        assert rc == 0, f"map --md exited {rc}"
        assert out.strip() == doc.read_text(encoding="utf-8").strip(), (
            "references/domain-map.md is stale; regenerate it with "
            "`help.py map --md > references/domain-map.md`")
        return "domain-map matches the database"

    def t_exit_codes():
        rc_cn, _, _ = run(["search", "怎么改所有者"])
        rc_none, _, _ = run(["search", "zzqqxxnotaword"])
        rc_nodb, _, _ = run(["--db", str(db_path.with_name("selftest-missing.db")), "map"])
        assert (rc_cn, rc_none, rc_nodb) == (2, 3, 4), \
            f"got {rc_cn}/{rc_none}/{rc_nodb}, expected 2/3/4"
        return "2 chinese / 3 no match / 4 no database"

    def t_limit_bounds():
        rc_search, _, err_search = run(["search", "ownership", "--limit", "0"])
        rc_topic, _, err_topic = run(["topic", "156", "--limit", "-1"])
        assert (rc_search, rc_topic) == (2, 2), f"got {rc_search}/{rc_topic}"
        assert "--limit" in err_search and "--limit" in err_topic, "message does not name --limit"
        rc, out, _ = run(["search", "ownership", "--limit", "100", "--json"])
        assert rc == 0, f"limit 100 exited {rc}"
        rows = json.loads(out)
        assert len(rows) == 100, f"limit 100 returned {len(rows)} rows"
        return "limit <1 rejected, limit 100 honoured"

    def t_page_json_truncates():
        big = conn.execute("SELECT title, length(text) n FROM pages WHERE dup_of IS NULL "
                           "ORDER BY n DESC LIMIT 1").fetchone()
        rc, out, _ = run(["page", big["title"], "--max-chars", "100", "--json"])
        assert rc == 0, f"page --json exited {rc}"
        doc = json.loads(out)
        assert doc["chars_total"] == big["n"] and doc["truncated"] is True, "truncation flags wrong"
        assert len(doc["text"]) == 100, f"text kept {len(doc['text'])} chars"
        assert len(out) < 4000, f"{len(out)} bytes of json"
        rc, _, err = run(["page", big["title"], "--max-chars", "-5"])
        assert rc == 2 and "--max-chars" in err, f"negative --max-chars exited {rc}"
        return f"json capped at 100 of {big['n']:,} chars"

    def t_ambiguous_title():
        row = conn.execute("""SELECT title, COUNT(*) n, COUNT(DISTINCT href) h FROM pages
                              GROUP BY title HAVING h > 1 ORDER BY n DESC LIMIT 1""").fetchone()
        rc, out, _ = run(["page", row["title"], "--max-chars", "50"])
        assert rc == 3, f"ambiguous page exited {rc}"
        assert f"{row['n']} pages share that title ({row['h']} distinct hrefs)" in out, out.splitlines()[:2]
        assert "pass one href" in out, out[:200]
        assert len(out.strip().splitlines()) <= 13, "listing is not capped"
        return f"{row['n']} same-title rows / {row['h']} hrefs reported, 10 listed"

    def t_cat_values():
        rc, _, err = run(["map", "--cat", ""])
        assert rc == 2 and "--cat" in err, f"empty --cat exited {rc}"
        names = [r["name"] for r in conn.execute("SELECT name FROM categories")]
        probe = next((p for p in ("a", "e", "data", "management")
                      if sum(p in c.lower() for c in names) > 1), None)
        assert probe, "no ambiguous category probe found"
        rc, _, err = run(["map", "--cat", probe])
        assert rc == 2 and "pass the id" in err, f"--cat {probe} exited {rc}"
        return f"empty rejected, {probe!r} ambiguity listed"

    def t_book_wildcard():
        rc, out, _ = run(["search", "ownership", "--book", "%"])
        assert rc == 3 and "no match" in out, f"--book % exited {rc}"
        return "% matched literally (no book titled so)"

    def t_multi_version():
        global SKILL_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            for ver in ("2506", "2606"):
                d = Path(tmp) / "data" / ver
                d.mkdir(parents=True)
                (d / f"tc_help_{ver}.db").write_bytes(b"")
            saved = SKILL_ROOT
            SKILL_ROOT = Path(tmp)
            try:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    try:
                        find_db(None, None)
                    except SystemExit as exc:
                        assert exc.code == 2, exc.code
                    else:
                        raise AssertionError("ambiguous database set was accepted")
                text = err.getvalue()
                assert "2506" in text and "2606" in text, text[:120]
                assert "--version" in text, text[:120]
            finally:
                SKILL_ROOT = saved
        return "two databases -> explicit choice required"

    def t_missing_db_hint():
        rc, _, err = run(["--db", str(db_path.with_name("selftest-missing.db")), "map"])
        assert rc == 4, f"missing db exited {rc}"
        assert "scripts/data/build_db.py" in err, err[:160]
        return "missing db -> rc 4 and the real build script path"

    def t_base_forced():
        rc, out, _ = run(["search", "ownership", "--limit", "1", "--base", "package", "--json"])
        assert rc == 0, f"package base exited {rc}"
        src = json.loads(out)[0]["source"]
        assert src.startswith(f"{m['release_sku']}/en-US/"), src
        rc, out, _ = run(["search", "ownership", "--limit", "1", "--base", "local", "--json"])
        assert rc == 0, f"local base exited {rc}"
        url = json.loads(out)[0]["url"]
        assert url.startswith(f"{LOCAL_BASE}/"), url
        rc, out, _ = run(["search", "ownership", "--limit", "1", "--base", "online", "--json"])
        assert rc == 0, f"online base exited {rc}"
        url = json.loads(out)[0]["url"]
        assert url.startswith(ONLINE_BASE + "/") and "/doc/" not in url, url
        rc, out, _ = run(["search", "ownership", "--limit", "1", "--base", "bogus"])
        assert rc == 2, f"unknown base should be a usage error, rc={rc}"
        return "package / local / online honoured, bogus rejected"

    def t_index_tables():
        h = conn.execute("SELECT COUNT(*) FROM handlers").fetchone()[0]
        u = conn.execute("SELECT COUNT(*) FROM utilities").fetchone()[0]
        assert (h, u) == (316, 358), f"indexes are {h}/{u}, expected 316/358"
        assert (m.get("handlers"), m.get("utilities")) == ("316", "358"), "meta counts drifted"
        return f"{h} handlers, {u} utilities"

    def t_handler_lookup():
        rc, out, _ = run(["handler", "EPM-set-property", "--limit", "2"])
        assert rc == 0, f"handler exited {rc}"
        assert "EPM-set-property" in out and "Action Handler" in out, out[:200]
        assert "-property" in out and "放置" in out, out[:300]
        assert "    src:  " in out, "no src line"
        return "name -> placement, arguments, src"

    def t_util_lookup():
        rc, out, _ = run(["util", "export administration data", "--limit", "3"])
        assert rc == 0, f"util exited {rc}"
        assert "admin_data_export" in out, out[:200]
        assert "参数" in out and "src:" in out
        rc, out, _ = run(["util", "tcxml_export", "--limit", "2"])
        assert rc == 0 and out.lstrip().startswith("[1] tcxml_export"), out[:120]
        return "description words find the tool, exact name first"

    def t_search_client():
        rc, out, _ = run(["search", "ownership", "--client", "awc", "--limit", "3", "--json"])
        assert rc == 0, f"client filter exited {rc}"
        rows = json.loads(out)
        bad = [r["topic"] for r in rows if "active workspace" not in r["topic"].lower()]
        assert not bad, f"non-AWC rows leaked: {bad[:2]}"
        return f"{len(rows)} rows, all Active Workspace"

    def t_search_cite():
        rc, out, _ = run(["search", "change ownership", "--limit", "1", "--cite"])
        assert rc == 0, f"cite exited {rc}"
        assert "引用:" in out and "原文:" in out, out[-300:]
        return "two-line citation printed"

    def t_page_grep():
        href = conn.execute("SELECT href FROM handlers WHERE name='EPM-set-property'").fetchone()[0]
        rc, full, _ = run(["page", href, "--max-chars", "0"])
        assert rc == 0, f"page exited {rc}"
        rc2, out, _ = run(["page", href, "--grep", "bypass"])
        assert rc2 == 0, f"page --grep exited {rc2}"
        assert len(out) < len(full) / 3, f"grep did not shrink ({len(out)} vs {len(full)})"
        assert "bypass" in out.lower()
        return f"{len(full):,} -> {len(out):,} bytes"

    def t_fuzzy():
        rc, out, _ = run(["search", "signof", "--limit", "2"])
        assert rc == 3, f"typo query exited {rc}"
        assert "close:" in out, out[:200]
        return "zero hit -> close titles"

    def t_pref_table():
        n = conn.execute("SELECT COUNT(*) FROM prefs").fetchone()[0]
        assert n == 13455, f"prefs table has {n} rows, expected 13455"
        assert m.get("prefs") == "13455", f"meta prefs={m.get('prefs')}"
        return f"{n:,} definitions"

    def t_pref_exact():
        name = conn.execute("SELECT name FROM prefs WHERE default_value<>'' LIMIT 1").fetchone()[0]
        rc, out, _ = run(["pref", name, "--limit", "1"])
        assert rc == 0 and name in out, out[:200]
        assert "默认" in out and "说明" in out and "    src:  " in out, out[:400]
        assert "AdminDataReport/Preferences/" in out, "src is not the report page"
        return "name -> category, default, description, src"

    def t_pref_desc():
        rc, out, _ = run(["pref", "summary stylesheet", "--limit", "3"])
        assert rc == 0 and "说明" in out, out[:200]
        return "description words find preferences"

    def t_pref_category():
        rc, out, _ = run(["pref", "--category", "Active Workspace", "--limit", "2"])
        assert rc == 0 and "分类  Active Workspace" in out, out[:200]
        return "category listing"

    def t_pref_cjk():
        rc, _, err = run(["pref", "首选项"])
        assert rc == 2 and "English" in err, f"rc={rc} err={err[:120]}"
        return "Chinese query refused"

    def t_sq_table():
        n = conn.execute("SELECT COUNT(*) FROM saved_queries").fetchone()[0]
        assert n == 983, f"saved queries table has {n} rows, expected 983"
        assert m.get("queries") == "983", f"meta queries={m.get('queries')}"
        return f"{n} saved queries"

    def t_sq_exact():
        name = conn.execute("SELECT name FROM saved_queries WHERE clauses<>'' LIMIT 1").fetchone()[0]
        rc, out, _ = run(["savedquery", name, "--limit", "1"])
        assert rc == 0 and name in out, out[:200]
        assert "查询类" in out and "条件" in out and "src:" in out, out[:400]
        assert "SavedQueries/" in out, "src is not the report page"
        return "name -> class, clauses, src"

    def t_sq_clause():
        rc, out, _ = run(["savedquery", "owning_user", "--limit", "3"])
        assert rc == 0 and "条件" in out, out[:200]
        return "clause words find queries"

    def t_sq_cjk():
        rc, _, err = run(["savedquery", "查询"])
        assert rc == 2 and "English" in err, f"rc={rc} err={err[:120]}"
        return "Chinese query refused"

    def t_lookup_cjk_rejected():
        for cmd in ("handler", "util"):
            rc, out, err = run([cmd, "设置属性"])
            assert rc == 2 and "English" in err, f"{cmd} rc={rc} err={err[:120]}"
            assert not out.strip(), f"{cmd} printed results for a Chinese query"
        return "handler/util reject Chinese like search"

    def t_util_rank():
        rc, out, _ = run(["util", "TC XML", "--limit", "2"])
        assert rc == 0 and out.lstrip().startswith("[1] tcxml_export"), out[:120]
        return "exact name ranks first"

    def t_no_truncation():
        rc, out, _ = run(["pref", "TC_remote_checkin_assy_option", "--limit", "1"])
        assert rc == 0 and "deprecated" in out.lower(), out[:400]
        assert "tc-biz" in out, "missing site-reality note"
        return "full description kept, tc-biz note"

    def t_sq_footer():
        rc, out, _ = run(["savedquery", "BOM", "--limit", "1"])
        assert rc == 0 and "tc-biz" in out, out[-200:]
        return "site-reality note"

    def t_json_shapes():
        rc, out, _ = run(["handler", "EPM-set-property", "--limit", "1", "--json"])
        row = json.loads(out)[0]
        assert {"name", "kind", "placement", "args", "source"} <= set(row), sorted(row)
        rc, out, _ = run(["util", "tcxml_export", "--limit", "1", "--json"])
        row = json.loads(out)[0]
        assert {"name", "kind", "args", "source"} <= set(row), sorted(row)
        rc, out, _ = run(["pref", "TC_remote_checkin_assy_option", "--limit", "1", "--json"])
        row = json.loads(out)[0]
        assert {"name", "category", "data_type", "scope", "default_value", "source"} <= set(row), sorted(row)
        rc, out, _ = run(["savedquery", "BOM", "--limit", "1", "--json"])
        row = json.loads(out)[0]
        assert {"name", "query_class", "clauses", "source"} <= set(row), sorted(row)
        return "handler/util/pref/savedquery json keys"

    def t_clause_integrity():
        bad = conn.execute("SELECT COUNT(*) FROM saved_queries WHERE clauses LIKE ?",
                           ('%"Identifier "Identifier%',)).fetchone()[0]
        assert bad == 0, f"{bad} clauses were corrupted by tag stripping"
        row = conn.execute("SELECT clauses FROM saved_queries WHERE clauses LIKE ? LIMIT 1",
                           ("%<-altid_of.idfr_id%",)).fetchone()
        assert row is not None, "relation syntax is missing from clauses"
        return "relation syntax kept in clauses"

    def t_pref_and():
        expected = conn.execute("""SELECT COUNT(*) FROM prefs WHERE
            lower(name||' '||description||' '||category) LIKE '%summary%'
            AND lower(name||' '||description||' '||category) LIKE '%stylesheet%'""").fetchone()[0]
        rc, out, _ = run(["pref", "summary stylesheet", "--limit", "2"])
        assert rc == 0 and f"({expected} preference matches" in out, out[-200:]
        return f"multi-word query needs every word ({expected} rows)"

    def t_no_cut():
        row = conn.execute("SELECT name, description FROM prefs WHERE length(description) > 1200 LIMIT 1").fetchone()
        assert row is not None, "no long description to test"
        rc, out, _ = run(["pref", row["name"], "--limit", "1"])
        assert rc == 0 and row["description"] in out, "long description was cut"
        return f"{len(row['description'])}-char description printed whole"

    def t_sq_hint_and_args():
        rc, out, _ = run(["savedquery", "zzqqxx"])
        assert rc == 3 and "--class Item" in out, out[:200]
        row = conn.execute("SELECT name FROM utilities WHERE args LIKE ? LIMIT 1",
                           ("%…%",)).fetchone()
        if row:
            rc, out, _ = run(["util", row["name"], "--limit", "1"])
            assert rc == 0 and "参数表到此截断" in out, out[:300]
        return "class hint + truncated-args note"

    def t_handler_coverage():
        sections = conn.execute("""SELECT COUNT(*) FROM pages p
                                   JOIN sections s ON s.id=p.section_id
                                   WHERE s.title IN ('Action handlers','Rule handlers')""").fetchone()[0]
        copies = conn.execute("""SELECT COUNT(*) FROM pages p
                                 JOIN sections s ON s.id=p.section_id
                                 WHERE s.title IN ('Action handlers','Rule handlers')
                                   AND p.dup_of IS NOT NULL""").fetchone()[0]
        structured = conn.execute("SELECT COUNT(*) FROM handlers").fetchone()[0]
        names = {r[0] for r in conn.execute("SELECT name FROM handlers")}
        uncovered = [r[0] for r in conn.execute("""SELECT p.title FROM pages p
                                                   JOIN sections s ON s.id=p.section_id
                                                   WHERE s.title IN ('Action handlers','Rule handlers')
                                                     AND p.dup_of IS NULL
                                                     AND p.id NOT IN (SELECT page_id FROM handlers)""")]
        variants = sum(1 for t in uncovered if t in names)
        new = len(uncovered) - variants
        assert (structured, sections, copies, variants, new) == (316, 592, 149, 127, 0), (
            f"structured {structured}, sections {sections}, copies {copies}, "
            f"variants {variants}, new {new}; expected 316/592/149/127/0 - update SKILL.md, "
            "query-protocol.md, tc-ask and corpus.md if the rebuild is intentional")
        return (f"{structured} structured, {new} new, {variants} variants, "
                f"{copies} cross-book copies of {sections} pages")

    def t_base_auto_fallback():
        saved = globals()["local_alive"]
        try:
            globals()["local_alive"] = lambda *a, **k: False
            _BASE_CACHE.pop("auto", None)
            assert choose_base("auto") == "online", "dead mirror must fall back to online"
            globals()["local_alive"] = lambda *a, **k: True
            _BASE_CACHE.pop("auto", None)
            assert choose_base("auto") == "local", "live mirror must be preferred"
        finally:
            globals()["local_alive"] = saved
            _BASE_CACHE.pop("auto", None)
        return "auto: mirror alive -> local, dead -> online"

    def t_sq_class_exact():
        rc, out, _ = run(["savedquery", "--class", "Item", "--limit", "50", "--json"])
        assert rc == 0, f"--class Item exited {rc}"
        rows = json.loads(out)
        bad = [r["name"] for r in rows if r["query_class"].lower() != "item"]
        assert not bad, f"non-Item classes leaked: {bad[:3]}"
        rc, out, _ = run(["savedquery", "--class", "ItemRevisio"])
        assert rc == 3 and "close classes" in out, out[:200]
        return "class listing is exact, near miss suggests classes"

    def t_phrase_search():
        assert build_match('"check out"') == '"check out"', build_match('"check out"')
        assert build_match("check out") == '"check" "out"', build_match("check out")
        rc, out, _ = run(["search", '"check out"', "--limit", "2"])
        assert rc == 0 and "src:" in out, out[:200]
        return "quoted phrase stays one phrase"

    def t_pref_warnings():
        rc, out, _ = run(["pref", "TC_remote_checkin_assy_option", "--limit", "1"])
        assert rc == 0 and "⚠ 已弃用" in out, out[:300]
        rc, out, _ = run(["pref", "Acp0SPCCheckRevision_PseudoFolder", "--limit", "1"])
        assert rc == 0 and "⚠ 内部项" in out, out[:300]
        rc, out, _ = run(["pref", "TC_remote_checkin_assy_option", "--limit", "1", "--json"])
        assert json.loads(out)[0]["warnings"], "json has no warnings"
        return "deprecated / internal flags surfaced"

    def t_page_title_dups():
        same = conn.execute("""SELECT title FROM pages GROUP BY title
                               HAVING COUNT(*)>1 AND COUNT(DISTINCT href)=1 LIMIT 1""").fetchone()
        assert same, "no same-href duplicate title to test"
        rc, out, _ = run(["page", same[0], "--max-chars", "60"])
        assert rc == 0, f"same-href title exited {rc}: {out[:120]}"
        multi = conn.execute("""SELECT title FROM pages GROUP BY title
                                HAVING COUNT(DISTINCT href)>1 LIMIT 1""").fetchone()
        rc, out, _ = run(["page", multi[0], "--max-chars", "60"])
        assert rc == 3 and "pass one href" in out, out[:200]
        return "same-href title reads, distinct hrefs list candidates"

    def t_lookup_noise():
        rc, _, _ = run(["handler", "ZZZ-no-such-handler-xyz"])
        assert rc == 3, f"fake hyphenated name returned rc={rc}"
        rc, _, _ = run(["handler", "zzz-no-such-handler"])
        assert rc == 3, f"short noise name returned rc={rc}"
        rc, out, _ = run(["handler", "EPM-set-proprty", "--limit", "1"])
        assert rc == 0 and out.lstrip().startswith("[1] EPM-set-property"), out[:120]
        return "fake names miss, typo finds the real name"

    def t_bad_db():
        rc, _, err = run(["--db", str(SKILL_ROOT / "README.md"), "map"])
        assert rc == 4 and "not a tc-help database" in err, f"rc={rc} {err[:120]}"
        return "non-sqlite file -> rc 4, no traceback"

    check("meta", t_meta)
    check("page counts", t_counts)
    check("english only", t_cjk)
    check("reject chinese query", t_reject_cjk)
    check("reject stopword query", t_reject_stopwords)
    check("known answer in top5", t_known_page)
    check("prefix search", t_prefix)
    check("copies marked", t_copies)
    check("dedupe in search", t_dedupe)
    check("read page by href", t_page_by_href)
    check("read page by title", t_page_by_title)
    check("original url", t_url)
    check("no invented vendor link", t_no_online_link)
    check("source for unlinked pages", t_no_url_source)
    check("category map", t_map)
    check("domain map in sync", t_map_doc)
    check("exit codes", t_exit_codes)
    check("--limit bounds", t_limit_bounds)
    check("page --json truncation", t_page_json_truncates)
    check("ambiguous title count", t_ambiguous_title)
    check("--cat values", t_cat_values)
    check("--book literal", t_book_wildcard)
    check("multi-version choice", t_multi_version)
    check("missing db hint", t_missing_db_hint)
    check("link base forced", t_base_forced)
    check("handler/util tables", t_index_tables)
    check("handler by name", t_handler_lookup)
    check("utility lookup", t_util_lookup)
    check("search --client", t_search_client)
    check("search --cite", t_search_cite)
    check("page --grep", t_page_grep)
    check("zero hit close titles", t_fuzzy)
    check("preference table", t_pref_table)
    check("preference by name", t_pref_exact)
    check("preference by description", t_pref_desc)
    check("preference category", t_pref_category)
    check("preference rejects chinese", t_pref_cjk)
    check("saved query table", t_sq_table)
    check("saved query by name", t_sq_exact)
    check("saved query by clause", t_sq_clause)
    check("saved query rejects chinese", t_sq_cjk)
    check("lookup rejects chinese", t_lookup_cjk_rejected)
    check("utility ranking", t_util_rank)
    check("no text truncation", t_no_truncation)
    check("saved query note", t_sq_footer)
    check("json shapes", t_json_shapes)
    check("clause integrity", t_clause_integrity)
    check("preference multi-word", t_pref_and)
    check("long text not cut", t_no_cut)
    check("query hint and args note", t_sq_hint_and_args)
    check("handler coverage", t_handler_coverage)
    check("auto base fallback", t_base_auto_fallback)
    check("saved query class exact", t_sq_class_exact)
    check("phrase search", t_phrase_search)
    check("preference warnings", t_pref_warnings)
    check("page title duplicates", t_page_title_dups)
    check("lookup noise", t_lookup_noise)
    check("bad database file", t_bad_db)

    print(f"database: {db_path}")
    ok = 0
    for good, name, detail in checks:
        print(f"  [{'PASS' if good else 'FAIL'}] {name:<24} {detail}")
        ok += good
    print(f"\n{ok}/{len(checks)} passed")
    return 0 if ok == len(checks) else 1


# ── entry point ─────────────────────────────────────────────────────

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="database path (default: data/<version>/tc_help_<version>.db)")
    ap.add_argument("--version", default=None)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--base", choices=("auto", "local", "online", "package"), default="auto",
                    help="src line: local mirror URL, public docs URL, or package-relative path")
    sub = ap.add_subparsers(dest="cmd")

    def add_base(p):
        # --base is global, but the documented usage puts it after the subcommand;
        # SUPPRESS keeps the global value when the subcommand does not set it.
        p.add_argument("--base", choices=("auto", "local", "online", "package"), default=argparse.SUPPRESS,
                       help="src line: local mirror URL, public docs URL, or package-relative path")

    p = sub.add_parser("map", help="categories, or the books inside one category")
    p.add_argument("--cat")
    p.add_argument("--md", action="store_true", help="print the whole map as markdown")

    p = sub.add_parser("search", help="full-text search (English terms)")
    p.add_argument("query", nargs="+")
    p.add_argument("--cat")
    p.add_argument("--topic", type=int, help="restrict to one book id")
    p.add_argument("--book", help="restrict to a book id or part of its title")
    p.add_argument("--client", choices=("awc", "rc", "admin"),
                   help="only books for that client (Active Workspace / rich client / admin)")
    p.add_argument("--group", action="store_true", help="group results by book")
    p.add_argument("--cite", action="store_true", help="print the two citation lines for each hit")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    add_base(p)

    for name, desc in (("handler", "workflow handler: placement, key arguments"),
                       ("util", "command-line utility: key arguments")):
        p = sub.add_parser(name, help=desc)
        p.add_argument("query", nargs="+")
        p.add_argument("--limit", type=int, default=5)
        p.add_argument("--cite", action="store_true", help="print the two citation lines for each hit")
        p.add_argument("--json", action="store_true")
        add_base(p)

    p = sub.add_parser("pref", help="preference definition: category, data type, scope, default value")
    p.add_argument("query", nargs="*")
    p.add_argument("--category", help="list preferences whose category contains this text")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--cite", action="store_true", help="print the two citation lines for each hit")
    p.add_argument("--json", action="store_true")
    add_base(p)

    p = sub.add_parser("savedquery", help="saved query: query class, description, clauses")
    p.add_argument("query", nargs="*")
    p.add_argument("--class", dest="query_class", help="list queries for this query class")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--cite", action="store_true", help="print the two citation lines for each hit")
    p.add_argument("--json", action="store_true")
    add_base(p)

    p = sub.add_parser("topic", help="table of contents of one book")
    p.add_argument("topic_id", type=int)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--grep", help="only show sections/pages matching this text")

    p = sub.add_parser("page", help="read one page by href or exact title")
    p.add_argument("ref")
    p.add_argument("--max-chars", type=int, default=12000)
    p.add_argument("--grep", help="only show the passages containing this text")
    p.add_argument("--cite", action="store_true", help="print the two citation lines")
    p.add_argument("--json", action="store_true")
    add_base(p)

    args = ap.parse_args(argv)
    db_path = find_db(args.db, args.version)

    if args.selftest:
        return selftest(db_path)
    if not args.cmd:
        ap.print_help()
        return 2

    conn = connect(db_path)
    if args.cmd == "map":
        return cmd_map(conn, args)
    if args.cmd == "search":
        return cmd_search(conn, args)
    if args.cmd in ("handler", "util"):
        return cmd_lookup(conn, args, args.cmd)
    if args.cmd == "pref":
        return cmd_pref(conn, args)
    if args.cmd == "savedquery":
        return cmd_savedquery(conn, args)
    if args.cmd == "topic":
        return cmd_topic(conn, args)
    if args.cmd == "page":
        return cmd_page(conn, args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
