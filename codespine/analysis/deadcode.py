from __future__ import annotations


def detect_dead_code(store, limit: int = 200) -> list[dict]:
    """Java-aware dead code detection with exemption passes."""
    candidates = store.query_records(
        """
        MATCH (m:Method)
        WHERE NOT EXISTS { MATCH (:Method)-[:CALLS]->(m) }
        RETURN m.id as method_id,
               m.name as name,
               m.signature as signature,
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
        if c.get("is_constructor"):
            exempt.add(c["method_id"])
        if c.get("is_test"):
            exempt.add(c["method_id"])
        if c.get("name") == "main" and "string[]" in sig:
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
        MATCH (:Class)-[:IMPLEMENTS]->(:Class)
        WITH 1 as x
        MATCH (m:Method)
        WHERE m.name IS NOT NULL
        RETURN DISTINCT m.id as method_id
        LIMIT 100000
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
