#!/usr/bin/env python3
"""Relevance score, tags and a one-sentence summary for every paper (Claude Haiku 5.5).

Reads data/days/*.json, asks the model about each paper's title + abstract, caches
the answers in data/llm.json (keyed by paper id and prompt version, so re-runs only
pay for new papers) and merges `score`, `tags`, `summary` back into the day files.

The API key is read from ANTHROPIC_API_KEY (a GitHub Actions secret); it is never
written to the repository.

Usage (from the repository root):
    python -I scripts/summarize.py --dry-run         # show the request, no API call
    python -I scripts/summarize.py --limit 5         # first 5 papers only
    python -I scripts/summarize.py                   # all papers without a summary
"""
import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10, 0.50  # USD per million tokens (prompts up to 100K tokens)

SYSTEM = """你在为一个公开的具身智能论文日报处理 arXiv 新论文。每次会给你一篇论文的标题、分类和摘要，请按要求输出 JSON。

1. score：0–10 的整数，表示与下列方向的相关程度。
   核心方向：机器人操作（抓取、灵巧手、双臂、移动操作、装配）；VLA 与机器人基础模型；世界模型 / world action model；模仿学习、强化学习、sim2real；人形与足式机器人的全身控制与运动追踪；触觉等多模态感知用于操作；具身智能的仿真、数据与基准。
   较远方向：无人机、自动驾驶、纯 SLAM/定位、经典控制理论、与学习无关的机构设计。
   9–10：核心方向，且有明确的新方法或重要结果；7–8：核心方向；4–6：与核心方向有交集；1–3：机器人论文，但属于较远方向；0：与机器人无关。
2. tags：1–4 个英文短标签，优先从这些里选：VLA, world model, manipulation, dexterous, grasping, bimanual, mobile manipulation, humanoid, locomotion, sim2real, imitation, RL, diffusion policy, VLM/LLM, tactile, navigation, planning, perception, simulation, dataset, benchmark, aerial, driving, control, HRI。
3. summary_zh：一句中文概括，40–90 字，写清做了什么、怎么做、关键结果。方法名、模型名、数据集名保留英文原文；摘要里有关键数字就带上，并写明对比对象；不要用“本文提出”“该论文”这类开头。
4. summary_en：同一句话的英文版，不超过 40 个词。

只依据给出的标题和摘要，不要推测摘要里没有的数字或结论。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "summary_zh": {"type": "string"},
        "summary_en": {"type": "string"},
    },
    "required": ["score", "tags", "summary_zh", "summary_en"],
    "additionalProperties": False,
}
PROMPT_VERSION = hashlib.sha256(
    (MODEL + SYSTEM + json.dumps(SCHEMA, sort_keys=True)).encode()
).hexdigest()[:12]


def user_text(paper):
    return (f"标题：{paper['title']}\n"
            f"分类：{', '.join(paper.get('categories') or [paper.get('primary', '')])}\n"
            f"摘要：{paper['abstract']}")


def request_params(paper, thinking_off=True):
    params = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": SYSTEM,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": user_text(paper)}],
    }
    if thinking_off:  # a short classification + one sentence needs no thinking
        params["thinking"] = {"type": "disabled"}
    return params


def clean(out):
    """Validate the model's JSON; raises ValueError on anything unusable."""
    score = int(out["score"])
    tags = [str(t).strip() for t in out["tags"] if str(t).strip()][:4]
    zh, en = out["summary_zh"].strip(), out["summary_en"].strip()
    if not zh or not en:
        raise ValueError("empty summary")
    return {"score": max(0, min(10, score)), "tags": tags, "summary": {"zh": zh, "en": en}}


class Summarizer:
    def __init__(self):
        import anthropic  # imported here so --dry-run works without the SDK

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=5)  # SDK retries 429 / 5xx with backoff
        self.thinking_off = True

    def __call__(self, paper):
        try:
            resp = self.client.messages.create(**request_params(paper, self.thinking_off))
        except self.anthropic.BadRequestError as exc:
            if self.thinking_off and "thinking" in str(exc).lower():
                self.thinking_off = False  # model rejects disabled thinking: fall back to adaptive
                resp = self.client.messages.create(**request_params(paper, False))
            else:
                raise
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"refused ({getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("output truncated at max_tokens")
        text = next(b.text for b in resp.content if b.type == "text")
        result = clean(json.loads(text))
        result["usage"] = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
        return result


def load_days(data_dir):
    return {path: json.loads(path.read_text()) for path in sorted((data_dir / "days").glob("*.json"))}


def merge(days, cache):
    for path, day in days.items():
        for paper in day["papers"]:
            entry = cache.get(paper["id"])
            if not entry:
                continue
            paper["score"], paper["tags"], paper["summary"] = entry["score"], entry["tags"], entry["summary"]
            paper["score_source"] = entry["model"]
        path.write_text(json.dumps(day, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--limit", type=int, default=0, help="process at most N papers (0 = all)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="print the first request and a token estimate")
    args = ap.parse_args()

    data_dir = Path(args.data)
    cache_path = data_dir / "llm.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    days = load_days(data_dir)
    papers = {p["id"]: p for day in days.values() for p in day["papers"]}
    todo = [p for pid, p in papers.items() if cache.get(pid, {}).get("v") != PROMPT_VERSION]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} papers to summarize ({len(papers) - len(todo)} cached), prompt {PROMPT_VERSION}")

    if args.dry_run:
        if todo:
            print(json.dumps(request_params(todo[0]), ensure_ascii=False, indent=1))
        est_in = sum(len(SYSTEM) + len(user_text(p)) for p in todo) / 3.2  # rough mixed zh/en chars per token
        est_out = 300 * len(todo)
        print(f"estimate: ~{est_in / 1e3:.0f}k input + ~{est_out / 1e3:.0f}k output tokens, "
              f"~${(est_in * PRICE_IN + est_out * PRICE_OUT) / 1e6:.3f}")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (add it as a repository secret)")
    run = Summarizer()
    used_in = used_out = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, p): p["id"] for p in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            pid = futures[fut]
            try:
                result = fut.result()
            except run.anthropic.AuthenticationError:
                pool.shutdown(cancel_futures=True)
                sys.exit("authentication failed: check the ANTHROPIC_API_KEY secret")
            except Exception as exc:
                failed += 1
                print(f"[{n}/{len(todo)}] {pid} FAILED: {exc}", flush=True)
                continue
            used_in += result["usage"]["in"]
            used_out += result["usage"]["out"]
            cache[pid] = {"v": PROMPT_VERSION, "model": MODEL, **result}
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))
            print(f"[{n}/{len(todo)}] {pid} score={result['score']} {result['summary']['zh'][:40]}", flush=True)

    merge(days, cache)
    cost = (used_in * PRICE_IN + used_out * PRICE_OUT) / 1e6
    print(f"done: {len(todo) - failed} ok, {failed} failed; "
          f"{used_in} input + {used_out} output tokens, ${cost:.4f}")
    if failed and failed == len(todo):
        sys.exit(1)


if __name__ == "__main__":
    main()
