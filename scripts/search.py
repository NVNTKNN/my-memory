#!/usr/bin/env python3
"""
Called by Claude Code UserPromptSubmit hook.
Reads JSON from stdin: {"prompt": "..."} or plain text fallback.
Prints the top-5 relevant memory pages (each trimmed to its current-state
head) wrapped in <memory_context>, kept under Claude Code's ~10k-char hook
additionalContext inline cap so the whole blob injects instead of spilling
to a tool-results file with only a ~2KB preview.

Retrieval is HYBRID: dense semantic similarity (multilingual MiniLM via ChromaDB)
fused with lexical BM25 over the same corpus using Reciprocal Rank Fusion.
Dense alone misses exact-token queries (error strings, file names,
invoice numbers, product codes) because MiniLM only embeds each page's
top ~128 tokens and scores on fuzzy meaning; BM25 catches the literal matches.
Per Anthropic's Contextual Retrieval, adding a lexical signal to dense
embeddings cuts retrieval misses materially (~35% -> ~49% fewer).

The BM25 augmentation is wrapped in a try/except: any failure falls back to
pure-dense ranking, so this session-start hook never hard-fails.
"""

import sys
import json
import hashlib
import os
import re
import math
import time
import unicodedata
from datetime import date

# The embedding model is cached locally; force offline so model load never
# fires a network HEAD to huggingface.co. huggingface_hub does that freshness
# check on every load even for cached models, and when HF is slow/unreachable
# it retries for >30s and silently blows this hook's timeout budget, which
# drops the memory injection for that prompt.
# setdefault so an explicit HF_HUB_OFFLINE=0 can still opt back in.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import chromadb
from sentence_transformers import SentenceTransformer

CHROMA_DIR = os.path.expanduser("~/.claude/memory_chroma")
COLLECTION = "memory_wiki_ml"  # must match build.py
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
TOP_K = 5          # files injected into context
CANDIDATE_K = 40   # dense candidate pool size before fusion (pages + section chunks)
RRF_K = 60         # Reciprocal Rank Fusion constant (standard default)
INJECT_POOL = 10   # only this many top-ranked pages are injection candidates —
                   # session dedup backfills from here, never from the tail
STATE_DIR = os.path.expanduser("~/.claude/memory_index/session_state")
STATE_MAX_AGE_DAYS = 7

# Claude Code injects a UserPromptSubmit hook's additionalContext inline only
# while it stays under ~10,000 chars. Past that, CC spills the full text to a
# tool-results file and injects just a ~2KB preview — silently dropping most of
# the retrieved memory. Keep the whole
# <memory_context> blob under this ceiling and trim each page to its current-
# state head; full pages are re-read on demand.
MAX_CONTEXT_CHARS = 8500   # hard ceiling for the whole <memory_context> blob
CRIT_PAGE_CHARS = 2100     # priority:critical pages kept ~whole (<=2KB by design)
PER_PAGE_CHARS = 1400      # ordinary pages trimmed to frontmatter + current-state head

RECENCY_HALF_LIFE_DAYS = 90   # freshness halves every ~quarter
RECENCY_FLOOR = 0.85          # max recency penalty: 15% of fused score
LINK_WEIGHT = 0.5             # RRF weight of the wikilink-neighbor ranking
LINK_TOP = 3                  # expand neighbors of this many top fused hits

# ASCII words, or runs of Japanese kana / CJK characters (split into pairs below).
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CRITICAL_RE = re.compile(r"^priority:\s*critical\b", re.MULTILINE)
_UPDATED_RE = re.compile(r"^\s*updated:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")


def is_critical(doc: str) -> bool:
    """True if the page's frontmatter marks it priority: critical."""
    return bool(_CRITICAL_RE.search(doc[:600]))


def get_payload() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"prompt": raw}  # plain text fallback


def get_prompt() -> str:
    data = get_payload()
    return data.get("prompt", "") or data.get("user_prompt", "")


