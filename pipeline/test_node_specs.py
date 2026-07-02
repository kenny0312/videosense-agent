"""M1(DAG→loop):node_specs 的结构化 parameters + function-declaration 构建。

纯离线:不 import vertexai,不碰运行时。验证每个工具都有合法的 OpenAPI 参数 schema,
且 build_function_declarations() 产出可直接喂给 Gemini FunctionDeclaration 的形状。
"""
from pipeline import node_specs as ns


def test_every_spec_has_object_parameters():
    for tool, spec in ns.SPECS.items():
        p = spec.parameters
        assert isinstance(p, dict), tool
        assert p.get("type") == "object", tool
        assert isinstance(p.get("properties"), dict) and p["properties"], tool
        assert isinstance(p.get("required", []), list), tool


def test_required_is_subset_of_properties():
    for tool, spec in ns.SPECS.items():
        props = set(spec.parameters["properties"])
        req = set(spec.parameters.get("required", []))
        assert req <= props, f"{tool}: required {req - props} 不在 properties 里"


def test_build_declarations_one_per_tool():
    decls = ns.build_function_declarations()
    assert {d["name"] for d in decls} == set(ns.SPECS)
    for d in decls:
        assert d["name"] and d["description"]
        assert d["parameters"]["type"] == "object"
        # description 由 planner_desc 压平而来,不该含换行
        assert "\n" not in d["description"]


def test_build_declarations_accepts_subset():
    decls = ns.build_function_declarations({"sql_query": ns.SPECS["sql_query"]})
    assert len(decls) == 1 and decls[0]["name"] == "sql_query"


def test_required_inputs_helper():
    assert ns.required_inputs("sql_query") == ("sql",)
    assert set(ns.required_inputs("merge_asof")) == {"left_on", "right_on", "tolerance_ms"}
    assert ns.required_inputs("show_video") == ()        # 上游可替代,无必填


def test_spot_check_key_schemas():
    assert ns.SPECS["sql_query"].parameters["required"] == ["sql"]
    assert set(ns.SPECS["ols_regress"].parameters["required"]) == {"y", "x"}
    assert ns.SPECS["ols_regress"].parameters["properties"]["x"]["type"] == "array"
    assert ns.SPECS["plot"].parameters["properties"]["kind"]["enum"] == ["scatter", "line"]
    assert "load_artifact" not in ns.SPECS              # 记忆简化:值复用工具已下线


def test_existing_helpers_unchanged():
    # 加 parameters 不应破坏既有用途
    assert ns.needs_sandbox("sql_query") is False
    assert ns.needs_sandbox("ols_regress") is True
    assert "纯 Python" in ns.codegen_hint("plot")
