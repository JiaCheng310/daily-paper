#!/usr/bin/env python3
"""Daily list = the popular papers on Hugging Face Daily Papers and alphaXiv.

Candidates:
  - Hugging Face Daily Papers, every listing date in the window (upvotes)
  - alphaXiv's "Hot" feed (visits, upvotes); only Computer Science papers
Metadata (title, authors, abstract, categories, date) comes from the arXiv API.
Papers are grouped by their arXiv submission date (UTC), like arXiv itself; a day's
list only ever grows, so a paper that was popular once stays on its day.

Writes data/days/<date>.json and data/index.json, merging the caches written by
popularity.py (upvotes), enrich.py (previews, organizations) and summarize_gemini.py.

    python3 -I scripts/fetch_hot.py --days 3
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script dir out
from fetch_demo import CATEGORY_NAMES, demo_score, parse_arxiv  # noqa: E402

UA = "daily-paper/0.1 (+https://github.com/JiaCheng310/daily-paper)"
ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}$")
VISIT_WEIGHT = 0.1  # same as popularity.py
SOURCE = "hot"  # day files written by this script (older ones held one arXiv category)


def get(url, retries=3, timeout=60):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or attempt == retries or exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(3 * (attempt + 1))


def hf_candidates(dates):
    """{id: upvotes} from HF Daily Papers listings."""
    votes = {}
    for date in dates:
        try:
            items = json.loads(get(f"https://huggingface.co/api/daily_papers?date={date}&limit=100"))
        except Exception as exc:
            print(f"HF {date} skipped: {exc}", file=sys.stderr)
            continue
        for item in items:
            paper = item.get("paper") or {}
            pid, up = paper.get("id"), paper.get("upvotes")
            if pid and ARXIV_ID.match(pid):
                votes[pid] = max(votes.get(pid, 0), up if isinstance(up, int) else 0)
        time.sleep(0.5)
    return votes


def alphaxiv_candidates(interval, pages, page_size=100):
    """{id: (votes, visits)} from alphaXiv's Hot feed, Computer Science only."""
    found = {}
    for page in range(pages):
        query = urllib.parse.urlencode({"pageNum": page, "sort": "Hot", "interval": interval, "pageSize": page_size})
        try:
            data = json.loads(get(f"https://api.alphaxiv.org/papers/v3/feed?{query}"))
        except Exception as exc:
            print(f"alphaXiv page {page} skipped: {exc}", file=sys.stderr)
            break
        papers = data.get("papers") or []
        for p in papers:
            pid = p.get("universal_paper_id") or ""
            if not ARXIV_ID.match(pid) or "Computer Science" not in (p.get("topics") or []):
                continue
            m = p.get("metrics") or {}
            found[pid] = (int(m.get("public_total_votes") or 0), int((m.get("visits_count") or {}).get("all") or 0))
        if len(papers) < page_size:
            break
        time.sleep(1)
    return found


