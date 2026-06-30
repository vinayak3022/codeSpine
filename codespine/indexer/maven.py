from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET


_PROP_RE = re.compile(r"\$\{([^}]+)\}")


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _child_text(node, name: str) -> str:
    if node is None:
        return ""
    for child in list(node):
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _resolve_props(raw: str, props: dict[str, str]) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    for _ in range(6):
        updated = _PROP_RE.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        if updated == value:
            break
        value = updated
    return value.strip()


def load_maven_project_metadata(project_path: str) -> dict:
    pom_path = os.path.join(os.path.abspath(project_path), "pom.xml")
    if not os.path.isfile(pom_path):
        return {}

    try:
        root = ET.parse(pom_path).getroot()
    except ET.ParseError:
        return {}

    parent = None
    props: dict[str, str] = {}
    for child in list(root):
        tag = _local_name(child.tag)
        if tag == "parent":
            parent = child
        elif tag == "properties":
            for prop in list(child):
                props[_local_name(prop.tag)] = (prop.text or "").strip()

    parent_group = _child_text(parent, "groupId")
    parent_version = _child_text(parent, "version")
    group_id = _child_text(root, "groupId") or parent_group
    artifact_id = _child_text(root, "artifactId")
    version = _child_text(root, "version") or parent_version
    packaging = _child_text(root, "packaging") or "jar"

    props.setdefault("project.groupId", group_id)
    props.setdefault("project.artifactId", artifact_id)
    props.setdefault("project.version", version)
    props.setdefault("groupId", group_id)
    props.setdefault("artifactId", artifact_id)
    props.setdefault("version", version)

    group_id = _resolve_props(group_id, props)
    artifact_id = _resolve_props(artifact_id, props)
    version = _resolve_props(version, props)
    packaging = _resolve_props(packaging, props) or "jar"

    dependencies: list[dict] = []
    for child in list(root):
        if _local_name(child.tag) != "dependencies":
            continue
        for dep in list(child):
            if _local_name(dep.tag) != "dependency":
                continue
            dep_group = _resolve_props(_child_text(dep, "groupId"), props)
            dep_artifact = _resolve_props(_child_text(dep, "artifactId"), props)
            dep_version = _resolve_props(_child_text(dep, "version"), props)
            dep_scope = _resolve_props(_child_text(dep, "scope"), props) or "compile"
            optional = _resolve_props(_child_text(dep, "optional"), props).lower() == "true"
            if not dep_group or not dep_artifact or dep_scope == "test":
                continue
            dependencies.append(
                {
                    "group_id": dep_group,
                    "artifact_id": dep_artifact,
                    "version": dep_version,
                    "scope": dep_scope,
                    "optional": optional,
                    "coord": f"{dep_group}:{dep_artifact}",
                }
            )

    modules: list[str] = []
    for child in list(root):
        if _local_name(child.tag) != "modules":
            continue
        for mod in list(child):
            if _local_name(mod.tag) == "module" and (mod.text or "").strip():
                modules.append((mod.text or "").strip())

    return {
        "pom_path": pom_path,
        "group_id": group_id,
        "artifact_id": artifact_id,
        "version": version,
        "packaging": packaging,
        "coord": f"{group_id}:{artifact_id}" if group_id and artifact_id else "",
        "modules": modules,
        "dependencies": dependencies,
    }
