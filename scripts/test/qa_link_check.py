#!/usr/bin/env python3
"""Check that every原文链接 in a qa_run.py report actually opens.

    python scripts/test/qa_link_check.py --report <report.md> [--workers 8]

200 = 页面可点；其它状态一律点名。Siemens Web Framework 那本的链接走官网
（本机 5000 镜像没收录该集合），直连会跳登录，属于已知缺口，报告里必须见数。
"""

import argparse
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

LOGIN_REDIRECT = re.compile(r"diswlogin|/login", re.I)


def check(url, timeout):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(20000).decode("utf-8", "replace")
            title = re.search(r"<title>(.*?)</title>", body, re.S)
            return url, resp.status, (title.group(1).strip()[:60] if title else "(no title)")
    except Exception as exc:  # noqa: BLE001 - the report is the point, not the exception type
        where = getattr(exc, "url", "") or str(exc)
        kind = "跳登录" if LOGIN_REDIRECT.search(where) else type(exc).__name__
        return url, getattr(exc, "code", kind), str(where)[:70]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    text = open(args.report, encoding="utf-8").read()
    urls = sorted(set(re.findall(r"\]\((https?://[^)\s]+)\)", text)))
    if not urls:
        print(f"no links found in {args.report}")
        return 2
    bad = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for url, status, detail in pool.map(lambda u: check(u, args.timeout), urls):
            if status != 200:
                bad.append((url, status, detail))
    print(f"{args.report}: {len(urls)} 条链接，可点 {len(urls) - len(bad)}，打不开 {len(bad)}")
    for url, status, detail in bad:
        print(f"  [{status}] {detail}  {url}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
