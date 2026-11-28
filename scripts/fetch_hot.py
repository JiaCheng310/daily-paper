#!/usr/bin/env python3
"""Daily list = the popular papers on Hugging Face Daily Papers and alphaXiv.

Candidates:
  - Hugging Face Daily Papers, every listing date in the window (upvotes)
  - alphaXiv's Hot / Views / Likes feeds (visits, upvotes); only Computer Science papers
  - the same alphaXiv feeds for the reinforcement-learning topics, for the RL section
Metadata (title, authors, abstract, categories, date) comes from the arXiv API.
Papers are grouped by the Beijing date on which they were first listed: the Hugging Face
Daily Papers date when HF lists the paper, otherwise the morning after arXiv announces it
(arXiv announces Sunday-Thursday 20:00 US Eastern = Monday-Friday morning in Beijing).
A paper keeps the day it was first given, and a day's list only ever grows.

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
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script dir out
from fetch_demo import CATEGORY_NAMES, demo_score, parse_arxiv  # noqa: E402

UA = "daily-paper/0.1 (+https://github.com/JiaCheng310/daily-paper)"
ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}$")
VISIT_WEIGHT = 0.1  # same as popularity.py
SOURCE = "hot"  # day files written by this script (older ones held one arXiv category)
EASTERN = ZoneInfo("America/New_York")
CUTOFF = dt.time(14, 0)  # arXiv's daily submission deadline, US Eastern
# Sections besides the main list: id -> (alphaXiv topics, title/abstract pattern).
SECTIONS = {
    "rl": (("reinforcement-learning", "deep-reinforcement-learning"),
           re.compile(r"reinforcement[- ]learning|\bRL(?:HF|AIF|VR)?\b|\bGRPO\b|\bPPO\b|policy optimi[sz]ation|"
                      r"policy gradient|reward model|\bQ-learning\b|actor-critic", re.I)),
}


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


def listing_date(published):
    """Beijing date of the morning after arXiv announces a paper submitted at `published`.
    Submissions up to 14:00 Eastern on Monday-Thursday are announced at 20:00 that day;
    Friday's go out on Sunday. 20:00 Eastern is the next morning in Beijing. arXiv holidays
    are not known here; Hugging Face's dates cover most papers anyway."""
    t = dt.datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(EASTERN)
    deadline = t.date() + dt.timedelta(days=t.time() >= CUTOFF)
    while deadline.weekday() >= 5:  # Saturday, Sunday: next Monday's deadline
        deadline += dt.timedelta(days=1)
    announce = deadline + dt.timedelta(days=2 if deadline.weekday() == 4 else 0)
    return (announce + dt.timedelta(days=1)).isoformat()


def hf_candidates(dates):
    """{id: upvotes}, {id: first listing date} from HF Daily Papers listings."""
    votes, listed = {}, {}
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
                listed[pid] = min(listed.get(pid, date), date)
        time.sleep(0.5)
    return votes, listed


def alphaxiv_candidates(interval, pages, page_size=100, sorts=("Hot", "Views", "Likes"), topics=None):
    """{id: (votes, visits)} from alphaXiv's feeds (hot, most viewed, most liked), Computer
    Science only. Three orderings, so a paper popular by any measure is a candidate.
    `topics` limits the feeds to alphaXiv topics such as "reinforcement-learning"."""
    found = {}
    for sort in sorts:
        found.update(alphaxiv_feed(sort, interval, pages, page_size, topics))
    return found


def alphaxiv_feed(sort, interval, pages, page_size, topics=None):
    found = {}
    for page in range(pages):
        params = {"pageNum": page, "sort": sort, "interval": interval, "pageSize": page_size}
        if topics:
            params["topics"] = json.dumps(list(topics))
        query = urllib.parse.urlencode(params)
        try:
            data = json.loads(get(f"https://api.alphaxiv.org/papers/v3/feed?{query}"))
        except Exception as exc:
            print(f"alphaXiv {sort} page {page} skipped: {exc}", file=sys.stderr)
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
    # A paper often climbs for days after it appears, so a week of days is rebuilt.
    ap.add_argument("--days", type=int, default=7, help="newest listing days that take new papers")
    ap.add_argument("--interval", default="7 Days", help="alphaXiv feed window: 3 Days, 7 Days, 30 Days")
    ap.add_argument("--pages", type=int, default=3, help="alphaXiv pages of 100 per feed")
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    out = Path(args.out)

    beijing = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
    hf, hf_listed = hf_candidates([(beijing - dt.timedelta(days=d)).isoformat() for d in range(args.days + 4)])
    ax = alphaxiv_candidates(args.interval, args.pages)
    section_ids = {}
    for name, (topics, _) in SECTIONS.items():
        found = alphaxiv_candidates(args.interval, args.pages, topics=topics)
        section_ids[name] = set(found)
        for pid, row in found.items():
            ax.setdefault(pid, row)
        print(f"{name}: {len(found)} from alphaXiv topics {', '.join(topics)}")
    print(f"candidates: {len(hf)} from Hugging Face, {len(ax)} from alphaXiv, {len(set(hf) | set(ax))} together")
    meta = arxiv_metadata(set(hf) | set(ax))
    if not meta:
        sys.exit("arXiv returned no metadata")

    # Everything already on the site keeps its day; new candidates join the newest days.
    old = {}
    for path in (out / "days").glob("*.json"):
        day = json.loads(path.read_text())
        if day.get("source") == SOURCE:
            for p in day["papers"]:
                old.setdefault(p["id"], p).setdefault("listed", hf_listed.get(p["id"]) or listing_date(p["published"]))
    for pid, paper in meta.items():
        paper["listed"] = old[pid]["listed"] if pid in old else hf_listed.get(pid) or listing_date(paper["published"])
    newest = sorted({p["listed"] for p in meta.values()}, reverse=True)[: args.days]
    by_day = {}
    for p in old.values():
        by_day.setdefault(p["listed"], {})[p["id"]] = p
    for pid, paper in meta.items():
        if pid in old or paper["listed"] in newest:
            by_day.setdefault(paper["listed"], {})[pid] = {**old.get(pid, {}), **paper}
    days = sorted(by_day, reverse=True)

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
        papers = by_day[date]
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
            paper["sections"] = sorted(
                name for name, (_, pattern) in SECTIONS.items()
                if paper["id"] in section_ids[name] or name in (paper.get("sections") or [])
                or pattern.search(f"{paper['title']}\n{paper.get('abstract', '')}")
                or (name == "rl" and "RL" in (paper.get("tags") or [])))
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

    # Files for dates that no longer hold any paper (older layouts: one arXiv category, or
    # days by submission date) are dropped, so the archive holds one kind of list.
    for path in (out / "days").glob("*.json"):
        if path.stem not in by_day:
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
        "categories": [{"id": "all", "name": "Popular papers", "name_zh": "热门论文"},
                       {"id": "rl", "name": "Reinforcement learning", "name_zh": "强化学习"}],
        "category_names": {c: {"en": CATEGORY_NAMES[c][0], "zh": CATEGORY_NAMES[c][1]} for c in cats if c in CATEGORY_NAMES},
        "days": index_days,
    }
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