def load_session_state(session_id: str) -> dict:
    """Pages already injected this session: {page_id: content_hash}."""
    try:
        with open(os.path.join(STATE_DIR, f"{session_id}.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_session_state(session_id: str, state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, f"{session_id}.json"), "w") as f:
            json.dump(state, f)
        _prune_stale_state()
    except OSError:
        pass  # dedup state is an optimisation — never fail the hook over it


def _prune_stale_state() -> None:
    cutoff = time.time() - STATE_MAX_AGE_DAYS * 86400
    for name in os.listdir(STATE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(STATE_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue


def tokenize(text: str) -> list:
    """Lowercase alphanumeric/underscore tokens. Keeps identifiers like
    `invoice_2026` and `sku_100g` intact for lexical matching.
    Japanese has no spaces between words, so a kana/CJK run becomes its
    overlapping two-character pieces (信用状 -> 信用, 用状). NFKC first turns
    full-width letters and half-width katakana into their usual forms."""
    out = []
    for tok in _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).lower()):
        if tok.isascii() or len(tok) == 1:
            out.append(tok)
        else:
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
    return out


def bm25_scores(corpus_tokens: list, query_tokens: list, k1: float = 1.5, b: float = 0.75) -> list:
    """Pure-Python BM25 Okapi. Returns one score per document, aligned with
    corpus_tokens order. No external dependency (corpus is small, ~hundreds)."""
    n = len(corpus_tokens)
    if n == 0:
        return []
    doc_len = [len(d) for d in corpus_tokens]
    avgdl = (sum(doc_len) / n) if n else 0.0

    df = {}
    for toks in corpus_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + (n - dfi + 0.5) / (dfi + 0.5)) for t, dfi in df.items()}

    q = [t for t in set(query_tokens) if t in idf]
    scores = []
    for i, toks in enumerate(corpus_tokens):
        if not q:
            scores.append(0.0)
            continue
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        norm = (1 - b + b * (doc_len[i] / avgdl)) if avgdl else 1.0
        for t in q:
            f = tf.get(t, 0)
            if f == 0:
                continue
            s += idf[t] * (f * (k1 + 1)) / (f + k1 * norm)
        scores.append(s)
    return scores


def reciprocal_rank_fusion(rankings: list, k: int = RRF_K, weights: list = None) -> list:
    """Fuse N ordered id-lists into one. Each list ranks ids best-first.
    Returns [(id, score), ...] sorted best-first. RRF is rank-based, so the
    incomparable score scales of cosine vs BM25 never need normalising.
    Optional per-list weights let auxiliary signals (e.g. wikilink neighbors)
    contribute without outvoting the primary rankings."""
    fused = {}
    for li, ranking in enumerate(rankings):
        w = weights[li] if weights else 1.0
        for rank, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + w / (k + rank + 1)
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)


def extract_updated_date(doc: str):
    """The updated: date from a page's frontmatter (flat or legacy nested),
    or None."""
    m = _UPDATED_RE.search(doc[:600])
    return m.group(1) if m else None


def freshness(updated: str, today=None) -> float:
    """Exponential freshness in (0, 1]: 1.0 = updated today, halving every
    RECENCY_HALF_LIFE_DAYS. Undated / unparseable pages are neutral (0.5),
    not punished."""
    if not updated:
        return 0.5
    try:
        age_days = (date.fromisoformat(str(today) if today else date.today().isoformat())
                    - date.fromisoformat(updated)).days
    except ValueError:
        return 0.5
    if age_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)


def extract_wikilinks(doc: str) -> list:
    """Ordered unique [[wikilink]] slugs, aliases and anchors stripped."""
    seen, out = set(), []
    for m in _WIKILINK_RE.finditer(doc):
        slug = m.group(1).strip()
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def page_ids_from_hits(ids: list, metas: list) -> list:
    """Collapse dense hits (page entries + section chunks) to unique parent-page
    ids, best-rank-first. Section entries carry metadata `parent`; a page whose
    best match is a below-the-fold section ranks by that section's position."""
    seen, out = set(), []
    for hit_id, meta in zip(ids, metas):
        page_id = (meta or {}).get("parent") or hit_id
        if page_id not in seen:
            seen.add(page_id)
            out.append(page_id)
    return out


