from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass
class SymbolRef:
    symbol_id: str
    method_id: str
    class_id: str
    file_id: str


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha1(payload).hexdigest()


def file_id(project_id: str, rel_path: str) -> str:
    return hashlib.sha1(f"{project_id}:{rel_path}".encode("utf-8")).hexdigest()


def class_id(fqcn: str, scope: str | None = None) -> str:
    key = f"{scope}::{fqcn}" if scope else fqcn
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def method_id(fqcn: str, signature: str, scope: str | None = None) -> str:
    key = f"{scope}::{fqcn}#{signature}" if scope else f"{fqcn}#{signature}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def symbol_id(kind: str, fqname: str, scope: str | None = None) -> str:
    key = f"{kind}:{scope}:{fqname}" if scope else f"{kind}:{fqname}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()
