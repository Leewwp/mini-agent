"""工具描述文案的定稿测试(工单 05):中文为默认、英文为热备。

语义校准断言钉住两条最容易漂移的契约——「只有 run_tests 计入成功判定」与
「finish(success) 触发声明时复验」——中英文各说一遍,防止热备文案与中文
文案在演进中失配(降级只换配置的前提是语义等价)。
"""

import pytest

from mini_agent.errors import ConfigError
from mini_agent.tools import build_default_registry
from mini_agent.tools.base import ToolError
from mini_agent.tools.descriptions import (
    EN_DESCRIPTIONS,
    TOOL_DESC_LANGS,
    ZH_DESCRIPTIONS,
    descriptions_for,
)

SEVEN_TOOLS = {
    "list_files", "read_file", "write_file", "search",
    "run_command", "run_tests", "finish",
}

# 中英文都必须出现的契约锚点(小写化后子串匹配)
VERIFICATION_ANCHORS = {
    "run_command": ["不计入成功判定", "do NOT count as verification"],
    "run_tests": ["唯一计入成功判定", "the ONLY tool whose result counts as verification"],
}
FINISH_ANCHORS = {
    "zh": ["复验", "failed"],
    "en": ["re-run", "failed"],
}


def test_both_language_maps_cover_exactly_the_seven_tools():
    assert set(ZH_DESCRIPTIONS) == SEVEN_TOOLS
    assert set(EN_DESCRIPTIONS) == SEVEN_TOOLS
    for mapping in (ZH_DESCRIPTIONS, EN_DESCRIPTIONS):
        for name, text in mapping.items():
            assert text.strip(), f"{name} 描述为空"


def test_default_registry_uses_zh_map_verbatim(tmp_path):
    registry = build_default_registry(tmp_path)
    definitions = {d["function"]["name"]: d["function"]["description"] for d in registry.definitions()}
    assert definitions == ZH_DESCRIPTIONS


def test_en_override_swaps_descriptions_only(tmp_path):
    registry = build_default_registry(tmp_path, tool_descriptions=descriptions_for("en"))
    definitions = {d["function"]["name"]: d["function"] for d in registry.definitions()}
    for name, text in EN_DESCRIPTIONS.items():
        assert definitions[name]["description"] == text
    # 参数级描述仍是中文:热备只换工具级文案,行为与参数语义不动
    assert definitions["read_file"]["parameters"]["properties"]["offset"]["description"] == (
        "起始行号(从 1 开始,默认 1)"
    )
    # 覆盖是实例属性,类属性(中文默认)不被污染——新注册表仍是中文
    fresh = {
        d["function"]["name"]: d["function"]["description"] for d in build_default_registry(tmp_path).definitions()
    }
    assert fresh == ZH_DESCRIPTIONS


def test_verification_contract_present_in_both_languages():
    for name, anchors in VERIFICATION_ANCHORS.items():
        zh, en = anchors
        assert zh in ZH_DESCRIPTIONS[name], f"中文 {name} 描述缺少验证契约"
        assert en in EN_DESCRIPTIONS[name], f"英文 {name} 描述缺少验证契约"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_finish_contract_present_in_both_languages(lang):
    mapping = descriptions_for(lang)
    for anchor in FINISH_ANCHORS[lang]:
        assert anchor in mapping["finish"]


def test_descriptions_for_rejects_unknown_lang():
    with pytest.raises(ConfigError, match="MINI_AGENT_TOOL_DESC_LANG"):
        descriptions_for("jp")
    assert TOOL_DESC_LANGS == ("zh", "en")


def test_override_with_unknown_tool_name_raises(tmp_path):
    with pytest.raises(ToolError, match="未注册的工具"):
        build_default_registry(tmp_path, tool_descriptions={"no_such_tool": "x"})


# ---------- CLI 层:MINI_AGENT_TOOL_DESC_LANG ----------


def _args(tmp_path):
    from mini_agent.cli import build_arg_parser

    return build_arg_parser().parse_args(["--repo", str(tmp_path), "--task", "t"])


def test_tool_desc_lang_env_default_en_and_invalid(tmp_path):
    from mini_agent.cli import resolve_config

    env = {"MINI_AGENT_API_KEY": "k"}
    assert resolve_config(_args(tmp_path), env=env).tool_desc_lang == "zh"
    env_en = {**env, "MINI_AGENT_TOOL_DESC_LANG": "en"}
    assert resolve_config(_args(tmp_path), env=env_en).tool_desc_lang == "en"
    with pytest.raises(ConfigError, match="MINI_AGENT_TOOL_DESC_LANG"):
        resolve_config(_args(tmp_path), env={**env, "MINI_AGENT_TOOL_DESC_LANG": "jp"})


def test_cli_en_hot_backup_end_to_end(monkeypatch, tmp_path, capsys):
    """MINI_AGENT_TOOL_DESC_LANG=en:session_meta 记录,发往模型的工具定义整体换英文。"""
    import json

    from mini_agent.cli import main

    for name in (
        "MINI_AGENT_API_KEY", "MINI_AGENT_BASE_URL",
        "MINI_AGENT_MODEL", "MINI_AGENT_TOOL_DESC_LANG",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MINI_AGENT_TOOL_DESC_LANG", "en")
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            {"turns": [
                {"tool_calls": [{"name": "finish", "arguments": {"result": "failed", "summary": "热备演练"}}]},
            ]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = main(["--repo", str(repo), "--task", "热备演练", "--model", f"fake:{script}"])
    assert code == 1  # TESTS_FAILED(model_declared_failure)
    session = max((tmp_path / "mini-agent-runs").glob("*/session.jsonl"))
    events = [
        json.loads(line)
        for line in session.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta = next(e for e in events if e["type"] == "session_meta")
    assert meta["tool_desc_lang"] == "en"
    request = next(e for e in events if e["type"] == "llm_request")
    sent = {t["function"]["name"]: t["function"]["description"] for t in request["tools"]}
    assert sent == EN_DESCRIPTIONS  # 发往模型的定义整体为英文热备
