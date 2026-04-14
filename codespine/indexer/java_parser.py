from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Query

JAVA_LANGUAGE = Language(tsjava.language())
PARSER = Parser(JAVA_LANGUAGE)

# Pre-compiled regexes used in the hot path (_normalize_java_bytes is called
# once per method/class body for digest computation).
_RE_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RE_LINE_COMMENT = re.compile(r"//.*?$", re.MULTILINE)
_RE_WHITESPACE = re.compile(r"\s+")


@dataclass
class ParsedMethod:
    name: str
    signature: str
    return_type: str
    modifiers: list[str]
    annotations: list[str]
    parameter_types: list[str]
    line: int
    col: int
    body_hash: str
    calls: list["ParsedCall"] = field(default_factory=list)
    local_types: dict[str, str] = field(default_factory=dict)
    # DI metadata — set for @Provides/@Bean methods.
    provides_type: str | None = None  # return type when the method is a DI provider


@dataclass
class ParsedCall:
    name: str
    receiver: str | None
    arg_count: int
    line: int
    col: int


@dataclass
class ParsedField:
    name: str
    type_name: str
    line: int
    col: int
    # DI metadata — set when the field has an injection annotation.
    injection_annotation: str | None = None  # e.g. "Inject", "Autowired"
    qualifier: str | None = None             # value of @Named/@Qualifier if present


@dataclass
class ParsedClass:
    name: str
    package: str
    fqcn: str
    line: int
    col: int
    modifiers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    extends: str | None = None
    field_types: dict[str, str] = field(default_factory=dict)
    body_hash: str = ""
    methods: list[ParsedMethod] = field(default_factory=list)
    fields: list[ParsedField] = field(default_factory=list)


@dataclass
class ParsedFile:
    package: str
    imports: list[str]
    classes: list[ParsedClass]


def _text(node) -> str:
    return node.text.decode("utf-8")


def _captures(query: Query, node) -> list[tuple]:
    """Compatibility wrapper for tree-sitter Python bindings."""
    if hasattr(query, "captures"):
        return query.captures(node)

    from tree_sitter import QueryCursor

    raw = None
    # API shape A: QueryCursor(query).captures(node)
    try:
        cursor = QueryCursor(query)
        if hasattr(cursor, "captures"):
            raw = cursor.captures(node)
    except TypeError:
        raw = None

    # API shape B/C: QueryCursor().captures(...)
    if raw is None:
        cursor = QueryCursor()
        for call in (
            lambda: cursor.captures(query, node),
            lambda: cursor.captures(node, query),
        ):
            try:
                raw = call()
                break
            except TypeError:
                continue

    if raw is None:
        return []

    # Newer bindings may return {capture_name: [nodes...]}
    if isinstance(raw, dict):
        out: list[tuple] = []
        for tag, nodes in raw.items():
            for n in nodes:
                out.append((n, tag))
        return out

    out: list[tuple] = []
    for item in raw:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        n, t = item[0], item[1]
        if isinstance(t, int):
            tag = None
            for attr in ("capture_name_for_id", "capture_name"):
                if hasattr(query, attr):
                    try:
                        tag = getattr(query, attr)(t)
                        break
                    except Exception:
                        pass
            out.append((n, tag if tag else str(t)))
        else:
            out.append((n, t))
    return out


def _hash_node(node) -> str:
    return hashlib.sha1(_normalize_java_bytes(node.text).encode("utf-8")).hexdigest()


def _normalize_java_bytes(source: bytes) -> str:
    text = source.decode("utf-8", errors="ignore")
    text = _RE_BLOCK_COMMENT.sub("", text)
    text = _RE_LINE_COMMENT.sub("", text)
    text = _RE_WHITESPACE.sub(" ", text).strip()
    return text


def _node_type_name(node) -> str:
    if node is None:
        return ""
    if node.type in {"type_identifier", "identifier", "scoped_identifier"}:
        return _text(node)
    for child in node.named_children:
        name = _node_type_name(child)
        if name:
            return name
    return _text(node).strip()


def _extract_modifiers_and_annotations(node) -> tuple[list[str], list[str]]:
    modifiers: list[str] = []
    annotations: list[str] = []
    for child in node.children:
        if child.type != "modifiers":
            continue
        for m in child.named_children:
            m_text = _text(m).strip()
            if not m_text:
                continue
            if m.type == "annotation" or m_text.startswith("@"):
                annotations.append(m_text.lstrip("@"))
            else:
                modifiers.append(m_text)
    return modifiers, annotations


def _arg_count(args_text: str) -> int:
    args = args_text.strip()
    if not args.startswith("(") or not args.endswith(")"):
        return 0
    inner = args[1:-1].strip()
    if not inner:
        return 0
    return inner.count(",") + 1


