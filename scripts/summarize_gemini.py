#!/usr/bin/env python3
"""Tags, a one-sentence zh/en summary and the organizations for every paper, with DeepSeek
(deepseek-flash, thinking off) and Gemini's free tier as the fallback.

Reads data/days/*.json, asks the model about each paper's title + abstract, caches the
answers in data/llm.json (keyed by paper id and prompt version, so re-runs only ask
about new papers) and merges `tags` and `summary` back into the day files.

API keys come from DEEPSEEK_API_KEY and GEMINI_API_KEY (GitHub Actions secrets); they are
never written to the repository or printed. Either one is enough.

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

# 2026-11-27: DeepSeek (about $0.5 a month off-peak, no daily cap) replaced Gemini's free
# tier, whose daily quota ran out; Gemini stays as the fallback.
MODEL = "deepseek-flash"
FALLBACK = "gemini-3.5-flash-lite"
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY", "gemini": "GEMINI_API_KEY"}


def provider(model):
    return "deepseek" if model.startswith("deepseek") else "gemini"


def key_for(model):
    return os.environ.get(KEY_ENV[provider(model)]) if model else None

TAGS = ["LLM", "VLM", "multimodal", "agents", "reasoning", "RL", "alignment", "efficiency", "pretraining",
        "code", "math", "retrieval", "speech", "image generation", "video generation", "diffusion", "3D",
        "detection", "segmentation", "world model", "robotics", "VLA", "manipulation", "humanoid", "driving",
        "dataset", "benchmark", "theory", "interpretability", "safety"]

SYSTEM = f"""你在为一个公开的 AI 论文日报（Hugging Face 与 alphaXiv 上的热门论文）处理 arXiv 新论文。每次会给你一篇论文的标题、分类和摘要，请按要求输出 JSON。

1. tags：1–4 个英文短标签，按与论文核心内容的相关程度排序，优先从这些里选：{", ".join(TAGS)}。只给论文真正在做的方向，摘要里仅顺带提到的不算。
2. summary_zh：一句中文概括，40–90 字，写清做了什么、怎么做、关键结果。方法名、模型名、数据集名保留英文原文；摘要里有关键数字就带上，并写明对比对象；不要用“本文提出”“该论文”“作者”这类开头，直接从方法名或做法写起。
3. summary_en：同一句话的英文版，不超过 40 个词。
4. orgs：作者所属机构，按第一次出现的顺序，最多 8 个，只依据给出的"作者与单位"原文；没有这段原文就给空数组。
   - name：顶层机构的常用英文名，例如 "Tsinghua University"、"XPENG"、"Hong Kong Polytechnic University"；院系、实验室、团队归到所属的大学或公司（"GRASP Laboratory, University of Pennsylvania" → "University of Pennsylvania"，"Robotics Foundation Model Team, XPENG" → "XPENG"）；同名机构要写清楚，例如 "Eastern Institute of Technology, Ningbo"。
   - country：机构所在国家或地区的英文名，例如 "China"、"Hong Kong"、"United States"、"United Kingdom"、"South Korea"；看不出来就给空字符串。

只依据给出的内容，不要推测摘要里没有的数字或结论，不要编造原文里没有的机构。

输出一个 JSON 对象，格式如下：
{{"tags": ["LLM", "reasoning"], "summary_zh": "……", "summary_en": "……", "orgs": [{{"name": "Tsinghua University", "country": "China"}}]}}"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "summary_zh": {"type": "STRING"},
        "summary_en": {"type": "STRING"},
        "orgs": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"name": {"type": "STRING"}, "country": {"type": "STRING"}},
            "required": ["name", "country"],
            "propertyOrdering": ["name", "country"]}},
    },
    "required": ["tags", "summary_zh", "summary_en", "orgs"],
    "propertyOrdering": ["tags", "summary_zh", "summary_en", "orgs"],
}


def prompt_version(model):
    return hashlib.sha256((model + SYSTEM + json.dumps(SCHEMA, sort_keys=True)).encode()).hexdigest()[:12]


AFFIL_CHARS = 1500  # the author block; PDF fallbacks carry the whole first page, cut it short
AFFILS = {}  # paper id -> raw author/affiliation text from data/enrich.json, filled in main()


def user_text(paper):
    text = (f"标题：{paper['title']}\n"
            f"分类：{', '.join(paper.get('categories') or [paper.get('primary', '')])}\n"
            f"摘要：{paper['abstract']}")
    affils = AFFILS.get(paper["id"])
    if affils:
        text += f"\n作者与单位（从论文页面抽取的原文，可能夹杂别的文字）：\n{affils[:AFFIL_CHARS]}"
    return text


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


