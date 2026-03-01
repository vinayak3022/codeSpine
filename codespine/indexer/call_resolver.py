from __future__ import annotations

from collections import defaultdict

from codespine.noise.blocklist import NOISE_METHOD_NAMES


def resolve_calls(method_catalog: dict[str, str], calls: dict[str, list[str]]) -> list[tuple[str, str, float, str]]:
    """Resolve call names to known method ids.

    Returns tuples: (source_method_id, target_method_id, confidence, reason)
    """
    name_to_method_ids: dict[str, list[str]] = defaultdict(list)
    for method_id, signature in method_catalog.items():
        method_name = signature.split("(", 1)[0]
        name_to_method_ids[method_name].append(method_id)

    edges: list[tuple[str, str, float, str]] = []
    for source_id, call_names in calls.items():
        for call_name in call_names:
            if call_name in NOISE_METHOD_NAMES:
                continue
            targets = name_to_method_ids.get(call_name, [])
            if not targets:
                continue
            if len(targets) == 1:
                edges.append((source_id, targets[0], 1.0, "exact_name_unique"))
            else:
                # Ambiguous method-name match: add lower-confidence edges to all candidates.
                for target_id in targets:
                    edges.append((source_id, target_id, 0.5, "fuzzy_name_ambiguous"))

    return edges