def _extract_local_types(method_node) -> dict[str, str]:
    q = Query(
        JAVA_LANGUAGE,
        """
        (local_variable_declaration
          type: (_) @type
          declarator: (variable_declarator name: (identifier) @name))
        """,
    )
    captures = _captures(q, method_node)
    locals_map: dict[str, str] = {}
    current_type = None
    for node, tag in captures:
        if tag == "type":
            current_type = _node_type_name(node)
        elif tag == "name" and current_type:
            locals_map[_text(node)] = current_type
    return locals_map


_DI_FIELD_ANNOTATIONS = frozenset({
    "Inject", "Autowired", "Resource", "Value", "Qualifier", "Named",
    "javax.inject.Inject", "jakarta.inject.Inject",
})

_DI_QUALIFIER_ANNOTATIONS = frozenset({"Named", "Qualifier", "javax.inject.Named", "jakarta.inject.Named"})

_DI_PROVIDER_ANNOTATIONS = frozenset({"Provides", "Bean"})


def _extract_field_types(class_node) -> tuple[dict[str, str], list[ParsedField]]:
    """Extract field names→types and DI annotations from a class node.

    Single O(N) pass: iterate field_declaration nodes directly, extract the
    type + annotations once per declaration, then yield all variable declarators.
    This avoids the previous O(N²) approach that scanned all field_decl_nodes
    for each variable name to find its enclosing declaration.
    """
    field_decl_q = Query(
        JAVA_LANGUAGE,
        "(field_declaration) @field_decl",
    )

    field_map: dict[str, str] = {}
    field_list: list[ParsedField] = []

    for fd_node, _ in _captures(field_decl_q, class_node):
        # Resolve annotations once per field_declaration (shared by all declarators).
        _, fd_annotations = _extract_modifiers_and_annotations(fd_node)
        injection_annotation: str | None = None
        qualifier: str | None = None
        for ann in fd_annotations:
            ann_simple = ann.split(".")[-1]
            if ann_simple in _DI_FIELD_ANNOTATIONS:
                injection_annotation = ann_simple
            if ann_simple in _DI_QUALIFIER_ANNOTATIONS:
                qualifier = ann_simple

        # Resolve the declared type (shared across all declarators in this declaration).
        type_node = fd_node.child_by_field_name("type")
        type_name = _node_type_name(type_node) if type_node else None
        if not type_name:
            continue

        # Each variable_declarator within this declaration shares the same type + annotations.
        for child in fd_node.named_children:
            if child.type != "variable_declarator":
                continue
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node)
            field_map[name] = type_name
            field_list.append(ParsedField(
                name=name,
                type_name=type_name,
                line=name_node.start_point[0] + 1,
                col=name_node.start_point[1] + 1,
                injection_annotation=injection_annotation,
                qualifier=qualifier,
            ))
    return field_map, field_list


def _extract_parameter_types(params_node) -> list[str]:
    if params_node is None:
        return []
    types: list[str] = []
    for child in params_node.named_children:
        if child.type in {"formal_parameter", "spread_parameter"}:
            tnode = child.child_by_field_name("type")
            types.append(_node_type_name(tnode))
        elif child.type == "receiver_parameter":
            # Keep receiver as pseudo-type to stabilize signature arity
            tnode = child.child_by_field_name("type")
            types.append(_node_type_name(tnode))
    return [t for t in types if t]


def _extract_inheritance(class_node) -> tuple[str | None, list[str]]:
    extends_name = None
    interfaces: list[str] = []

    super_node = class_node.child_by_field_name("superclass")
    if super_node is not None:
        extends_name = _node_type_name(super_node)

    iface_node = class_node.child_by_field_name("interfaces")
    if iface_node is not None:
        type_query = Query(
            JAVA_LANGUAGE,
            """
            [
              (type_identifier) @t
              (scoped_type_identifier) @t
              (generic_type) @t
              (scoped_identifier) @t
            ]
            """,
        )
        interfaces = [_node_type_name(n) for n, tag in _captures(type_query, iface_node) if tag == "t"]

    # Fallback for grammar variants where interfaces are not exposed as a field.
    if not interfaces:
        for child in class_node.named_children:
            if child.type in {"super_interfaces", "type_list"}:
                type_query = Query(
                    JAVA_LANGUAGE,
                    """
                    [
                      (type_identifier) @t
                      (scoped_type_identifier) @t
                      (generic_type) @t
                      (scoped_identifier) @t
                    ]
                    """,
                )
                interfaces.extend([_node_type_name(n) for n, tag in _captures(type_query, child) if tag == "t"])

    return extends_name, interfaces