def retry_delay(detail):
    """Seconds Google asks us to wait (RetryInfo.retryDelay, e.g. "34s"), or None."""
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', detail)
    return float(m.group(1)) if m else None


def deepseek_body(model, paper):
    return {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_text(paper)}],
        "thinking": {"type": "disabled"},  # a one-line summary needs no reasoning; thinking is billed
        "response_format": {"type": "json_object"},
        "max_tokens": 2048,
        "stream": False,
    }


def call(model, paper, key, retries=8):
    """One request -> {"text", "finish", "usage"}. Per-minute 429s wait as long as asked and
    retry; an exhausted daily quota or balance raises QuotaExhausted at once."""
    if provider(model) == "deepseek":
        resp = post(DEEPSEEK_API, deepseek_body(model, paper),
                    {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, retries)
        choice = (resp.get("choices") or [{}])[0]
        usage = resp.get("usage") or {}
        return {"text": (choice.get("message") or {}).get("content") or "",
                "finish": "STOP" if choice.get("finish_reason") == "stop" else choice.get("finish_reason"),
                "usage": {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0),
                          "cached": usage.get("prompt_cache_hit_tokens", 0)}}
    resp = post(API.format(model=model), body(paper), {"Content-Type": "application/json", "x-goog-api-key": key}, retries)
    cand = (resp.get("candidates") or [{}])[0]
    usage = resp.get("usageMetadata", {})
    return {"text": "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought")),
            "finish": cand.get("finishReason"),
            "usage": {"in": usage.get("promptTokenCount", 0), "out": usage.get("candidatesTokenCount", 0),
                      "thoughts": usage.get("thoughtsTokenCount", 0)}}


def post(url, payload, headers, retries):
    data = json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            short = re.sub(r"\s+", " ", detail)[:300]
            if exc.code in (401, 403):
                raise AuthFailed(short)
            if exc.code == 402:  # DeepSeek: balance used up
                raise QuotaExhausted(short)
            if exc.code == 429:
                if "PerDay" in detail or "per day" in detail.lower():
                    raise QuotaExhausted(short)
                wait = (retry_delay(detail) or 10 * (attempt + 1)) + 1
            elif exc.code in (500, 502, 503, 504):
                wait = 3 * (attempt + 1)
            else:
                raise RuntimeError(f"HTTP {exc.code}: {short}")
            if attempt == retries:
                raise RuntimeError(f"HTTP {exc.code} after {retries} retries: {short}")
            time.sleep(min(wait, 90))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise RuntimeError(str(exc))
            time.sleep(3 * (attempt + 1))


# Openers the prompt forbids but the model still writes now and then.
BOILERPLATE_ZH = re.compile(r"^(本文|该论文|该研究|作者们?|研究者们?)?(提出了?|介绍了?)(一种|一个)?(名为)?")
BOILERPLATE_EN = re.compile(r"^(this (paper|work|study) )?(proposes|presents|introduces) ", re.I)


def clean(resp):
    """Validate the model's JSON; raises ValueError on anything unusable."""
    if resp["finish"] not in (None, "STOP"):
        raise ValueError(f"finish reason {resp['finish']}")
    out = json.loads(resp["text"])
    allowed = {t.lower(): t for t in TAGS}
    tags = list(dict.fromkeys(allowed[t] for t in (str(x).strip().lower() for x in out["tags"]) if t in allowed))[:4]
    zh = BOILERPLATE_ZH.sub("", out["summary_zh"].strip())
    en = BOILERPLATE_EN.sub("", out["summary_en"].strip())
    en = en[:1].upper() + en[1:]
    if not zh or not en:
        raise ValueError("empty summary")
    orgs, seen = [], set()
    for org in out.get("orgs") or []:
        name = re.sub(r"\s+", " ", str(org.get("name", ""))).strip(" .,;")
        if name and len(name) <= 120 and name.lower() not in seen:
            seen.add(name.lower())
            orgs.append({"name": name, "country": str(org.get("country", "")).strip()})
    return {"tags": tags, "summary": {"zh": zh, "en": en}, "orgs": orgs[:8], "usage": resp["usage"]}


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
    # Organizations from the model are resolved (ROR names, logos) by `enrich.py --merge`.
    refresh_popular(days)