def rank_hybrid(prompt: str, dense_ids: list, corpus_ids: list, corpus_docs: list,
                today=None) -> list:
    """Fuse dense + BM25 rankings over the page corpus, expand with wikilink
    neighbors of the top hits, then apply a bounded recency multiplier.
    Returns an ordered list of doc ids, best-first."""
    doc_by_id = dict(zip(corpus_ids, corpus_docs))
    scores = bm25_scores([tokenize(d) for d in corpus_docs], tokenize(prompt))
    bm25_ranked = [
        cid for cid, sc in sorted(zip(corpus_ids, scores), key=lambda kv: kv[1], reverse=True)
        if sc > 0
    ]
    fused = reciprocal_rank_fusion([dense_ids, bm25_ranked])

    # 1-hop graph expansion: pages [[linked]] from the top hits ride along at
    # low weight — enough to surface a See-also neighbor when the budget
    # allows, never enough to outvote a real dense/BM25 match.
    slug_to_id = {os.path.splitext(os.path.basename(cid))[0]: cid for cid in corpus_ids}
    neighbors = []
    for top_id, _ in fused[:LINK_TOP]:
        for slug in extract_wikilinks(doc_by_id.get(top_id, "")):
            nid = slug_to_id.get(slug)
            if nid and nid not in neighbors:
                neighbors.append(nid)
    if neighbors:
        fused = reciprocal_rank_fusion(
            [dense_ids, bm25_ranked, neighbors], weights=[1.0, 1.0, LINK_WEIGHT]
        )

    # Bounded recency multiplier (adjacent RRF ranks differ by ~0.0003, so a
    # weighted rank-list could never flip them — a multiplier can).
    rescored = [
        (doc_id,
         score * (RECENCY_FLOOR + (1 - RECENCY_FLOOR)
                  * freshness(extract_updated_date(doc_by_id.get(doc_id, "")), today)))
        for doc_id, score in fused
    ]
    rescored.sort(key=lambda kv: kv[1], reverse=True)
    return [doc_id for doc_id, _ in rescored]


def retrieve(prompt: str):
    """Shared retrieval core for the hook and the recall CLI.
    Returns (ranked_page_ids, docmap) — docmap is page id -> (document,
    metadata). Returns (None, None) when the index is missing or empty."""
    model = SentenceTransformer(MODEL_NAME)
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    try:
        collection = client.get_collection(COLLECTION)
    except Exception:
        return None, None  # index not built yet — fail silently

    count = collection.count()
    if count == 0:
        return None, None

    query_embedding = model.encode(prompt).tolist()
    dense = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(CANDIDATE_K, count),
        include=["documents", "metadatas"],
    )
    dense_ids = dense.get("ids", [[]])[0]
    dense_docs = dense.get("documents", [[]])[0]
    dense_metas = dense.get("metadatas", [[]])[0]
    if not dense_ids:
        return None, None

    # Section hits collapse to their parent page — the page stays the unit
    # of retrieval and injection.
    dense_page_ids = page_ids_from_hits(dense_ids, dense_metas)

    # page id -> (document, metadata), seeded from the dense result's page hits
    # (section entries hold chunk text, not the injectable full page).
    docmap = {
        i: (d, m) for i, d, m in zip(dense_ids, dense_docs, dense_metas)
        if not (m or {}).get("parent")
    }

    # Augment with BM25 over the page corpus, fused via RRF. Any failure
    # (e.g. corpus fetch unavailable) falls back to pure-dense order; pages
    # known only via a section hit drop out in that fallback.
    ranked_ids = dense_page_ids
    try:
        corpus = collection.get(include=["documents", "metadatas"])
        pages = [
            (i, d, m)
            for i, d, m in zip(corpus["ids"], corpus["documents"], corpus["metadatas"])
            if not (m or {}).get("parent")
        ]
        for i, d, m in pages:
            docmap.setdefault(i, (d, m))
        c_ids = [i for i, _, _ in pages]
        c_docs = [d for _, d, _ in pages]
        ranked_ids = rank_hybrid(prompt, dense_page_ids, c_ids, c_docs)
    except Exception:
        ranked_ids = dense_page_ids
    return ranked_ids, docmap


