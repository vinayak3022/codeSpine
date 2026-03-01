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


def class_id(fqcn: str) -> str:
    return hashlib.sha1(fqcn.encode("utf-8")).hexdigest()


def method_id(fqcn: str, signature: str) -> str:
    return hashlib.sha1(f"{fqcn}#{signature}".encode("utf-8")).hexdigest()


def symbol_id(kind: str, fqname: str) -> str:
    return hashlib.sha1(f"{kind}:{fqname}".encode("utf-8")).hexdigest()
