#!/usr/bin/env python3
"""Build demo data for the Daily Paper frontend.

Paper metadata comes from the arXiv API; upvotes come from the Hugging Face
Daily Papers API. The relevance score here is a transparent keyword rule used
only for the demo; the production pipeline will replace it with an LLM score.

Usage (from the repository root):
    python3 -I scripts/fetch_demo.py --cat cs.RO --days 3
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
UA = "daily-paper-demo/0.1 (+https://github.com/JiaCheng310/daily-paper)"

CATEGORY_NAMES = {
    "cs.RO": ("Robotics", "机器人"),
    "cs.CV": ("Computer Vision and Pattern Recognition", "计算机视觉"),
    "cs.LG": ("Machine Learning", "机器学习"),
    "cs.AI": ("Artificial Intelligence", "人工智能"),
    "cs.CL": ("Computation and Language", "计算语言学"),
    "cs.HC": ("Human-Computer Interaction", "人机交互"),
    "cs.MA": ("Multiagent Systems", "多智能体系统"),
    "cs.GR": ("Graphics", "图形学"),
    "cs.SY": ("Systems and Control", "系统与控制"),
    "eess.SY": ("Systems and Control", "系统与控制"),
    "eess.IV": ("Image and Video Processing", "图像与视频处理"),
    "math.OC": ("Optimization and Control", "优化与控制"),
}

# (pattern, weight, tag). Each rule counts once per paper.
RULES = [
    (r"vision[- ]language[- ]action|\bVLAs?\b", 3.0, "VLA"),
    (r"world (action )?models?", 2.0, "world model"),
    (r"dexter", 2.0, "dexterous"),
    (r"manipulat", 2.0, "manipulation"),
    (r"humanoid", 2.0, "humanoid"),
    (r"sim[- ]?(to|2)[- ]?real", 2.0, "sim2real"),
    (r"grasp", 1.5, "grasping"),
    (r"imitation learning|behaviou?r cloning|demonstrations?", 1.5, "imitation"),
    (r"diffusion polic|flow[- ]matching polic", 1.5, "diffusion policy"),
    (r"locomotion|legged|quadruped", 1.5, "locomotion"),
    (r"embodied", 1.5, "embodied"),
    (r"reinforcement learning|\bRL\b", 1.0, "RL"),
    (r"\bVLMs?\b|\bLLMs?\b|vision[- ]language model|foundation model", 1.0, "VLM/LLM"),
    (r"teleoperat", 1.0, "teleop"),
    (r"tactile", 1.0, "tactile"),
    (r"navigation", 0.8, "navigation"),
    (r"benchmark", 0.5, "benchmark"),
    (r"real[- ]world|real robot|physical robot", 0.5, None),
    (r"\bUAVs?\b|drone|quadrotor|aerial", -1.5, "aerial"),
    (r"autonomous driving|\bvehicles?\b", -1.0, "driving"),
]
COMPILED = [(re.compile(p, re.I), w, t) for p, w, t in RULES]
GITHUB_RE = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+", re.I)


def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as exc:  # network hiccups: back off and retry
            if attempt == retries - 1:
                raise
            print(f"retry {url}: {exc}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def demo_score(paper):
    text = f"{paper['title']} {paper['abstract']}"
    score, tags = 0.0, []
    for rx, weight, tag in COMPILED:
        if rx.search(text):
            score += weight
            if tag:
                tags.append((weight, tag))
    if paper["primary"] == "cs.RO":
        score += 1.0
    score = max(0.0, min(10.0, round(score * 2) / 2))
    tags = [t for _, t in sorted(tags, key=lambda x: -x[0])][:4]
    return score, tags


def parse_arxiv(xml_bytes):
    root = ET.fromstring(xml_bytes)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        abs_url = entry.findtext(f"{ATOM}id", "")
        match = re.search(r"abs/([^v]+)(v\d+)?$", abs_url)
        if not match:
            continue
        primary = entry.find(f"{ARXIV}primary_category")
        cats = [c.get("term") for c in entry.findall(f"{ATOM}category")]
        primary_term = primary.get("term") if primary is not None else (cats[0] if cats else "")
        cats = [primary_term] + [c for c in cats if c != primary_term]
        paper = {
            "id": match.group(1),
            "title": clean(entry.findtext(f"{ATOM}title")),
            "authors": [clean(a.findtext(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")],
            "abstract": clean(entry.findtext(f"{ATOM}summary")),
            "primary": primary_term,
            "categories": cats,
            "published": entry.findtext(f"{ATOM}published", ""),
            "updated": entry.findtext(f"{ATOM}updated", ""),
            "comment": clean(entry.findtext(f"{ARXIV}comment")),
        }
        code = GITHUB_RE.search(f"{paper['abstract']} {paper['comment']}")
        if code:
            paper["code_url"] = code.group(0).rstrip(".")
        papers.append(paper)
    return papers


def hf_upvotes(dates):
    votes = {}
    for date in dates:
        url = f"https://huggingface.co/api/daily_papers?date={date}&limit=100"
        try:
            items = json.loads(fetch(url))
        except Exception as exc:
            print(f"HF {date} skipped: {exc}", file=sys.stderr)
            continue
        for item in items:
            paper = item.get("paper", {})
            pid, up = paper.get("id"), paper.get("upvotes")
            if pid and isinstance(up, int):
                votes[pid] = max(votes.get(pid, 0), up)
        time.sleep(1)
    return votes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="cs.RO")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--max", type=int, default=400)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()
    out = Path(args.out)

    query = urllib.parse.urlencode({
        "search_query": f"cat:{args.cat}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": args.max,
    })
    papers = parse_arxiv(fetch(f"https://export.arxiv.org/api/query?{query}"))
    if not papers:
        sys.exit("arXiv returned no entries")

    by_day = {}
    for paper in papers:
        by_day.setdefault(paper["published"][:10], []).append(paper)
    days = sorted(by_day, reverse=True)
    # The oldest fetched day is usually cut off by max_results; drop it.
    if len(days) > 1:
        days = days[:-1]
    days = days[: args.days]

    newest = dt.date.fromisoformat(days[0])
    hf_dates = [(newest + dt.timedelta(days=d)).isoformat() for d in range(-args.days, 3)]
    votes = hf_upvotes(hf_dates)

    enrich_path = out / "enrich.json"  # written by scripts/enrich.py
    enrich = json.loads(enrich_path.read_text()) if enrich_path.exists() else {}
    llm_path = out / "llm.json"  # written by scripts/summarize.py
    llm = json.loads(llm_path.read_text()) if llm_path.exists() else {}
    pop_path = out / "popularity.json"  # written by scripts/popularity.py
    pop = json.loads(pop_path.read_text()) if pop_path.exists() else {}

    (out / "days").mkdir(parents=True, exist_ok=True)
    index_days = []
    for date in days:
        rows = []
        for paper in by_day[date]:
            if paper["id"] in llm:  # written by scripts/summarize_gemini.py
                entry = llm[paper["id"]]
                paper["tags"], paper["summary"] = entry["tags"], entry["summary"]
                paper["summary_source"] = entry["model"]
            else:
                _, paper["tags"] = demo_score(paper)  # keyword tags until the summarizer runs
            row = pop.get(paper["id"], {})
            hf_up = max(votes.get(paper["id"], 0), row.get("hf") or 0)
            if hf_up:
                paper["hf_upvotes"] = hf_up
            if row:
                paper["ax_votes"] = row.get("ax_votes") or 0
                paper["ax_visits"] = row.get("ax_visits") or 0
            paper["heat"] = hf_up + (row.get("ax_votes") or 0)
            extra = enrich.get(paper["id"])
            if extra:
                paper["orgs"] = extra.get("orgs", [])
                if extra.get("thumb"):
                    paper["thumb"], paper["thumb_w"], paper["thumb_h"] = extra["thumb"], extra["w"], extra["h"]
            rows.append(paper)
        payload = {"date": date, "category": args.cat, "demo": True, "papers": rows}
        (out / "days" / f"{date}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        index_days.append({"date": date, "count": len(rows)})
        print(f"{date}: {len(rows)} papers")

    cats = sorted({c for d in days for p in by_day[d] for c in p["categories"]})
    index = {
        "title": "Daily Paper",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "default_category": args.cat,
        "categories": [{"id": args.cat, "name": CATEGORY_NAMES.get(args.cat, (args.cat,))[0],
                        "name_zh": CATEGORY_NAMES.get(args.cat, (args.cat, args.cat))[1]}],
        "category_names": {c: {"en": CATEGORY_NAMES[c][0], "zh": CATEGORY_NAMES[c][1]}
                           for c in cats if c in CATEGORY_NAMES},
        "days": index_days,
        "demo": True,
    }
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
