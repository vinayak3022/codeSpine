from __future__ import annotations

from collections import defaultdict

# ── Annotation sets ──────────────────────────────────────────────────
# Entry-point annotations — exempt even in strict mode.  These represent
# actual runtime entry points that the framework calls reflectively.
ENTRY_POINT_ANNOTATIONS = {
    # JUnit / testing
    "Test",
    "ParameterizedTest",
    "BeforeEach",
    "AfterEach",
    "BeforeAll",
    "AfterAll",
    # Spring – web entry points
    "RequestMapping",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
    "MessageMapping",
    # Spring – messaging / async entry points
    "KafkaListener",
    "RabbitListener",
    "JmsListener",
    "SqsListener",
    "StreamListener",
    # Spring – lifecycle / event hooks
    "PostConstruct",
    "PreDestroy",
    "EventListener",
    "TransactionalEventListener",
    "Scheduled",
    # JPA / ORM lifecycle hooks
    "PrePersist",
    "PostPersist",
    "PreUpdate",
    "PostUpdate",
    "PreRemove",
    "PostRemove",
    "PostLoad",
}

# Broad annotations — exempt only in normal mode.  These indicate the
# method is *likely* used via DI / serialisation / reflection, but in a
# strict audit the user may want to verify that manually.
BROAD_ANNOTATIONS = {
    # Java standard
    "Override",
    # Spring – component model (class-level; methods inside are never "dead")
    "Component",
    "Service",
    "Repository",
    "Controller",
    "RestController",
    "Configuration",
    "Bean",
    "Aspect",
    # Spring Data / persistence
    "Query",
    "Modifying",
    # Guice DI
    "Inject",
    "Provides",
    "Singleton",
    "Named",
    "Qualifier",
    # Jakarta / javax DI
    "ApplicationScoped",
    "RequestScoped",
    "SessionScoped",
    "Dependent",
    # Jackson / serialization
    "JsonCreator",
    "JsonProperty",
    "JsonDeserialize",
    "JsonSerialize",
}

# Full set used in normal mode
EXEMPT_ANNOTATIONS = ENTRY_POINT_ANNOTATIONS | BROAD_ANNOTATIONS

EXEMPT_CONTRACT_METHODS = {
    "toString",
    "hashCode",
    "equals",
    "compareTo",
}

EXEMPT_CLASS_NAME_SUFFIXES = {
    "Controller",
    "RestController",
    "Service",
    "Repository",
    "Listener",
    "Handler",
    "Mapper",
    "Converter",
    "Factory",
    "Configuration",
    "Config",
    "Entity",
}

EXEMPT_FILE_PATH_PARTS = {
    "/controller/",
    "/controllers/",
    "/service/",
    "/services/",
    "/repository/",
    "/repositories/",
    "/listener/",
    "/listeners/",
    "/handler/",
    "/handlers/",
    "/mapper/",
    "/mappers/",
    "/entity/",
    "/entities/",
    "/config/",
}


def _modifier_tokens(modifiers) -> set[str]:
    if not modifiers:
        return set()
    return {str(m).strip() for m in modifiers}


def _matched_annotation(mods: set[str], annotation_set: set[str]) -> str | None:
    """Return the first annotation in *mods* that appears in *annotation_set*, or None."""
    for m in mods:
        bare = m.lstrip("@")
        if bare in annotation_set:
            return bare
    return None


def _assign_confidence(candidate: dict, strict: bool) -> str:
    """Assign a confidence level (high / medium / low) to each dead method.

    Heuristic:
      - high:   private method with no callers — almost certainly dead.
      - medium: package-private or protected method with no callers.
      - low:    public method — could be called via reflection / external JAR.
    In strict mode, every method that passes the minimal exemptions is 'high'.
    """
    if strict:
        return "high"
    mods = _modifier_tokens(candidate.get("modifiers"))
    if "private" in mods:
        return "high"
    if "public" in mods:
        return "low"
    # Default: protected / package-private
    return "medium"


def _simple_class_name(fqcn: str | None) -> str:
    if not fqcn:
        return ""
    return fqcn.rsplit(".", 1)[-1]


