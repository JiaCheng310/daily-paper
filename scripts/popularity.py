"""Popularity for recent papers: Hugging Face upvotes plus alphaXiv votes and visits.

For every paper in data/days/*.json from the last --window days (counted back from
the newest day), asks:
  - Hugging Face:  https://huggingface.co/api/papers/<id>         -> upvotes (404: not on HF)
  - alphaXiv:      https://api.alphaxiv.org/v2/papers/<id>/metadata -> votes, visits
The alphaXiv endpoint is undocumented, so every failure is tolerated: the previous
value is kept and the run still succeeds.

Writes:
  data/popularity.json  cache {id: {hf, ax_votes, ax_visits, checked_at}}; fetch_demo.py
                        merges it back when it rebuilds the day files
  data/days/*.json      hf_upvotes, ax_votes, ax_visits, heat (= hf + ax_votes) per paper
  data/popular.json     the top papers of the last 7 and 30 days by heat

    python3 -I scripts/popularity.py --window 30 --workers 4
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "daily-paper/0.1 (+https://github.com/JiaCheng310/daily-paper)"
PERIODS = (7, 30)
TOP_N = 20


def get_json(url, retries=2):
    """JSON body, None on 404, raises on anything else after retries."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == retries or exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(2 * (attempt + 1))


def hf(pid):
    data = get_json(f"https://huggingface.co/api/papers/{pid}")
    up = (data or {}).get("upvotes")
    return up if isinstance(up, int) else 0


def alphaxiv(pid):
    data = get_json(f"https://api.alphaxiv.org/v2/papers/{pid}/metadata")
    if data is None:
        return 0, 0
    metrics = data["data"]["paper_group"]["metrics"]
    visits = metrics.get("visits_count") or {}
    return int(metrics.get("public_total_votes") or 0), int(visits.get("all") or 0)


def check(pid, old, delay):
    """Fresh numbers for one paper; any source that fails keeps its old value."""
    row = dict(old)
    ok = False
    try:
        row["hf"] = hf(pid)
        ok = True
    except Exception as exc:
        print(f"HF {pid}: {exc}", file=sys.stderr)
    time.sleep(delay)
    try:
        row["ax_votes"], row["ax_visits"] = alphaxiv(pid)
        ok = True
    except Exception as exc:
        print(f"alphaXiv {pid}: {exc}", file=sys.stderr)
    time.sleep(delay)
    if ok:
        row["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return pid, row, ok


def heat(row):
    return (row.get("hf") or 0) + (row.get("ax_votes") or 0)


def apply(paper, row):
    """Copy cached numbers onto a paper dict (shared with fetch_demo.py)."""
    if not row:
        return
    if row.get("hf"):
        paper["hf_upvotes"] = row["hf"]
    else:
        paper.pop("hf_upvotes", None)
    paper["ax_votes"] = row.get("ax_votes") or 0
    paper["ax_visits"] = row.get("ax_visits") or 0
    paper["heat"] = heat(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--window", type=int, default=30, help="days back from the newest day to refresh")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.3, help="pause after each request, per worker")
    args = ap.parse_args()
    data = Path(args.data)

    day_files = sorted((data / "days").glob("*.json"), reverse=True)
    if not day_files:
        sys.exit("no day files")
    newest = dt.date.fromisoformat(day_files[0].stem)
    in_window = [f for f in day_files if (newest - dt.date.fromisoformat(f.stem)).days < args.window]

    cache_path = data / "popularity.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    days = {f: json.loads(f.read_text()) for f in in_window}
    ids = list(dict.fromkeys(p["id"] for d in days.values() for p in d["papers"]))
    print(f"{len(ids)} papers in {len(in_window)} days ({in_window[-1].stem} .. {in_window[0].stem})")

    failed = 0
    with cf.ThreadPoolExecutor(args.workers) as ex:
        jobs = [ex.submit(check, pid, cache.get(pid, {}), args.delay) for pid in ids]
        for n, job in enumerate(cf.as_completed(jobs), 1):
            pid, row, ok = job.result()
            cache[pid] = row
            failed += not ok
            if n % 50 == 0:
                print(f"[{n}/{len(ids)}]", flush=True)
    if failed == len(ids):
        sys.exit("every request failed; leaving data unchanged")

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))
    for f, day in days.items():
        for paper in day["papers"]:
            apply(paper, cache.get(paper["id"]))
        f.write_text(json.dumps(day, ensure_ascii=False, indent=1))

    # Popular: each paper once (its first day), ranked by heat, then alphaXiv visits.
    first_day = {}
    papers = {}
    for f in sorted(in_window):
        for paper in days[f]["papers"]:
            if paper["id"] not in papers:
                papers[paper["id"]], first_day[paper["id"]] = paper, f.stem
    popular = {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "newest": newest.isoformat(), "periods": {}}
    for period in PERIODS:
        pool = [p for pid, p in papers.items()
                if (newest - dt.date.fromisoformat(first_day[pid])).days < period and p.get("heat", 0) > 0]
        pool.sort(key=lambda p: (-p.get("heat", 0), -p.get("ax_visits", 0), p["id"]))
        popular["periods"][str(period)] = {
            "from": (newest - dt.timedelta(days=period - 1)).isoformat(),
            "days": len({first_day[p["id"]] for p in papers.values()
                         if (newest - dt.date.fromisoformat(first_day[p["id"]])).days < period}),
            "papers": pool[:TOP_N],
        }
    (data / "popular.json").write_text(json.dumps(popular, ensure_ascii=False, indent=1))
    print(f"done: {len(ids) - failed} updated, {failed} kept old values; "
          + ", ".join(f"{k}d top {len(v['papers'])}" for k, v in popular["periods"].items()))


if __name__ == "__main__":
    main()