def arxiv_metadata(ids):
    """arXiv API, 100 ids per request, 3 s apart (arXiv's guideline)."""
    out = {}
    ids = sorted(ids)
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        query = urllib.parse.urlencode({"id_list": ",".join(chunk), "max_results": len(chunk)})
        for paper in parse_arxiv(get(f"https://export.arxiv.org/api/query?{query}", timeout=120)):
            out[paper["id"]] = paper
        time.sleep(3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="arXiv submission days to (re)build, newest first")
    ap.add_argument("--interval", default="7 Days", help="alphaXiv feed window: 3 Days, 7 Days, 30 Days")
    ap.add_argument("--pages", type=int, default=4, help="alphaXiv feed pages of 100")
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    out = Path(args.out)

    today = dt.datetime.now(dt.timezone.utc).date()
    hf = hf_candidates([(today - dt.timedelta(days=d)).isoformat() for d in range(args.days + 4)])
    ax = alphaxiv_candidates(args.interval, args.pages)
    print(f"candidates: {len(hf)} from Hugging Face, {len(ax)} from alphaXiv, {len(set(hf) | set(ax))} together")
    meta = arxiv_metadata(set(hf) | set(ax))
    if not meta:
        sys.exit("arXiv returned no metadata")

    by_day = {}
    for paper in meta.values():
        by_day.setdefault(paper["published"][:10], {})[paper["id"]] = paper
    days = sorted(by_day, reverse=True)[: args.days]

    def cache(name):
        path = out / name
        return json.loads(path.read_text()) if path.exists() else {}
    llm, enrich, pop = cache("llm.json"), cache("enrich.json"), cache("popularity.json")

    # Fresh numbers from the candidate lists go into the popularity cache too.
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for pid in meta:
        row = pop.setdefault(pid, {})
        if pid in hf:
            row["hf"] = max(row.get("hf") or 0, hf[pid])
        if pid in ax:
            row["ax_votes"], row["ax_visits"] = ax[pid]
            row["checked_at"] = now
    (out / "popularity.json").write_text(json.dumps(pop, ensure_ascii=False, indent=1, sort_keys=True))

    (out / "days").mkdir(parents=True, exist_ok=True)
    for date in days:
        path = out / "days" / f"{date}.json"
        old = json.loads(path.read_text()) if path.exists() else {}
        papers = dict(by_day[date])
        if old.get("source") == SOURCE:  # keep everything that was on this day before
            for p in old["papers"]:
                papers.setdefault(p["id"], p)
        rows = []
        for paper in papers.values():
            entry = llm.get(paper["id"])
            if entry:
                paper["tags"], paper["summary"], paper["summary_source"] = entry["tags"], entry["summary"], entry["model"]
            elif not paper.get("tags"):
                _, paper["tags"] = demo_score(paper)
            row = pop.get(paper["id"], {})
            if row.get("hf"):
                paper["hf_upvotes"] = row["hf"]
            paper["ax_votes"] = row.get("ax_votes") or 0
            paper["ax_visits"] = row.get("ax_visits") or 0
            paper["heat"] = round((row.get("hf") or 0) + paper["ax_votes"] + VISIT_WEIGHT * paper["ax_visits"], 1)
            extra = enrich.get(paper["id"])
            if extra and "orgs" not in paper:
                paper["orgs"] = [{"name": o} if isinstance(o, str) else o for o in extra.get("orgs", [])]
            if extra and extra.get("thumb"):
                paper["thumb"], paper["thumb_w"], paper["thumb_h"] = extra["thumb"], extra["w"], extra["h"]
            rows.append(paper)
        rows.sort(key=lambda p: (-p["heat"], p["id"]))
        path.write_text(json.dumps({"date": date, "category": "all", "source": SOURCE, "papers": rows},
                                   ensure_ascii=False, indent=1))
        print(f"{date}: {len(rows)} papers")

    # Day files from before the switch (one arXiv category, hundreds of papers) are dropped
    # when they are not rebuilt, so the archive holds one kind of list.
    for path in (out / "days").glob("*.json"):
        if json.loads(path.read_text()).get("source") != SOURCE:
            path.unlink()
            print(f"removed old-style {path.name}")

    index_days = sorted(({"date": p.stem, "count": len(json.loads(p.read_text())["papers"])}
                         for p in (out / "days").glob("*.json")), key=lambda d: d["date"], reverse=True)
    cats = sorted({c for p in (out / "days").glob("*.json")
                   for paper in json.loads(p.read_text())["papers"] for c in paper["categories"]})
    index = {
        "title": "Daily Paper",
        "updated_at": now,
        "default_category": "all",
        "categories": [{"id": "all", "name": "Popular papers", "name_zh": "热门论文"}],
        "category_names": {c: {"en": CATEGORY_NAMES[c][0], "zh": CATEGORY_NAMES[c][1]} for c in cats if c in CATEGORY_NAMES},
        "days": index_days,
    }
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