def detect_dead_code(store, limit: int = 200, project: str | None = None, strict: bool = False) -> list[dict] | None:
    """Java-aware dead code detection with exemption passes.

    Parameters:
      limit   – Max results to return.
      project – Scope to a single module.
      strict  – When True, only exempt main()/@Test methods and explicit
                entry-point annotations (RequestMapping, KafkaListener, etc.).
                Skips the broad bean-getter/setter, contract-method,
                constructor, Override, and DI annotation exemptions.

    Returns a list of dead method dicts, each with:
      method_id, name, signature, class_fqcn, file_path, reason, confidence.

    The return value is augmented with a ``_stats`` entry (a sentinel dict
    with key ``_stats``) containing pre/post-exemption counts, a breakdown
    of exemption reasons, and a sample of exempted methods so callers can
    validate that the exemption logic is working correctly.
    """
    if project:
        candidates = store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $proj
              AND NOT EXISTS { MATCH (:Method)-[:CALLS]->(m) }
            RETURN m.id as method_id,
                   m.name as name,
                   m.signature as signature,
                   m.modifiers as modifiers,
                   c.fqcn as class_fqcn,
                   m.is_constructor as is_constructor,
                   m.is_test as is_test,
                   f.path as file_path
            LIMIT $limit
            """,
            {"limit": int(limit * 5), "proj": project},
        )
    else:
        candidates = store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id
              AND NOT EXISTS { MATCH (:Method)-[:CALLS]->(m) }
            RETURN m.id as method_id,
                   m.name as name,
                   m.signature as signature,
                   m.modifiers as modifiers,
                   c.fqcn as class_fqcn,
                   m.is_constructor as is_constructor,
                   m.is_test as is_test,
                   f.path as file_path
            LIMIT $limit
            """,
            {"limit": int(limit * 5)},
        )

    if not candidates:
        return []

    n_candidates = len(candidates)

    # Track exemptions as {method_id: reason} instead of a plain set
    exempt: dict[str, str] = {}

    # Choose annotation set based on mode
    annotations_to_check = ENTRY_POINT_ANNOTATIONS if strict else EXEMPT_ANNOTATIONS

    # ── Exemption passes ──────────────────────────────────────────────
    for c in candidates:
        mid = c["method_id"]
        if mid in exempt:
            continue
        sig = (c.get("signature") or "").lower()
        name = c.get("name") or ""
        mods = _modifier_tokens(c.get("modifiers"))

        # Always exempt test methods and main()
        if c.get("is_test"):
            exempt[mid] = "test_method"
            continue
        if name == "main" and "string[]" in sig:
            exempt[mid] = "main_method"
            continue

        # Exempt methods with entry-point (strict) or all framework (normal) annotations
        matched = _matched_annotation(mods, annotations_to_check)
        if matched:
            exempt[mid] = f"annotation:{matched}"
            continue

        # ── Broad exemptions (only in normal mode) ────────────────────
        if not strict:
            if c.get("is_constructor"):
                exempt[mid] = "constructor"
                continue
            simple_cls = _simple_class_name(c.get("class_fqcn"))
            if any(simple_cls.endswith(suffix) for suffix in EXEMPT_CLASS_NAME_SUFFIXES):
                exempt[mid] = f"class_role:{simple_cls}"
                continue
            file_path = (c.get("file_path") or "").replace("\\", "/").lower()
            if any(part in file_path for part in EXEMPT_FILE_PATH_PARTS):
                exempt[mid] = "framework_path"
                continue
            if name in EXEMPT_CONTRACT_METHODS:
                exempt[mid] = f"contract_method:{name}"
                continue
            # Java bean-ish APIs often rely on reflection/serialization.
            if "public" in mods and (
                name.startswith("get") or name.startswith("set") or name.startswith("is")
            ):
                exempt[mid] = "bean_accessor"
                continue
            # Reflection-style hooks
            if name in {"valueOf", "fromString", "builder"}:
                exempt[mid] = f"reflection_hook:{name}"
                continue

    # Exempt methods that DIRECTLY override another method.
    # In strict mode, overrides are NOT exempted — if nobody calls the method,
    # it's flagged regardless of whether it overrides a parent.
    if not strict:
        override_methods = store.query_records(
            """
            MATCH (m:Method)-[:OVERRIDES]->(:Method)
            RETURN DISTINCT m.id as method_id
            """
        )
        for r in override_methods:
            mid = r["method_id"]
            if mid not in exempt:
                exempt[mid] = "method_override"

        base_contract_methods = store.query_records(
            """
            MATCH (:Method)-[:OVERRIDES]->(m:Method)
            RETURN DISTINCT m.id as method_id
            """
        )
        for r in base_contract_methods:
            mid = r["method_id"]
            if mid not in exempt:
                exempt[mid] = "interface_or_base_contract"

    # ── Build dead list ───────────────────────────────────────────────
    dead = []
    for c in candidates:
        if c["method_id"] in exempt:
            continue
        dead.append(
            {
                "method_id": c["method_id"],
                "name": c.get("name"),
                "signature": c.get("signature"),
                "class_fqcn": c.get("class_fqcn"),
                "file_path": c.get("file_path"),
                "confidence": _assign_confidence(c, strict),
                "reason": "no_incoming_calls_after_exemptions",
            }
        )

    result = dead[:limit]

    # ── Stats with exemption breakdown ────────────────────────────────
    reason_counts: dict[str, int] = defaultdict(int)
    for reason in exempt.values():
        # Group annotation reasons by prefix for readability
        key = reason.split(":")[0] if ":" in reason else reason
        reason_counts[key] += 1

    # Sample of exempted methods (up to 10) for user inspection
    exempted_sample = []
    for mid, reason in list(exempt.items())[:10]:
        candidate = next((c for c in candidates if c["method_id"] == mid), None)
        if candidate:
            exempted_sample.append({
                "name": candidate.get("name"),
                "signature": candidate.get("signature"),
                "class_fqcn": candidate.get("class_fqcn"),
                "exemption_reason": reason,
            })

    if strict:
        exemption_note = (
            "STRICT MODE: Only test methods, main(), and entry-point "
            "annotations (RequestMapping, KafkaListener, Scheduled, etc.) "
            "are exempted. Constructors, getters/setters, @Override, DI "
            "annotations, and contract methods are NOT exempt."
        )
    else:
        exemption_note = (
            "Exemptions cover: constructors, test methods, main(), "
            "toString/hashCode/equals/compareTo, public getters/setters, "
            "methods with DI/framework annotations, and direct method overrides. "
            "Use strict=True for minimal exemptions."
        )
    result.append({
        "_stats": {
            "candidates_with_no_callers": n_candidates,
            "exempted": len(exempt),
            "dead_returned": len(result),
            "mode": "strict" if strict else "normal",
            "note": exemption_note,
            "exemptions_breakdown": dict(reason_counts),
            "exempted_sample": exempted_sample,
        }
    })

    return result
