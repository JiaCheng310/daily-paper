# Daily Paper

Daily embodied-AI papers from arXiv, ranked by relevance, with AI-generated one-line summaries and FAQ-style readings.

- Layout after [Cool Papers](https://papers.cool) (list per category and day, `[PDF] [Copy] [FAQ] [REL]`, bottom bar with stars / search / settings); palette after [Embodied AI Daily Papers](https://ipplab.github.io/embodied-ai-daily-papers/); font: [Roboto Mono](https://fonts.google.com/specimen/Roboto+Mono).
- Static site: `index.html` + `assets/` + `data/`, no build step. Deploy with GitHub Pages.

## Preview locally

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

## Data layout

```
data/index.json              # categories, available days, category names
data/days/YYYY-MM-DD.json    # papers of one day
data/faq/<arxiv_id>.json     # FAQ for one paper (loaded on demand)
```

Paper fields: `id, title, authors[], abstract, primary, categories[], published, comment, code_url?, score (0–10), tags[], hf_upvotes?, summary?{zh,en}, faq (bool)`.

FAQ file: `{id, model, generated_at, qa: {zh: [{q, a(markdown)}], en: [...]}}`.

## Preview images and organizations

`scripts/enrich.py` adds a preview image (first well-shaped figure of the arXiv HTML version, PDF fallback) and the authors' organizations to every paper. Organizations are parsed from the author block, matched to [ROR](https://ror.org) (or a small company table) and shown with the organization's site icon (`data/logos/`). It runs in GitHub Actions (`Enrich papers` workflow); caches: `data/enrich.json`, `data/orgs.json`.

## Demo data

`scripts/fetch_demo.py` builds the current demo from the arXiv API (metadata) and the Hugging Face Daily Papers API (upvotes). Its relevance score is a keyword rule and the FAQ in `data/faq/` is a hand-written sample; the production pipeline will generate scores, one-line summaries and FAQs with an LLM.

```bash
python3 -I scripts/fetch_demo.py --cat cs.RO --days 3
```

Thank you to arXiv for use of its open access interoperability. Paper copyright belongs to the authors; AI readings may contain errors.
