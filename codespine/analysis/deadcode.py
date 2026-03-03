from __future__ import annotations

EXEMPT_ANNOTATIONS = {
    "Override",
    "Test",
    "ParameterizedTest",
    "Bean",
    "PostConstruct",
    "PreDestroy",
    "Scheduled",
    "KafkaListener",
    "EventListener",
    "JsonCreator",
    "Inject",
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


def detect_dead_code(store, limit: int = 200, project: str | None = None) -> list[dict]:
    """Java-aware dead code detection with exemption passes."""
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
                   m.is_test as is_test
            LIMIT $limit
            """,
            {"limit": int(limit * 3), "proj": project},
        )
    else:
        candidates = store.query_records(
            """
            MATCH (m:Method), (c:Class)
            WHERE m.class_id = c.id
              AND NOT EXISTS { MATCH (:Method)-[:CALLS]->(m) }
            RETURN m.id as method_id,
                   m.name as name,
                   m.signature as signature,
                   m.modifiers as modifiers,
                   c.fqcn as class_fqcn,
                   m.is_constructor as is_constructor,
                   m.is_test as is_test
            LIMIT $limit
            """,
            {"limit": int(limit * 3)},
        )

    if not candidates:
        return []

    exempt: set[str] = set()

    # Exempt constructors, test methods, and Java main entrypoints.
    for c in candidates:
        sig = (c.get("signature") or "").lower()
        name = c.get("name") or ""
        mods = _modifier_tokens(c.get("modifiers"))
        if c.get("is_constructor"):
            exempt.add(c["method_id"])
        if c.get("is_test"):
            exempt.add(c["method_id"])
        if name == "main" and "string[]" in sig:
            exempt.add(c["method_id"])
        if name in EXEMPT_CONTRACT_METHODS:
            exempt.add(c["method_id"])
        if any(m.lstrip("@") in EXEMPT_ANNOTATIONS for m in mods):
            exempt.add(c["method_id"])
        # Java bean-ish APIs often rely on reflection/serialization.
        if "public" in mods and (name.startswith("get") or name.startswith("set") or name.startswith("is")):
            exempt.add(c["method_id"])
        # Reflection-style hooks
        if name in {"valueOf", "fromString", "builder"}:
            exempt.add(c["method_id"])

    # Exempt override/interface contract methods if relation exists.
    override_methods = store.query_records(
        """
        MATCH (m:Method)-[:OVERRIDES]->(:Method)
        RETURN DISTINCT m.id as method_id
        """
    )
    interface_methods = store.query_records(
        """
        MATCH (c:Class)-[:IMPLEMENTS]->(:Class), (m:Method)
        WHERE m.class_id = c.id
        RETURN DISTINCT m.id as method_id
        """
    )
    exempt.update(r["method_id"] for r in override_methods)
    exempt.update(r["method_id"] for r in interface_methods)

    dead = []
    for c in candidates:
        if c["method_id"] in exempt:
            continue
        dead.append(
            {
                "method_id": c["method_id"],
                "name": c.get("name"),
                "signature": c.get("signature"),
                "reason": "no_incoming_calls_after_exemptions",
            }
        )

    return dead[:limit]
