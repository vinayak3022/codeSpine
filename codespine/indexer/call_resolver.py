from __future__ import annotations

from collections import defaultdict

from codespine.noise.blocklist import NOISE_METHOD_NAMES


def _simple_type_name(type_name: str | None) -> str:
    if not type_name:
        return ""
    base = type_name.strip().replace("[]", "")
    return base.split(".")[-1]


def _resolve_type_candidates(type_name: str | None, context: dict, class_catalog: dict[str, list[str]]) -> list[str]:
    """Best-effort type resolution using fqcn/simple-name, imports, and package."""
    if not type_name:
        return []
    resolved: list[str] = []
    raw = type_name.strip()
    simple = _simple_type_name(raw)

    # Direct FQCN hint.
    if "." in raw:
        resolved.append(raw)

    # Imported types.
    imports = context.get("imports", []) or []
    for imp in imports:
        if imp.endswith(f".{simple}"):
            resolved.append(imp)

    # Same package fallback.
    pkg = context.get("package", "")
    if pkg:
        resolved.append(f"{pkg}.{simple}")

    # Indexed type matches by simple class name.
    resolved.extend(class_catalog.get(simple, []))

    # Stable unique order.
    uniq: list[str] = []
    seen = set()
    for item in resolved:
        if item and item not in seen:
            uniq.append(item)
            seen.add(item)
    return uniq


def resolve_calls(
    method_catalog: dict[str, dict],
    calls: dict[str, list],
    method_context: dict[str, dict],
    class_catalog: dict[str, list[str]],
) -> list[tuple[str, str, float, str]]:
    """Resolve call names to known method ids.

    Returns tuples: (source_method_id, target_method_id, confidence, reason)
    """
    name_arity_to_method_ids: dict[tuple[str, int], list[str]] = defaultdict(list)
    class_method_index: dict[str, dict[tuple[str, int], list[str]]] = defaultdict(lambda: defaultdict(list))
    for method_id, meta in method_catalog.items():
        key = (meta["name"], int(meta["param_count"]))
        name_arity_to_method_ids[key].append(method_id)
        class_method_index[meta["class_fqcn"]][key].append(method_id)

    edges: list[tuple[str, str, float, str]] = []
    for source_id, call_sites in calls.items():
        src_meta = method_catalog.get(source_id, {})
        src_ctx = method_context.get(source_id, {})
        src_class = src_meta.get("class_fqcn", "")
        local_types = src_ctx.get("local_types", {}) or {}
        field_types = src_ctx.get("field_types", {}) or {}

        for call in call_sites:
            call_name = call.name
            if call_name in NOISE_METHOD_NAMES:
                continue

            key = (call_name, int(call.arg_count))
            targets: list[str] = []
            confidence = 0.5
            reason = "fuzzy_name_ambiguous"

            receiver = (call.receiver or "").strip() if getattr(call, "receiver", None) else ""
            if receiver:
                receiver_type = None
                receiver_is_this = False
                if receiver == "this":
                    receiver_type = src_class
                    receiver_is_this = True
                elif receiver in local_types:
                    receiver_type = local_types[receiver]
                elif receiver in field_types:
                    receiver_type = field_types[receiver]
                else:
                    receiver_type = receiver

                receiver_fqcn_candidates = _resolve_type_candidates(receiver_type, src_ctx, class_catalog)

                for fqcn in receiver_fqcn_candidates:
                    targets.extend(class_method_index.get(fqcn, {}).get(key, []))

                if targets:
                    confidence = 1.0 if receiver_is_this else 0.8
                    reason = "receiver_this_exact" if receiver_is_this else "receiver_method_match"

            if not targets:
                in_class = class_method_index.get(src_class, {}).get(key, [])
                if in_class:
                    targets = in_class
                    confidence = 1.0
                    reason = "intra_class_exact"

            if not targets:
                # Prefer same-package candidates before global fallback.
                src_pkg = src_ctx.get("package", "")
                same_pkg = []
                for mid in name_arity_to_method_ids.get(key, []):
                    fqcn = method_catalog.get(mid, {}).get("class_fqcn", "")
                    if src_pkg and fqcn.startswith(f"{src_pkg}."):
                        same_pkg.append(mid)
                targets = same_pkg or name_arity_to_method_ids.get(key, [])
                if len(targets) == 1:
                    confidence = 1.0
                    reason = "exact_name_arity_unique"
                elif len(targets) > 1:
                    confidence = 0.5
                    reason = "fuzzy_name_arity_ambiguous"

            if not targets:
                continue
            for target_id in targets:
                edges.append((source_id, target_id, confidence, reason))

    return edges