def main():
    payload = get_payload()
    prompt = payload.get("prompt", "") or payload.get("user_prompt", "")
    if not prompt:
        sys.exit(0)
    session_id = payload.get("session_id") or ""
    state = load_session_state(session_id) if session_id else None

    ranked_ids, docmap = retrieve(prompt)
    if not ranked_ids:
        sys.exit(0)

    def page_hash(doc_id):
        doc, meta = docmap[doc_id]
        return (meta or {}).get("hash") or hashlib.md5(doc.encode()).hexdigest()

    # Session dedup: skip pages already injected this session (same content),
    # backfilling from the next-ranked candidates — but only within the
    # INJECT_POOL head, so the tail of the ranking never pads the context.
    candidates = [i for i in ranked_ids if i in docmap][:INJECT_POOL]
    if state is not None:
        candidates = [i for i in candidates if state.get(i) != page_hash(i)]
    top_ids = candidates[:TOP_K]
    if not top_ids:
        sys.exit(0)

    # Critical pages first (stable — relevance order preserved within each group)
    # so a priority:critical rule is never the page dropped when the budget runs out.
    top_ids.sort(key=lambda i: not is_critical(docmap[i][0]))

    header = [
        "<memory_context>",
        "Semantically retrieved memory files (most relevant to this session):\n",
    ]
    footer = "</memory_context>"
    # Char budget for page blocks, after reserving the wrapper lines.
    budget = MAX_CONTEXT_CHARS - sum(len(h) + 1 for h in header) - len(footer) - 1

    blocks = []
    injected_ids = []
    for doc_id in top_ids:
        doc, meta = docmap[doc_id]
        fname = (meta or {}).get("filename", "unknown")
        cap = CRIT_PAGE_CHARS if is_critical(doc) else PER_PAGE_CHARS
        body = doc if len(doc) <= cap else (
            doc[:cap].rstrip() + "\n...[truncated - re-read full page on demand]"
        )
        block = f"### [{fname}]\n{body}\n"
        if len(block) + 1 > budget:   # no room left — drop the rest (least relevant)
            break
        blocks.append(block)
        injected_ids.append(doc_id)
        budget -= len(block) + 1

    if state is not None and injected_ids:
        state.update({i: page_hash(i) for i in injected_ids})
        save_session_state(session_id, state)

    context = "\n".join(header + blocks + [footer])
    # UserPromptSubmit hooks must output JSON with additionalContext to inject into model context
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def recall_cli(argv: list) -> int:
    """`search.py --query "..."` — agent-initiated recall, mid-session.
    Prints a ranked page list (or full pages with --full); never touches
    session-dedup state and never emits hook JSON."""
    import argparse
    ap = argparse.ArgumentParser(prog="search.py --query")
    ap.add_argument("--query", required=True)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(argv)

    ranked_ids, docmap = retrieve(args.query)
    if not ranked_ids:
        print("No results — index missing or empty. Run /my-memory:rebuild.")
        return 0

    top = [i for i in ranked_ids if i in docmap][: args.top]
    print(f'Top {len(top)} memory pages for: "{args.query}"\n')
    for rank, doc_id in enumerate(top, 1):
        doc, meta = docmap[doc_id]
        m = _DESC_RE.search(doc[:1500])
        desc = m.group(1).strip().strip("'\"") if m else "(no description)"
        fname = (meta or {}).get("filename", doc_id)
        print(f"{rank:2d}. {fname} — {desc}")
    if args.full:
        for doc_id in top:
            doc, meta = docmap[doc_id]
            print(f"\n### [{(meta or {}).get('filename', doc_id)}]")
            print(doc)
    return 0


if __name__ == "__main__":
    if "--query" in sys.argv:
        sys.exit(recall_cli(sys.argv[1:]))
    main()