def parse_java_source(source: bytes) -> ParsedFile:
    tree = PARSER.parse(source)
    root = tree.root_node

    pkg_query = Query(JAVA_LANGUAGE, "(package_declaration (scoped_identifier) @pkg)")
    import_query = Query(JAVA_LANGUAGE, "(import_declaration (scoped_identifier) @imp)")
    cls_query = Query(
        JAVA_LANGUAGE,
        """
        (class_declaration
          name: (identifier) @class_name
          body: (class_body) @class_body) @class_decl
        """,
    )

    package_name = ""
    imports: list[str] = []

    for node, tag in _captures(pkg_query, root):
        if tag == "pkg":
            package_name = _text(node)
            break

    for node, tag in _captures(import_query, root):
        if tag == "imp":
            imports.append(_text(node))

    classes: list[ParsedClass] = []
    method_query = Query(
        JAVA_LANGUAGE,
        """
        (method_declaration
          type: (_) @return_type
          name: (identifier) @method_name
          parameters: (formal_parameters) @params
          body: (block) @body) @method_decl
        """,
    )
    ctor_query = Query(
        JAVA_LANGUAGE,
        """
        (constructor_declaration
          name: (identifier) @method_name
          parameters: (formal_parameters) @params
          body: (constructor_body) @body) @method_decl
        """,
    )
    call_query = Query(
        JAVA_LANGUAGE,
        """
        (method_invocation
          name: (identifier) @call_name
          arguments: (argument_list) @call_args) @call_inv
        """,
    )

    for node, tag in _captures(cls_query, root):
        if tag != "class_decl":
            continue

        cls_name_node = node.child_by_field_name("name")
        if cls_name_node is None:
            continue
        cls_name = _text(cls_name_node)
        fqcn = f"{package_name}.{cls_name}" if package_name else cls_name
        cls_modifiers, cls_annotations = _extract_modifiers_and_annotations(node)
        extends_name, interface_names = _extract_inheritance(node)
        ft_map, ft_list = _extract_field_types(node)
        parsed_class = ParsedClass(
            name=cls_name,
            package=package_name,
            fqcn=fqcn,
            line=node.start_point[0] + 1,
            col=node.start_point[1] + 1,
            modifiers=cls_modifiers,
            annotations=cls_annotations,
            extends=extends_name,
            interfaces=interface_names,
            field_types=ft_map,
            body_hash=_hash_node(node),
            fields=ft_list,
        )

        method_nodes = [n for n, t in _captures(method_query, node) if t == "method_decl"]
        method_nodes.extend([n for n, t in _captures(ctor_query, node) if t == "method_decl"])

        for m_node in method_nodes:
            m_name_node = m_node.child_by_field_name("name")
            m_type_node = m_node.child_by_field_name("type")
            m_params_node = m_node.child_by_field_name("parameters")
            if m_name_node is None:
                continue

            method_name = _text(m_name_node)
            return_type = _text(m_type_node) if m_type_node else cls_name
            param_types = _extract_parameter_types(m_params_node)
            signature = f"{method_name}({','.join(param_types)})"
            modifiers, annotations = _extract_modifiers_and_annotations(m_node)
            # Mark DI provider methods so di_resolver can link them to consumers.
            provides_type: str | None = None
            for ann in annotations:
                ann_simple = ann.split(".")[-1]
                if ann_simple in _DI_PROVIDER_ANNOTATIONS and return_type and return_type not in {"void", "Void"}:
                    provides_type = return_type
                    break
            parsed_method = ParsedMethod(
                name=method_name,
                signature=signature,
                return_type=return_type,
                modifiers=modifiers,
                annotations=annotations,
                parameter_types=param_types,
                line=m_node.start_point[0] + 1,
                col=m_node.start_point[1] + 1,
                body_hash=_hash_node(m_node),
                local_types=_extract_local_types(m_node),
                provides_type=provides_type,
            )

            body_node = m_node.child_by_field_name("body")
            if body_node is not None:
                grouped: dict[object, dict[str, str]] = {}
                for c_node, c_tag in _captures(call_query, body_node):
                    inv_node = c_node if c_tag == "call_inv" else c_node.parent
                    grouped.setdefault(inv_node, {})[c_tag] = _text(c_node)
                for inv_node, capture_map in grouped.items():
                    name_text = capture_map.get("call_name")
                    if not name_text:
                        continue
                    receiver_node = inv_node.child_by_field_name("object")
                    receiver = _text(receiver_node) if receiver_node is not None else None
                    args = capture_map.get("call_args", "()")
                    parsed_method.calls.append(
                        ParsedCall(
                            name=name_text,
                            receiver=receiver,
                            arg_count=_arg_count(args),
                            line=inv_node.start_point[0] + 1,
                            col=inv_node.start_point[1] + 1,
                        )
                    )

            parsed_class.methods.append(parsed_method)

        classes.append(parsed_class)

    return ParsedFile(package=package_name, imports=imports, classes=classes)
