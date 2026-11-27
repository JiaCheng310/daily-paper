#!/usr/bin/env python3
"""Add preview images and author organizations to the day files.

For each paper in data/days/*.json:
  1. arXiv HTML (arxiv.org/html/<id>): organizations from the LaTeXML author
     block, preview image from the first suitably shaped figure.
  2. Fallback when there is no HTML version: the PDF's first page text for
     organizations, the largest embedded raster image on pages 1-3 (or a crop
     of page 1) for the preview.

Results are cached in data/enrich.json, thumbnails go to data/thumbs/<id>.jpg,
and the fields `orgs`, `thumb`, `thumb_w`, `thumb_h` are merged into the day
files. Requires Pillow and PyMuPDF (scripts/requirements.txt).

Usage (from the repository root):
    python -I scripts/enrich.py [--limit N] [--delay 1.0]
"""
import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

UA = "daily-paper-demo/0.1 (+https://github.com/JiaCheng310/daily-paper)"
THUMB_MAX = (640, 440)
MAX_ASPECT = 3.2  # wider figures (thin strips) make poor previews

ORG_KW = re.compile(
    r"Universit|Institut|Laborator|\bLabs?\b|College|Academy|Academia|\bInc\b|Corporation|\bCorp\b|"
    r"Company|\bLtd\b|GmbH|Research|Robotics|Technolog|Centre|Center|Hospital|Polytechn|\bTeam\b|"
    r"DeepMind|NVIDIA|Google|\bMeta\b|Microsoft|Amazon|Apple|Tesla|Toyota|Samsung|Huawei|Alibaba|"
    r"Tencent|ByteDance|Baidu|OpenAI|Anthropic|Physical Intelligence|Unitree|AgiBot|Galbot|"
    r"\bETH\b|EPFL|KAIST|\bMIT\b|\bCMU\b|\bUCLA\b|\bNUS\b|\bNTU\b|\bHKU\b|HKUST|\bKTH\b|\bTUM\b",
    re.I,
)
TOP_LEVEL = re.compile(
    r"Universit|Institute of Technology|Polytechn|\bETH\b|EPFL|KAIST|DeepMind|NVIDIA|Google|\bMeta\b|"
    r"Microsoft|Amazon|Apple|Tesla|Toyota|Samsung|Huawei|Alibaba|Tencent|ByteDance|Baidu|OpenAI|"
    r"Anthropic|\bInc\b|Corporation|\bLtd\b|GmbH|Company|Academy of Sciences|Shanghai AI Lab",
    re.I,
)
SUBUNIT = re.compile(
    r"^(Dep(artmen)?t\b|Department|School\b|Faculty|College of|Division|Graduate School|Key Lab|"
    r"State Key|Lab(oratory)? (of|for)|Center for|Centre for|Institute for|Institute of (?!Technology))",
    re.I,
)
EMBEDDED = re.compile(
    r"((?:[A-Z][\w'’.&-]*\s+){0,4}(?:University|Universit[éàaäi]\w*|Institute of Technology|Polytechnic\w*)"
    r"(?:\s+(?:of|de|di|del|degli|für|for)\s+(?:the\s+)?(?:[A-Z][\w'’.&-]*(?:\s+|$)){1,4})?)"
)
COMPANY_SUFFIX = re.compile(r"(Co\.?,?\s*)?(Ltd|Inc|LLC|GmbH|Corp|S\.?A)\.?", re.I)
NOT_ORG = re.compile(r"@|https?://|www\.|\b\d{5,6}\b|^\W*$")
CANONICAL = [
    (r"Massachusetts Institute of Technology|^MIT$", "MIT"),
    (r"Carnegie Mellon", "Carnegie Mellon University"),
    (r"Shanghai (Artificial Intelligence|AI) Lab", "Shanghai AI Lab"),
    (r"Google DeepMind|^DeepMind", "Google DeepMind"),
    (r"NVIDIA", "NVIDIA"),
    (r"Horizon Robotics", "Horizon Robotics"),
    (r"Swiss Federal Institute of Technology.*Zurich|ETH Z(u|ü)rich|^ETH$", "ETH Zurich"),
    (r"National University of Singapore|^NUS$", "NUS"),
    (r"Nanyang Technological University|^NTU$", "NTU"),
    (r"Hong Kong University of Science and Technology|^HKUST", "HKUST"),
    (r"^The University of Hong Kong|^University of Hong Kong|^HKU$", "HKU"),
    (r"Korea Advanced Institute of Science and Technology|^KAIST$", "KAIST"),
    (r"Polytechnique F(é|e)d(é|e)rale de Lausanne|^EPFL$", "EPFL"),
    (r"^Chinese University of Hong Kong|^CUHK", "CUHK"),
    (r"^University of Pennsylvania|^UPenn$", "University of Pennsylvania"),
    (r"^Stanford", "Stanford University"),
    (r"^Tsinghua", "Tsinghua University"),
    (r"^Peking University", "Peking University"),
    (r"Shanghai Jiao ?Tong", "Shanghai Jiao Tong University"),
    (r"Technical University of Munich|Technische Universit(ä|a)t M(ü|u)nchen|^TUM$", "TU Munich"),
    (r"Chinese Academy of Sciences", "Chinese Academy of Sciences"),
]
CANONICAL = [(re.compile(p, re.I), name) for p, name in CANONICAL]


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.geturl()


