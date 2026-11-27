#!/usr/bin/env python3
"""Tags and a one-sentence zh/en summary for every paper, with Gemini (free API tier).

Reads data/days/*.json, asks the model about each paper's title + abstract, caches the
answers in data/llm.json (keyed by paper id and prompt version, so re-runs only ask
about new papers) and merges `tags` and `summary` back into the day files.

The API key is read from GEMINI_API_KEY (a GitHub Actions secret); it is never written
to the repository or printed.

Usage (from the repository root):
    python -I scripts/summarize_gemini.py --dry-run
    python -I scripts/summarize_gemini.py --compare gemini-3.8-flash,gemini-3.5-flash-lite --limit 10
    python -I scripts/summarize_gemini.py                      # all papers without a summary
"""
import argparse
import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "gemini-3.5-flash"  # chosen 2026-11-27 over 3.5-flash-lite (sharper) and 3.8-flash (no free quota)
FALLBACK = "gemini-3.5-flash-lite"  # used when the main model's free quota is exhausted
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

TAGS = ["VLA", "world model", "manipulation", "dexterous", "grasping", "bimanual", "mobile manipulation",
        "humanoid", "locomotion", "sim2real", "imitation", "RL", "diffusion policy", "VLM/LLM", "tactile",
        "navigation", "planning", "perception", "simulation", "dataset", "benchmark", "aerial", "driving",
        "control", "HRI"]

SYSTEM = f"""你在为一个公开的具身智能论文日报处理 arXiv 新论文。每次会给你一篇论文的标题、分类和摘要，请按要求输出 JSON。

1. tags：1–4 个英文短标签，按与论文核心内容的相关程度排序，优先从这些里选：{", ".join(TAGS)}。只给论文真正在做的方向，摘要里仅顺带提到的不算。
2. summary_zh：一句中文概括，40–90 字，写清做了什么、怎么做、关键结果。方法名、模型名、数据集名保留英文原文；摘要里有关键数字就带上，并写明对比对象；不要用“本文提出”“该论文”“作者”这类开头，直接从方法名或做法写起。
3. summary_en：同一句话的英文版，不超过 40 个词。

只依据给出的标题和摘要，不要推测摘要里没有的数字或结论。"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "summary_zh": {"type": "STRING"},
        "summary_en": {"type": "STRING"},
    },
    "required": ["tags", "summary_zh", "summary_en"],
    "propertyOrdering": ["tags", "summary_zh", "summary_en"],
}


def prompt_version(model):
    return hashlib.sha256((model + SYSTEM + json.dumps(SCHEMA, sort_keys=True)).encode()).hexdigest()[:12]


def user_text(paper):
    return (f"标题：{paper['title']}\n"
            f"分类：{', '.join(paper.get('categories') or [paper.get('primary', '')])}\n"
            f"摘要：{paper['abstract']}")


def body(paper):
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user_text(paper)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "maxOutputTokens": 4096,
        },
    }


class QuotaExhausted(Exception):
    pass


class AuthFailed(Exception):
    pass


def call(model, paper, key, retries=4):
    data = json.dumps(body(paper), ensure_ascii=False).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(API.format(model=model), data=data, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code in (401, 403):
                raise AuthFailed(detail)
            if exc.code == 429:
                if "PerDay" in detail or "per day" in detail.lower():
                    raise QuotaExhausted(detail)
                wait = 20 * (attempt + 1)  # per-minute limit: back off and retry
            elif exc.code in (500, 502, 503, 504):
                wait = 5 * (attempt + 1)
            else:
                raise RuntimeError(f"HTTP {exc.code}: {detail}")
            if attempt == retries:
                if exc.code == 429:
                    raise QuotaExhausted(detail)
                raise RuntimeError(f"HTTP {exc.code}: {detail}")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise RuntimeError(str(exc))
            time.sleep(5 * (attempt + 1))


# Openers the prompt forbids but the model still writes now and then.
BOILERPLATE_ZH = re.compile(r"^(本文|该论文|该研究|作者们?|研究者们?)?(提出了?|介绍了?)(一种|一个)?(名为)?")
BOILERPLATE_EN = re.compile(r"^(this (paper|work|study) )?(proposes|presents|introduces) ", re.I)


def clean(resp):
    """Validate the model's JSON; raises ValueError on anything unusable."""
    cand = (resp.get("candidates") or [{}])[0]
    if cand.get("finishReason") not in (None, "STOP"):
        raise ValueError(f"finishReason {cand.get('finishReason')}")
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    out = json.loads(text)
    allowed = {t.lower(): t for t in TAGS}
    tags = list(dict.fromkeys(allowed[t] for t in (str(x).strip().lower() for x in out["tags"]) if t in allowed))[:4]
    zh = BOILERPLATE_ZH.sub("", out["summary_zh"].strip())
    en = BOILERPLATE_EN.sub("", out["summary_en"].strip())
    en = en[:1].upper() + en[1:]
    if not zh or not en:
        raise ValueError("empty summary")
    usage = resp.get("usageMetadata", {})
    return {"tags": tags, "summary": {"zh": zh, "en": en},
            "usage": {"in": usage.get("promptTokenCount", 0), "out": usage.get("candidatesTokenCount", 0),
                      "thoughts": usage.get("thoughtsTokenCount", 0)}}


