from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Query

JAVA_LANGUAGE = Language(tsjava.language())
PARSER = Parser(JAVA_LANGUAGE)


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


@dataclass
class ParsedCall:
    name: str
    receiver: str | None
    arg_count: int
    line: int
    col: int


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


@dataclass
class ParsedFile:
    package: str
    imports: list[str]
    classes: list[ParsedClass]


def _text(node) -> str:
    return node.text.decode("utf-8")


def _hash_node(node) -> str:
    return hashlib.sha1(_normalize_java_bytes(node.text).encode("utf-8")).hexdigest()


def _normalize_java_bytes(source: bytes) -> str:
    text = source.decode("utf-8", errors="ignore")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()
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
    captures = q.captures(method_node)
    locals_map: dict[str, str] = {}
    current_type = None
    for node, tag in captures:
        if tag == "type":
            current_type = _node_type_name(node)
        elif tag == "name" and current_type:
            locals_map[_text(node)] = current_type
    return locals_map


def _extract_field_types(class_node) -> dict[str, str]:
    q = Query(
        JAVA_LANGUAGE,
        """
        (field_declaration
          type: (_) @type
          declarator: (variable_declarator name: (identifier) @name))
        """,
    )
    captures = q.captures(class_node)
    field_map: dict[str, str] = {}
    current_type = None
    for node, tag in captures:
        if tag == "type":
            current_type = _node_type_name(node)
        elif tag == "name" and current_type:
            field_map[_text(node)] = current_type
    return field_map


def _extract_parameter_types(params_node) -> list[str]:
    if params_node is None:
        return []
    q = Query(
        JAVA_LANGUAGE,
        """
        [
          (formal_parameter type: (_) @ptype)
          (spread_parameter type: (_) @ptype)
        ]
        """,
    )
    return [_node_type_name(node) for node, tag in q.captures(params_node) if tag == "ptype"]


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
        interfaces = [_node_type_name(n) for n, tag in type_query.captures(iface_node) if tag == "t"]

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
                interfaces.extend([_node_type_name(n) for n, tag in type_query.captures(child) if tag == "t"])

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

    for node, tag in pkg_query.captures(root):
        if tag == "pkg":
            package_name = _text(node)
            break

    for node, tag in import_query.captures(root):
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

    for node, tag in cls_query.captures(root):
        if tag != "class_decl":
            continue

        cls_name_node = node.child_by_field_name("name")
        if cls_name_node is None:
            continue
        cls_name = _text(cls_name_node)
        fqcn = f"{package_name}.{cls_name}" if package_name else cls_name
        cls_modifiers, cls_annotations = _extract_modifiers_and_annotations(node)
        extends_name, interface_names = _extract_inheritance(node)
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
            field_types=_extract_field_types(node),
            body_hash=_hash_node(node),
        )

        method_nodes = [n for n, t in method_query.captures(node) if t == "method_decl"]
        method_nodes.extend([n for n, t in ctor_query.captures(node) if t == "method_decl"])

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
            )

            body_node = m_node.child_by_field_name("body")
            if body_node is not None:
                grouped: dict[object, dict[str, str]] = {}
                for c_node, c_tag in call_query.captures(body_node):
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
