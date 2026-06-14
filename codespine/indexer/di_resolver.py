"""Dependency-injection binding resolver for CodeSpine.

Produces INJECTS and BINDS_INTERFACE edges by inspecting:

1. @Inject / @Autowired fields  →  INJECTS(consumer_class → provider_class)
2. @Provides / @Bean methods    →  INJECTS(config_class → consumer_classes)
3. @Component/@Service classes  →  BINDS_INTERFACE(impl → interface) when
                                   the class implements an indexed interface.

All edges are written in sub-batched transactions by the engine after
call resolution completes.

Edge schemas
------------
INJECTS(FROM Class TO Class,
        framework STRING,   # "spring" | "guice" | "jakarta" | "javax" | "unknown"
        binding_type STRING, # "field_inject" | "constructor_inject" |
                              #  "provides_binding" | "bean_method" | "component_scan"
        confidence DOUBLE)

BINDS_INTERFACE(FROM Class TO Class,
                confidence DOUBLE,
                reason STRING)   # "implements" | "extends"
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

LOGGER = logging.getLogger(__name__)

# Annotations that mark a field/constructor parameter as injection point.
_INJECT_ANNOTATIONS = frozenset({
    "Inject", "Autowired", "Resource",
    "javax.inject.Inject", "jakarta.inject.Inject",
    "org.springframework.beans.factory.annotation.Autowired",
})

# Annotations that mark a method as a DI provider.
_PROVIDES_ANNOTATIONS = frozenset({
    "Provides", "Bean",
    "com.google.inject.Provides",
    "org.springframework.context.annotation.Bean",
})

# Component-scan annotations: classes with these are treated as injectable.
_COMPONENT_ANNOTATIONS = frozenset({
    "Component", "Service", "Repository", "Controller", "RestController",
    "Configuration", "ManagedBean", "Named",
    "org.springframework.stereotype.Component",
    "org.springframework.stereotype.Service",
    "javax.inject.Named", "jakarta.inject.Named",
})

# DI frameworks by annotation prefix
_FRAMEWORK_MAP: dict[str, str] = {
    "Autowired": "spring",
    "Component": "spring", "Service": "spring", "Repository": "spring",
    "Controller": "spring", "RestController": "spring", "Bean": "spring",
    "Configuration": "spring",
    "Inject": "guice",
    "Provides": "guice",
    "Named": "guice",
    "Resource": "jakarta",
    "ManagedBean": "jakarta",
}


def _framework(annotation: str) -> str:
    simple = annotation.split(".")[-1]
    if "springframework" in annotation:
        return "spring"
    if "google.inject" in annotation:
        return "guice"
    if "jakarta.inject" in annotation:
        return "jakarta"
    if "javax.inject" in annotation:
        return "javax"
    return _FRAMEWORK_MAP.get(simple, "unknown")


def _simple(annotation: str) -> str:
    return annotation.split(".")[-1]


def resolve_di_bindings(
    class_catalog: dict[str, list[str]],
    class_meta: dict[str, dict[str, Any]],
    parsed_classes: list[dict[str, Any]],
    fqcn_to_class_ids: dict[str, list[str]],
) -> Iterator[tuple[str, str, str, str, float, str]]:
    """Yield (src_class_id, dst_class_id, framework, binding_type, confidence, edge_type) tuples.

    edge_type is "INJECTS" or "BINDS_INTERFACE".

    Parameters
    ----------
    class_catalog      : name → [fqcn, ...]  (full project catalog)
    class_meta         : class_id → {fqcn, annotations, interfaces, extends, imports, package}
    parsed_classes     : list of dicts from build_overlay_file_entry / engine — each has:
                           id, fqcn, name, package, file_id, and extra DI metadata fields
                           injected_fields, methods_with_provides
    fqcn_to_class_ids  : fqcn → [class_id, ...]
    """
    # Build a reverse FQCN lookup: fqcn → class_id (first match)
    fqcn_to_id: dict[str, str] = {}
    for fqcn, ids in fqcn_to_class_ids.items():
        if ids:
            fqcn_to_id[fqcn] = ids[0]

    # Build simple-name → [fqcn] lookup for type resolution.
    name_to_fqcns: dict[str, list[str]] = {}
    for fqcn in fqcn_to_id:
        simple = fqcn.split(".")[-1]
        name_to_fqcns.setdefault(simple, []).append(fqcn)
    # Also add class_catalog entries.
    for name, fqcns in class_catalog.items():
        for fqcn in fqcns:
            if fqcn not in name_to_fqcns.get(name, []):
                name_to_fqcns.setdefault(name, []).append(fqcn)

    # Build a set of class_ids for component-scan eligible classes.
    component_class_ids: set[str] = set()
    for meta in class_meta.values():
        anns = [_simple(a) for a in (meta.get("annotations") or [])]
        if any(a in _COMPONENT_ANNOTATIONS for a in anns):
            cid = meta.get("id") or ""
            if cid:
                component_class_ids.add(cid)

    def _resolve_type(type_name: str, package: str, imports: list[str]) -> list[str]:
        """Best-effort resolution of a simple type name to known class IDs."""
        if not type_name:
            return []
        # Strip generics: List<Foo> → Foo
        if "<" in type_name:
            type_name = type_name[type_name.index("<") + 1:].rstrip(">").strip()
        simple = type_name.split(".")[-1]
        candidates: list[str] = []
        # 1. Exact FQ match.
        if "." in type_name and type_name in fqcn_to_id:
            candidates.append(fqcn_to_id[type_name])
        # 2. Same package.
        same_pkg = f"{package}.{simple}" if package else simple
        if same_pkg in fqcn_to_id:
            candidates.append(fqcn_to_id[same_pkg])
        # 3. From imports.
        for imp in imports:
            if imp.endswith(f".{simple}") and imp in fqcn_to_id:
                candidates.append(fqcn_to_id[imp])
        # 4. Name catalog.
        for fqcn in name_to_fqcns.get(simple, []):
            cid = fqcn_to_id.get(fqcn)
            if cid:
                candidates.append(cid)
        return list(dict.fromkeys(candidates))  # deduplicate preserving order

    # ---------------------------------------------------------------
    # Process each parsed class.
    # ---------------------------------------------------------------
    for cls in parsed_classes:
        src_id = cls.get("id") or cls.get("class_id") or ""
        if not src_id:
            continue
        meta = class_meta.get(src_id, {})
        package = str(meta.get("package") or cls.get("package") or "")
        imports: list[str] = meta.get("imports") or []
        cls_annotations: list[str] = [_simple(a) for a in (meta.get("annotations") or cls.get("annotations") or [])]

        # --- 1. @Inject / @Autowired fields → INJECTS edges ---------
        for fld in cls.get("injected_fields") or []:
            inj_ann = fld.get("injection_annotation") or ""
            if not inj_ann or _simple(inj_ann) not in frozenset({
                "Inject", "Autowired", "Resource"
            }):
                continue
            type_name = fld.get("type_name") or ""
            dst_ids = _resolve_type(type_name, package, imports)
            fw = _framework(inj_ann)
            for dst_id in dst_ids:
                if dst_id != src_id:
                    yield (src_id, dst_id, fw, "field_inject", 0.85, "INJECTS")

        # --- 1b. @Inject constructor/setter parameters → INJECTS edges ------
        for p in cls.get("injected_params") or []:
            inj_ann = p.get("injection_annotation") or ""
            if not inj_ann or _simple(inj_ann) not in frozenset({
                "Inject", "Autowired"
            }):
                continue
            type_name = p.get("type_name") or ""
            dst_ids = _resolve_type(type_name, package, imports)
            fw = _framework(inj_ann)
            for dst_id in dst_ids:
                if dst_id != src_id:
                    yield (src_id, dst_id, fw, "constructor_inject", 0.85, "INJECTS")

        # --- 2. @Provides / @Bean methods → INJECTS (config → type) -
        for method in cls.get("methods_with_provides") or []:
            provides_type = method.get("provides_type") or ""
            if not provides_type:
                continue
            ann = method.get("provides_annotation") or "Provides"
            fw = _framework(ann)
            dst_ids = _resolve_type(provides_type, package, imports)
            for dst_id in dst_ids:
                if dst_id != src_id:
                    binding_type = "bean_method" if _simple(ann) == "Bean" else "provides_binding"
                    yield (src_id, dst_id, fw, binding_type, 0.9, "INJECTS")

        # --- 3. Component-scan: class implementing interfaces → BINDS_INTERFACE
        if any(a in _COMPONENT_ANNOTATIONS for a in cls_annotations):
            for iface_name in meta.get("interfaces") or []:
                dst_ids = _resolve_type(iface_name, package, imports)
                for dst_id in dst_ids:
                    if dst_id != src_id:
                        yield (src_id, dst_id, 1.0, "implements", 0.95, "BINDS_INTERFACE")
            extends_name = meta.get("extends") or ""
            if extends_name:
                dst_ids = _resolve_type(extends_name, package, imports)
                for dst_id in dst_ids:
                    if dst_id != src_id:
                        yield (src_id, dst_id, 0.7, "extends", 0.7, "BINDS_INTERFACE")