def load_days(data_dir):
    return {path: json.loads(path.read_text()) for path in sorted((data_dir / "days").glob("*.json"))}


def merge(days, cache):
    for path, day in days.items():
        for paper in day["papers"]:
            entry = cache.get(paper["id"])
            if not entry:
                continue
            paper["tags"], paper["summary"] = entry["tags"], entry["summary"]
            paper["summary_source"] = entry["model"]
            for gone in ("score", "score_source", "faq"):
                paper.pop(gone, None)
        path.write_text(json.dumps(day, ensure_ascii=False, indent=1))


def sample(papers, n):
    """n papers spread over the newest day, for comparisons."""
    newest = max(p["published"][:10] for p in papers)
    day = [p for p in papers if p["published"][:10] == newest]
    step = max(1, len(day) // n)
    return day[::step][:n]


def compare(models, papers, key, out_path, delay):
    rows = []
    for paper in papers:
        row = {"id": paper["id"], "title": paper["title"], "abstract": paper["abstract"], "results": {}}
        for model in models:
            t0 = time.time()
            try:
                row["results"][model] = clean(call(model, paper, key))
            except Exception as exc:
                row["results"][model] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            row["results"][model]["seconds"] = round(time.time() - t0, 1)
            print(f"{paper['id']} {model}: {row['results'][model].get('summary', {}).get('zh', row['results'][model].get('error'))[:60]}",
                  flush=True)
            time.sleep(delay)
        rows.append(row)
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1))

    md = [f"# Gemini comparison ({', '.join(models)})", ""]
    for i, row in enumerate(rows, 1):
        md += [f"## {i}. {row['title']}", f"arXiv {row['id']}", ""]
        for model in models:
            r = row["results"][model]
            if "error" in r:
                md += [f"**{model}** — ERROR {r['error']}", ""]
                continue
            u = r["usage"]
            md += [f"**{model}** ({r['seconds']}s, {u['in']} in / {u['out']} out / {u['thoughts']} thinking tokens)",
                   f"- tags: {', '.join(r['tags'])}", f"- zh: {r['summary']['zh']}", f"- en: {r['summary']['en']}", ""]
    out_path.with_suffix(".md").write_text("\n".join(md))
    ok = {m: sum("error" not in row["results"][m] for row in rows) for m in models}
    print("ok per model:", ok)
    if not any(ok.values()):
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--fallback", default=FALLBACK)
    ap.add_argument("--limit", type=int, default=0, help="process at most N papers (0 = all)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=4.0, help="seconds between requests (free-tier rate limits)")
    ap.add_argument("--compare", default="", help="comma-separated models: write compare.json/.md, change no data")
    ap.add_argument("--out", default="compare.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data_dir = Path(args.data)
    cache_path = data_dir / "llm.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    days = load_days(data_dir)
    papers = list({p["id"]: p for day in days.values() for p in day["papers"]}.values())

    if args.dry_run:
        print(json.dumps(body(papers[0]), ensure_ascii=False, indent=1))
        return
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is not set (add it as a repository secret)")

    try:
        if args.compare:
            models = [m.strip() for m in args.compare.split(",") if m.strip()]
            compare(models, sample(papers, args.limit or 10), key, Path(args.out), args.delay)
            return

        todo = [p for p in papers if cache.get(p["id"], {}).get("v") not in
                {prompt_version(args.model), prompt_version(args.fallback)}]
        if args.limit:
            todo = todo[: args.limit]
        print(f"{len(todo)} papers to summarize with {args.model} ({len(papers) - len(todo)} cached), "
              f"{args.workers} workers", flush=True)
        state = {"model": args.model, "done": 0, "failed": 0}
        lock = threading.Lock()
        stop = threading.Event()

        def work(paper):
            if stop.is_set():
                return
            model = state["model"]
            try:
                result = clean(call(model, paper, key))
            except QuotaExhausted:
                with lock:
                    if state["model"] == args.fallback or not args.fallback:
                        stop.set()
                        raise
                    if state["model"] == model:
                        print(f"{model}: daily quota exhausted, switching to {args.fallback}", flush=True)
                        state["model"] = args.fallback
                model = state["model"]
                result = clean(call(model, paper, key))
            result.pop("usage", None)
            with lock:
                cache[paper["id"]] = {"v": prompt_version(model), "model": model, **result}
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))
                state["done"] += 1
                print(f"[{state['done']}/{len(todo)}] {paper['id']} {model} {result['summary']['zh'][:36]}",
                      flush=True)
            time.sleep(args.delay)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(work, p): p["id"] for p in todo}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except AuthFailed:
                    stop.set()
                    raise
                except QuotaExhausted:
                    stop.set()
                except Exception as exc:
                    state["failed"] += 1
                    print(f"{futures[fut]} FAILED: {exc}", flush=True)
        if stop.is_set():
            print("daily quota exhausted on every model; the rest waits for the next run", flush=True)
        print(f"{state['done']} summarized, {state['failed']} failed", flush=True)
    except AuthFailed as exc:
        sys.exit(f"authentication failed: check the GEMINI_API_KEY secret ({exc})")
    merge(days, cache)
    print("done")


if __name__ == "__main__":
    main()
