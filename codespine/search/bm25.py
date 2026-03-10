from __future__ import annotations

import math
import re
from collections import Counter

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=\D)(?=\d)|(?<=\d)(?=\D)")


def tokenize(text: str) -> list[str]:
    """Tokenize text, splitting on camelCase and underscores in addition to whitespace.

    'SolicitPanFetchActionCompletionEvent' → ['solicit', 'pan', 'fetch', 'action', 'completion', 'event']
    'get_symbol_context' → ['get', 'symbol', 'context']
    """
    raw_tokens = TOKEN_RE.findall(text or "")
    out: list[str] = []
    for tok in raw_tokens:
        # Split underscore-joined segments first, then camelCase within each
        for segment in tok.split("_"):
            if not segment:
                continue
            for part in _CAMEL_SPLIT_RE.split(segment):
                if part:
                    out.append(part.lower())
    return out


def rank_bm25(query: str, docs: list[tuple[str, str]], k1: float = 1.2, b: float = 0.75) -> list[tuple[str, float]]:
    """Simple BM25 ranker.

    docs: list of (doc_id, text)
    """
    if not docs:
        return []

    q_tokens = tokenize(query)
    if not q_tokens:
        return []

    tokenized_docs = [(doc_id, tokenize(text)) for doc_id, text in docs]
    avgdl = sum(len(tokens) for _, tokens in tokenized_docs) / max(len(tokenized_docs), 1)

    doc_freq: Counter[str] = Counter()
    term_freqs: dict[str, Counter[str]] = {}
    for doc_id, tokens in tokenized_docs:
        tf = Counter(tokens)
        term_freqs[doc_id] = tf
        for token in tf.keys():
            doc_freq[token] += 1

    n_docs = len(tokenized_docs)
    scores: dict[str, float] = {doc_id: 0.0 for doc_id, _ in tokenized_docs}
    for token in q_tokens:
        df = doc_freq.get(token, 0)
        if df == 0:
            continue
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for doc_id, tokens in tokenized_docs:
            tf = term_freqs[doc_id].get(token, 0)
            if tf == 0:
                continue
            dl = len(tokens)
            denom = tf + k1 * (1 - b + b * (dl / max(avgdl, 1e-9)))
            scores[doc_id] += idf * ((tf * (k1 + 1)) / max(denom, 1e-9))

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