def refresh_popular(days):
    """popular.json keeps copies of papers; replace them with the current day-file versions."""
    path = next(iter(days)).parent.parent / "popular.json"
    if not path.exists():
        return
    current = {p["id"]: p for day in days.values() for p in day["papers"]}
    popular = json.loads(path.read_text())
    for period in popular.get("periods", {}).values():
        period["papers"] = [current.get(p["id"], p) for p in period["papers"]]
    path.write_text(json.dumps(popular, ensure_ascii=False, indent=1))


def sample(papers, n):
    """n papers spread over the newest day, for comparisons."""
    newest = max(p["published"][:10] for p in papers)
    day = [p for p in papers if p["published"][:10] == newest]
    step = max(1, len(day) // n)
    return day[::step][:n]


def compare(models, papers, out_path, delay):
    rows = []
    for paper in papers:
        row = {"id": paper["id"], "title": paper["title"], "abstract": paper["abstract"], "results": {}}
        for model in models:
            t0 = time.time()
            try:
                row["results"][model] = clean(call(model, paper, key_for(model)))
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
            md += [f"**{model}** ({r['seconds']}s, {u['in']} in / {u['out']} out / {u.get('thoughts', 0)} thinking tokens)",
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
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--top", type=int, default=30, help="per day, only the N most popular papers (0 = all)")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between requests per worker (429s are handled anyway)")
    ap.add_argument("--compare", default="", help="comma-separated models: write compare.json/.md, change no data")
    ap.add_argument("--out", default="compare.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data_dir = Path(args.data)
    cache_path = data_dir / "llm.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    days = load_days(data_dir)
    enrich_path = data_dir / "enrich.json"
    if enrich_path.exists():
        for pid, entry in json.loads(enrich_path.read_text()).items():
            if entry.get("affils"):
                AFFILS[pid] = "\n".join(entry["affils"]).strip()
    papers = list({p["id"]: p for day in days.values() for p in day["papers"]}.values())

    if args.dry_run:
        print(json.dumps(deepseek_body(args.model, papers[0]) if provider(args.model) == "deepseek"
                         else body(papers[0]), ensure_ascii=False, indent=1))
        return
    # A model without its key is skipped: the fallback becomes the main model.
    if not key_for(args.model):
        print(f"{KEY_ENV[provider(args.model)]} is not set; using {args.fallback or 'nothing'}", flush=True)
        args.model, args.fallback = args.fallback, ""
    if args.fallback and not key_for(args.fallback):
        args.fallback = ""
    if not args.model or not key_for(args.model):
        sys.exit("no API key: set DEEPSEEK_API_KEY or GEMINI_API_KEY as a repository secret")

    try:
        if args.compare:
            models = [m.strip() for m in args.compare.split(",") if m.strip()]
            compare(models, sample(papers, args.limit or 10), Path(args.out), args.delay)
            return

        if args.top:  # only each day's most popular papers (heat from popularity.py)
            wanted = set()
            for day in days.values():
                ranked = sorted(enumerate(day["papers"]), key=lambda x: (-(x[1].get("heat") or 0), x[0]))
                wanted.update(p["id"] for _, p in ranked[: args.top])
            papers = [p for p in papers if p["id"] in wanted]
        todo = [p for p in papers if cache.get(p["id"], {}).get("v") not in
                {prompt_version(args.model), prompt_version(args.fallback)}]
        # Newest days first: when the daily free quota runs out, today's list is done.
        todo.sort(key=lambda p: (p.get("published", "")[:10], p.get("heat") or 0), reverse=True)
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
                result = clean(call(model, paper, key_for(model)))
            except QuotaExhausted:
                with lock:
                    if state["model"] == args.fallback or not args.fallback:
                        stop.set()
                        raise
                    if state["model"] == model:
                        print(f"{model}: quota or balance exhausted, switching to {args.fallback}", flush=True)
                        state["model"] = args.fallback
                model = state["model"]
                result = clean(call(model, paper, key_for(model)))
            usage = result.pop("usage", {})
            with lock:
                for k in ("in", "out", "cached"):
                    state[k] = state.get(k, 0) + (usage.get(k) or 0)
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
        print(f"tokens: {state.get('in', 0)} in ({state.get('cached', 0)} cached), {state.get('out', 0)} out", flush=True)
    except AuthFailed as exc:
        sys.exit(f"authentication failed: check the API key secrets ({exc})")
    merge(days, cache)
    print("done")


if __name__ == "__main__":
    main()
