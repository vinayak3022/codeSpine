from types import SimpleNamespace

from codespine.indexer.call_resolver import resolve_calls


def test_resolver_prefers_receiver_type_and_arity():
    method_catalog = {
        "src": {"name": "entry", "param_count": 0, "class_fqcn": "com.example.Service", "signature": "entry()"},
        "m1": {"name": "run", "param_count": 0, "class_fqcn": "com.example.Service", "signature": "run()"},
        "m2": {"name": "run", "param_count": 1, "class_fqcn": "com.example.Service", "signature": "run(String)"},
        "m3": {"name": "save", "param_count": 0, "class_fqcn": "com.example.Repo", "signature": "save()"},
    }
    calls = {
        "src": [
            SimpleNamespace(name="run", receiver="this", arg_count=0),
            SimpleNamespace(name="save", receiver="repo", arg_count=0),
        ]
    }
    method_context = {
        "src": {
            "class_fqcn": "com.example.Service",
            "local_types": {"repo": "Repo"},
            "field_types": {},
        }
    }
    class_catalog = {"Service": ["com.example.Service"], "Repo": ["com.example.Repo"]}

    out = resolve_calls(method_catalog, calls, method_context, class_catalog)
    assert ("src", "m1", 1.0, "receiver_this_exact") in out
    assert ("src", "m3", 0.8, "receiver_method_match") in out
