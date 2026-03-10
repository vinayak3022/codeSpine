from __future__ import annotations

EXEMPT_ANNOTATIONS = {
    # Java standard
    "Override",
    # JUnit / testing
    "Test",
    "ParameterizedTest",
    "BeforeEach",
    "AfterEach",
    "BeforeAll",
    "AfterAll",
    # Spring – component model (class-level; methods inside are never "dead")
    "Component",
    "Service",
    "Repository",
    "Controller",
    "RestController",
    "Configuration",
    "Bean",
    "Aspect",
    # Spring – lifecycle / event hooks
    "PostConstruct",
    "PreDestroy",
    "EventListener",
    "TransactionalEventListener",
    "Scheduled",
    # Spring – web entry points
    "RequestMapping",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
    "MessageMapping",
    # Spring – messaging / async
    "KafkaListener",
    "RabbitListener",
    "JmsListener",
    "SqsListener",
    "StreamListener",
    # Spring Data / persistence
    "Query",
    "Modifying",
    # Guice DI
    "Inject",
    "Provides",
    "Singleton",
    "Named",
    "Qualifier",
    # Jakarta / javax DI (same semantics as Guice/Spring variants)
    "ApplicationScoped",
    "RequestScoped",
    "SessionScoped",
    "Dependent",
    # Jackson / serialization (called reflectively)
    "JsonCreator",
    "JsonProperty",
    "JsonDeserialize",
    "JsonSerialize",
}

EXEMPT_CONTRACT_METHODS = {
    "toString",
    "hashCode",
    "equals",
    "compareTo",
}


def _modifier_tokens(modifiers) -> set[str]:
    if not modifiers:
        return set()
    return {str(m).strip() for m in modifiers}


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


def detect_dead_code(store, limit: int = 200, project: str | None = None, strict: bool = False) -> list[dict] | None:
    """Java-aware dead code detection with exemption passes.

    Parameters:
      limit   – Max results to return.
      project – Scope to a single module.
      strict  – When True, only exempt main()/@Test methods and explicit
                entry-point annotations. Skips the broad bean-getter/setter,
                contract-method, and constructor exemptions.

    Returns a list of dead method dicts, each with:
      method_id, name, signature, class_fqcn, file_path, reason, confidence.

    The return value is augmented with a ``_stats`` entry (a sentinel dict
    with key ``_stats``) containing pre/post-exemption counts so callers can
    show users that the exemption logic is actually working:
      candidates_with_no_callers, exempted, dead_returned
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
    exempt: set[str] = set()

    # Minimal exemptions (apply in both normal and strict mode)
    for c in candidates:
        sig = (c.get("signature") or "").lower()
        name = c.get("name") or ""
        mods = _modifier_tokens(c.get("modifiers"))

        # Always exempt test methods and main()
        if c.get("is_test"):
            exempt.add(c["method_id"])
        if name == "main" and "string[]" in sig:
            exempt.add(c["method_id"])

        # Always exempt explicit entry-point annotations (@Test, @RequestMapping, etc.)
        if any(m.lstrip("@") in EXEMPT_ANNOTATIONS for m in mods):
            exempt.add(c["method_id"])

        # Broad exemptions (only in normal mode, skipped in strict mode)
        if not strict:
            if c.get("is_constructor"):
                exempt.add(c["method_id"])
            if name in EXEMPT_CONTRACT_METHODS:
                exempt.add(c["method_id"])
            # Java bean-ish APIs often rely on reflection/serialization.
            if "public" in mods and (name.startswith("get") or name.startswith("set") or name.startswith("is")):
                exempt.add(c["method_id"])
            # Reflection-style hooks
            if name in {"valueOf", "fromString", "builder"}:
                exempt.add(c["method_id"])

    # Exempt methods that DIRECTLY override another method (precise: only the
    # specific overriding method is exempted, not the entire implementing class).
    # NOTE: we intentionally do NOT use the class-level IMPLEMENTS relation here
    # because that would exempt ALL methods of every class that implements ANY
    # interface — in a typical Spring project that wipes out almost everything
    # and produces 0 dead code results.
    # In strict mode, overrides are NOT exempted — if nobody calls the method,
    # it's flagged regardless of whether it overrides a parent.
    if not strict:
        override_methods = store.query_records(
            """
            MATCH (m:Method)-[:OVERRIDES]->(:Method)
            RETURN DISTINCT m.id as method_id
            """
        )
        exempt.update(r["method_id"] for r in override_methods)

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

    # Append stats as a sentinel entry so the MCP layer can surface them
    # without changing the return type.  Callers should strip entries that
    # have a "_stats" key when iterating over method results.
    if strict:
        exemption_note = (
            "STRICT MODE: Only test methods, main(), and explicit entry-point "
            "annotations are exempted. Constructors, getters/setters, "
            "contract methods, and overrides are NOT exempt."
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
        }
    })

    return result
