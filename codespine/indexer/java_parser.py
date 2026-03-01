from __future__ import annotations

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
    line: int
    col: int
    calls: list[str] = field(default_factory=list)


@dataclass
class ParsedClass:
    name: str
    package: str
    fqcn: str
    line: int
    col: int
    modifiers: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    extends: str | None = None
    methods: list[ParsedMethod] = field(default_factory=list)


@dataclass
class ParsedFile:
    package: str
    imports: list[str]
    classes: list[ParsedClass]


def _text(node) -> str:
    return node.text.decode("utf-8")


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
    call_query = Query(JAVA_LANGUAGE, "(method_invocation name: (identifier) @call)")

    for node, tag in cls_query.captures(root):
        if tag != "class_decl":
            continue

        cls_name_node = node.child_by_field_name("name")
        if cls_name_node is None:
            continue
        cls_name = _text(cls_name_node)
        fqcn = f"{package_name}.{cls_name}" if package_name else cls_name
        parsed_class = ParsedClass(
            name=cls_name,
            package=package_name,
            fqcn=fqcn,
            line=node.start_point[0] + 1,
            col=node.start_point[1] + 1,
        )

        for m_node, m_tag in method_query.captures(node):
            if m_tag != "method_decl":
                continue

            m_name_node = m_node.child_by_field_name("name")
            m_type_node = m_node.child_by_field_name("type")
            m_params_node = m_node.child_by_field_name("parameters")
            if m_name_node is None:
                continue

            method_name = _text(m_name_node)
            return_type = _text(m_type_node) if m_type_node else "void"
            signature = f"{method_name}{_text(m_params_node) if m_params_node else '()'}"
            parsed_method = ParsedMethod(
                name=method_name,
                signature=signature,
                return_type=return_type,
                modifiers=[],
                line=m_node.start_point[0] + 1,
                col=m_node.start_point[1] + 1,
            )

            body_node = m_node.child_by_field_name("body")
            if body_node is not None:
                for c_node, c_tag in call_query.captures(body_node):
                    if c_tag == "call":
                        parsed_method.calls.append(_text(c_node))

            parsed_class.methods.append(parsed_method)

        classes.append(parsed_class)

    return ParsedFile(package=package_name, imports=imports, classes=classes)
