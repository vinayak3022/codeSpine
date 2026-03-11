from __future__ import annotations

import itertools
import os
import subprocess
from collections import Counter, defaultdict

from codespine.config import SETTINGS
from codespine.indexer.symbol_builder import file_id


def _git_changed_file_sets(repo_path: str, months: int) -> list[set[str]]:
    cmd = [
        "git",
        "-C",
        repo_path,
        "log",
        "--name-only",
        "--pretty=format:__COMMIT__",
        f"--since={months}.months",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []

    changesets: list[set[str]] = []
    current: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line == "__COMMIT__":
            if current:
                changesets.append(current)
            current = set()
            continue
        if line:
            current.add(line)
    if current:
        changesets.append(current)
    return changesets


def compute_coupling(
    store,
    repo_path: str,
    project_id: str,
    months: int = SETTINGS.default_coupling_months,
    min_strength: float = SETTINGS.default_min_coupling_strength,
    min_cochanges: int = SETTINGS.default_min_cochanges,
    progress=None,
) -> list[dict]:
    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    _ping("reading git history")
    changesets = _git_changed_file_sets(repo_path, months)
    if not changesets:
        return []

    _ping(f"{len(changesets)} commits, computing co-changes")
    file_changes = Counter()
    co_changes: Counter[tuple[str, str]] = Counter()

    for cs in changesets:
        for path in cs:
            file_changes[path] += 1
        for a, b in itertools.combinations(sorted(cs), 2):
            co_changes[(a, b)] += 1

    _ping(f"{len(co_changes)} pairs, filtering and persisting")
    results = []
    for (a, b), pair_count in co_changes.items():
        denom = max(file_changes[a], file_changes[b])
        strength = pair_count / max(denom, 1)
        if strength < min_strength or pair_count < min_cochanges:
            continue

        aid = file_id(project_id, a)
        bid = file_id(project_id, b)
        store.upsert_coupling(aid, bid, strength, pair_count, months)
        results.append(
            {
                "file_a": a,
                "file_b": b,
                "strength": strength,
                "cochanges": pair_count,
            }
        )

    results.sort(key=lambda r: (r["strength"], r["cochanges"]), reverse=True)
    return results


def get_coupling(store, symbol: str | None = None, months: int = 6, min_strength: float = 0.3, min_cochanges: int = 3) -> dict:
    if symbol:
        recs = store.query_records(
            """
            MATCH (s:Symbol)-[:DECLARES]-(f:File)-[r:CO_CHANGED_WITH]-(f2:File)
            WHERE s.id = $q OR lower(s.fqname) = lower($q) OR lower(s.name) = lower($q)
            AND r.strength >= $min_strength AND r.cochanges >= $min_cochanges
            RETURN f.path as file, f2.path as coupled_file, r.strength as strength, r.cochanges as cochanges
            ORDER BY strength DESC, cochanges DESC
            LIMIT 200
            """,
            {
                "q": symbol,
                "min_strength": min_strength,
                "min_cochanges": min_cochanges,
            },
        )
        return {"symbol": symbol, "couplings": recs}

    recs = store.query_records(
        """
        MATCH (f:File)-[r:CO_CHANGED_WITH]-(f2:File)
        WHERE r.months = $months AND r.strength >= $min_strength AND r.cochanges >= $min_cochanges
        RETURN f.path as file, f2.path as coupled_file, r.strength as strength, r.cochanges as cochanges
        ORDER BY strength DESC, cochanges DESC
        LIMIT 500
        """,
        {
            "months": months,
            "min_strength": min_strength,
            "min_cochanges": min_cochanges,
        },
    )
    return {"symbol": None, "couplings": recs}
