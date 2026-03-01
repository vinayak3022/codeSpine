import pytest

pytest.importorskip("tree_sitter_java")

from codespine.indexer.java_parser import parse_java_source


SOURCE = b"""
package com.example;

public class Service {
    public void processPayment() {
        helper();
    }

    private void helper() {}
}
"""


def test_parse_java_methods_and_calls():
    parsed = parse_java_source(SOURCE)
    assert parsed.package == "com.example"
    assert len(parsed.classes) == 1
    methods = {m.name: m for m in parsed.classes[0].methods}
    assert "processPayment" in methods
    assert "helper" in methods
    assert "helper" in methods["processPayment"].calls