# ---------------------------------------------------------------- HTML parsing
class LatexmlParser(HTMLParser):
    """Collects affiliation strings, author-block text and figure images."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []  # (tag, classes)
        self.affils, self.images = [], []
        self.authors_text = []
        self._affil_depth = None
        self._affil_buf = []
        self._skip_depth = None

    def _classes(self, attrs):
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag, attrs):
        cls = self._classes(attrs)
        if tag in ("img", "br", "hr", "meta", "link", "input", "source"):
            self.handle_startendtag(tag, attrs)
            return
        self.stack.append((tag, cls))
        depth = len(self.stack)
        if self._affil_depth is None and ("ltx_role_affiliation" in cls or "ltx_role_address" in cls):
            self._affil_depth, self._affil_buf = depth, []
        if ("ltx_contact_name" in cls or "ltx_ERROR" in cls) and self._skip_depth is None:
            self._skip_depth = depth

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            if self._in("ltx_authors"):
                self.authors_text.append("\n")
            if self._affil_depth is not None:
                self._affil_buf.append("; ")
            return
        if tag != "img" or len(self.images) >= 10:
            return
        a = dict(attrs)
        if "ltx_graphics" not in (a.get("class") or ""):
            return
        try:
            w, h = int(a.get("width") or 0), int(a.get("height") or 0)
        except ValueError:
            w, h = 0, 0
        self.images.append({"src": a.get("src", ""), "w": w, "h": h, "figure": self._in("ltx_figure")})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                depth = i + 1
                if self._affil_depth is not None and depth <= self._affil_depth:
                    text = re.sub(r"\s+", " ", "".join(self._affil_buf)).strip()
                    if text:
                        self.affils.append(text)
                    self._affil_depth = None
                if self._skip_depth is not None and depth <= self._skip_depth:
                    self._skip_depth = None
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._skip_depth is not None:
            return
        if self._affil_depth is not None:
            self._affil_buf.append(data)
        if self._in("ltx_authors"):
            self.authors_text.append(data)

    def _in(self, cls):
        return any(cls in c for _, c in self.stack)


# ------------------------------------------------------------ org extraction
def clean_affil(text):
    text = re.sub(r"^\s*Affiliations?\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"\\[A-Za-z]+(?=\s|$)", " ", text)  # LaTeX macros LaTeXML could not expand
    text = re.sub(r"[†‡§¶∗*#♯♮♭⋆★☆✉]", " ", text)
    text = re.sub(r"\S+@\S+|https?://\S+", " ", text)
    m = re.search(r"\b(?:are|is) (?:with|at|from) (?:the )?(.*)", text, re.I)
    if m:
        text = m.group(1)
    return re.sub(r"\s+", " ", text).strip(" .;,")


def pick_org(affil):
    segs = [s.strip(" .") for s in affil.split(",")]
    segs = [re.sub(r"^\(?\d+\)?\s*|^[a-z]\s+(?=[A-Z])|^(and|&)\s+(the\s+)?", "", s).strip() for s in segs]
    # "..., Ltd. Foshan University": a company suffix glued to the next organization
    segs = [re.sub(r"^(Co\.,?\s*)?(Ltd|Inc|LLC|GmbH)\.?\s+(?=[A-Z])", "", s) for s in segs]
    merged = []  # "X Technology Co., Ltd." splits into "X Technology Co" + "Ltd"
    for s in segs:
        if merged and COMPANY_SUFFIX.fullmatch(s):
            merged[-1] = f"{merged[-1]}, {s}"
        else:
            merged.append(s)
    segs = merged
    # "University of California, Berkeley" -> "UC Berkeley"
    for i, s in enumerate(segs[:-1]):
        if re.fullmatch(r"(The )?University of California", s, re.I) and segs[i + 1]:
            return "UC " + segs[i + 1]
    cands = [s for s in segs if s and ORG_KW.search(s) and not NOT_ORG.search(s) and len(s) <= 120]
    if not cands:
        return None
    top = [s for s in cands if TOP_LEVEL.search(s) and not SUBUNIT.search(s)]
    plain = [s for s in cands if not SUBUNIT.search(s)]
    choice = top[0] if top else (plain or cands)[-1]
    # "Department of X (DEI) at the University of Padova" -> "University of Padova"
    if SUBUNIT.search(choice) or len(choice) > 48 or " at " in choice:
        m = EMBEDDED.search(choice)
        if m:
            choice = m.group(1).strip()
    return choice


def canonical(org):
    org = re.sub(r"^The\s+", "", org).strip()
    org = re.sub(r",?\s*(Co\.?,?\s*)?(Ltd|Inc|LLC|GmbH|Corp)\.?$", "", org).strip(" ,.")
    for rx, name in CANONICAL:
        if rx.search(org):
            return name
    return org


def orgs_from_affils(affils):
    out = []
    for raw in affils:
        for part in re.split(r";|\n|\s{2,}", clean_affil(raw)):
            part = part.strip()
            if not part:
                continue
            org = pick_org(part)
            if org:
                org = canonical(org)
                if org.lower() not in {o.lower() for o in out}:
                    out.append(org)
    return out


def orgs_from_text(text):
    """Fallback for free text (author block without tags, PDF first page)."""
    lines = [ln.strip() for ln in re.split(r"\n|;", text)]
    return orgs_from_affils([ln for ln in lines if ln and ORG_KW.search(ln) and len(ln) <= 600])


def compute_orgs(kind, affils):
    return orgs_from_affils(affils) if kind == "tagged" else orgs_from_text("\n".join(affils))


# ----------------------------------------------------------------- previews
def to_thumb(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail(THUMB_MAX, Image.LANCZOS)
    return img


def choose_html_image(images):
    figs = [im for im in images if im["figure"] and not im["src"].lower().endswith(".svg")] or \
        [im for im in images if not im["src"].lower().endswith(".svg")]
    for im in figs[:5]:
        if im["w"] and im["h"] and im["w"] / im["h"] <= MAX_ASPECT and im["w"] >= 120:
            return im
    return figs[0] if figs else None


def from_html(pid):
    try:
        html, final_url = fetch(f"https://arxiv.org/html/{pid}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    parser = LatexmlParser()
    parser.feed(html.decode("utf-8", "replace"))
    affils, kind = list(dict.fromkeys(parser.affils)), "tagged"
    orgs = compute_orgs(kind, affils)
    if not orgs:
        affils, kind = ["".join(parser.authors_text)], "text"
        orgs = compute_orgs(kind, affils)
    image = choose_html_image(parser.images)
    thumb = None
    if image:
        # e.g. https://arxiv.org/html/2611.34936 + "2611.34936v1/x1.png"
        url = urllib.parse.urljoin(final_url, image["src"])
        time.sleep(0.5)
        try:
            data, _ = fetch(url)
            thumb = to_thumb(data)
        except Exception as exc:
            print(f"  image failed {url}: {exc}", file=sys.stderr)
    return {"orgs": orgs, "affils": affils, "kind": kind, "thumb_img": thumb, "source": "html"}


def from_pdf(pid):
    import pymupdf  # imported lazily: only needed for papers without HTML

    data, _ = fetch(f"https://arxiv.org/pdf/{pid}", timeout=120)
    doc = pymupdf.open(stream=data, filetype="pdf")
    first = doc[0].get_text("text")
    head = re.split(r"\n\s*Abstract\b|\nABSTRACT\b", first, maxsplit=1)[0]
    orgs = compute_orgs("text", [head])
    best, best_area = None, 0
    for pno in range(min(3, len(doc))):
        for info in doc[pno].get_images(full=True):
            try:
                pix = pymupdf.Pixmap(doc, info[0])
                if pix.n - pix.alpha >= 4:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                w, h = pix.width, pix.height
                if w >= 300 and h >= 150 and w / h <= MAX_ASPECT and w * h > best_area:
                    best, best_area = pix.tobytes("png"), w * h
            except Exception:
                continue
    if best is None:  # vector figures only: use the top of page 1
        page = doc[0]
        rect = page.rect
        clip = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + rect.height * 0.55)
        best = page.get_pixmap(dpi=110, clip=clip).tobytes("png")
    return {"orgs": orgs, "affils": [head], "kind": "text", "thumb_img": to_thumb(best), "source": "pdf"}


# --------------------------------------------------------------------- main
def merge(data_dir, cache):
    for path in sorted((data_dir / "days").glob("*.json")):
        day = json.loads(path.read_text())
        for paper in day["papers"]:
            info = cache.get(paper["id"])
            if not info:
                continue
            paper["orgs"] = info.get("orgs", [])
            if info.get("thumb"):
                paper["thumb"], paper["thumb_w"], paper["thumb_h"] = info["thumb"], info["w"], info["h"]
            else:
                for key in ("thumb", "thumb_w", "thumb_h"):
                    paper.pop(key, None)
        path.write_text(json.dumps(day, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--limit", type=int, default=0, help="process at most N new papers (0 = all)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between papers")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--renorm", action="store_true", help="recompute orgs from cached affiliations, no network")
    args = ap.parse_args()
    data_dir = Path(args.data)
    thumbs = data_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / "enrich.json"
    cache = {} if args.refresh or not cache_path.exists() else json.loads(cache_path.read_text())

    if args.renorm:
        for entry in cache.values():
            if "affils" in entry:
                entry["orgs"] = compute_orgs(entry.get("kind", "tagged"), entry["affils"])
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
        merge(data_dir, cache)
        print("renormalized and merged", flush=True)
        return

    ids = []
    for path in sorted((data_dir / "days").glob("*.json"), reverse=True):
        ids += [p["id"] for p in json.loads(path.read_text())["papers"]]
    todo = [pid for pid in dict.fromkeys(ids) if pid not in cache]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} papers to enrich ({len(cache)} cached)", flush=True)

    for n, pid in enumerate(todo, 1):
        try:
            info = from_html(pid)
            if info is None or info["thumb_img"] is None or not info["orgs"]:
                time.sleep(args.delay)
                pdf = from_pdf(pid)
                if info is None:
                    info = pdf
                else:
                    if not info["orgs"]:
                        info.update(orgs=pdf["orgs"], affils=pdf["affils"], kind=pdf["kind"])
                    if info["thumb_img"] is None:
                        info["thumb_img"] = pdf["thumb_img"]
                    info["source"] = "html+pdf"
        except Exception as exc:
            print(f"[{n}/{len(todo)}] {pid} FAILED: {exc}", flush=True)
            time.sleep(args.delay)
            continue
        entry = {"orgs": info["orgs"], "affils": info["affils"], "kind": info["kind"], "source": info["source"]}
        if info["thumb_img"] is not None:
            img = info["thumb_img"]
            img.save(thumbs / f"{pid}.jpg", "JPEG", quality=76, optimize=True, progressive=True)
            entry.update(thumb=f"data/thumbs/{pid}.jpg", w=img.width, h=img.height)
        cache[pid] = entry
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
        print(f"[{n}/{len(todo)}] {pid} {info['source']} orgs={entry['orgs'][:4]}", flush=True)
        time.sleep(args.delay)

    merge(data_dir, cache)
    print("merged into day files", flush=True)


if __name__ == "__main__":
    main()
