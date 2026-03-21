"""Noise filters for call graph generation."""

NOISE_METHOD_NAMES = {
    # Object / lang
    "print", "println", "printf",
    "hashCode", "equals", "toString", "getClass",
    "notify", "notifyAll", "wait", "clone", "finalize",
    "compareTo",
    # Collections / streams
    "isEmpty", "size", "length",
    "stream", "parallelStream", "forEach", "map", "filter", "collect",
    "orElse", "orElseGet", "orElseThrow", "of", "ofNullable",
    "add", "append", "remove", "contains", "put", "putAll",
    "addAll", "removeAll", "containsAll", "containsKey", "containsValue",
    "entrySet", "keySet", "values", "iterator", "hasNext", "next",
    # Logging
    "log", "debug", "info", "warn", "error", "trace",
    # Common short helpers that create false-positive edges
    "get", "set", "apply", "accept", "test",
    "run", "call", "execute", "invoke",
    "build", "create", "from", "parse", "format",
    "close", "open", "init", "start", "stop", "reset",
    "read", "write", "flush", "clear",
    "supply", "compose", "andThen",
    # Builder / accessor patterns
    "builder", "toBuilder", "newBuilder",
    "getName", "setName", "getValue", "setValue",
    "getId", "setId", "getType", "setType",
}

# Minimum method name length for fuzzy (global name+arity) fallback.
# Shorter names are too ambiguous for unresolved-receiver resolution.
MIN_FUZZY_NAME_LEN = 4
