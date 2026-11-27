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

## Demo data

`scripts/fetch_demo.py` builds the current demo from the arXiv API (metadata) and the Hugging Face Daily Papers API (upvotes). Its relevance score is a keyword rule and the summaries / FAQ in `data/demo_notes.json` and `data/faq/` are hand-written samples; the production pipeline will generate them with an LLM.

```bash
python3 -I scripts/fetch_demo.py --cat cs.RO --days 3
```

Thank you to arXiv for use of its open access interoperability. Paper copyright belongs to the authors; AI readings may contain errors.
