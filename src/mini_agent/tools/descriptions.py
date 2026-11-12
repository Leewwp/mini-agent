"""七工具描述文案的单一事实源(工单 05 定稿)。

- ``ZH_DESCRIPTIONS``:默认中文描述——对齐项目需求「中文输入中文输出」的演示要求;
- ``EN_DESCRIPTIONS``:英文热备文案,语义与中文逐一校准。若真实模型实测中文
  工具描述导致 function calling 不稳,设 ``MINI_AGENT_TOOL_DESC_LANG=en``
  即整体切换——只换配置,不重写代码。

各工具类不再内联描述字符串,统一从本模块取,保证两种语言永远只差一个
配置项(降级预案,见 DESIGN.md §2)。

四条契约在每个描述里都得到重申(系统提示词再总述一遍):
1. finish 的用法(success 触发声明时复验;failed 是合法的如实失败);
2. 先读后写(覆盖已存在文件前必须读过;新建不受限);
3. 验证优先(改完必须跑测试,不要凭假设宣称完成);
4. 只有 run_tests 计入成功判定(run_command 的结果不算)。
"""

from __future__ import annotations

from mini_agent.errors import ConfigError

ZH_DESCRIPTIONS: dict[str, str] = {
    "list_files": (
        "列出仓库的文件树(树状文本),用于定位代码。自动忽略 .git/.venv/"
        "node_modules/__pycache__/mini-agent-runs 目录;深度最多 8 层、条数最多 500,"
        "超限部分不展开。可用 path 参数指定子目录。"
    ),
    "read_file": (
        "读取 UTF-8 文本文件内容,支持 offset(起始行号,从 1 计)与 limit(行数)"
        "分段读取。单次读取上限 2000 行或 100KB,超限会被拒,请用 offset/limit 分段;"
        "二进制文件与密钥文件(.env、*.pem、id_rsa* 等)拒绝读取。"
        "注意:write_file 覆盖已存在文件前,必须先用本工具读过该文件(先读后写)。"
    ),
    "write_file": (
        "整文件写入(不是局部 patch,必须提供完整文件内容)。新建文件允许,"
        "父目录不存在时自动创建;覆盖已存在的文件前,必须在本次运行中先用 "
        "read_file 读取过该文件(先读后写,防止盲覆盖);路径限制在仓库内,"
        "越界拒绝;密钥文件(.env 等)因拒读而无法被覆盖。"
    ),
    "search": (
        "在仓库文本文件中按 Python 正则表达式搜索,返回「相对路径:行号: 行内容」"
        "格式的匹配列表,最多 50 条。自动忽略 .git 等目录与密钥文件"
        "(.env、*.pem、id_rsa*)、二进制文件。"
    ),
    "run_command": (
        "在仓库目录内执行一条辅助命令,用于运行脚本或查看行为(如 python/pytest/"
        "node/npm/npx/go/cargo/make 等 allowlist 内的解释器/构建/测试类命令);"
        "危险命令(rm -rf、sudo、git push、fork 炸弹等)一律拒绝;"
        "默认 120 秒超时,超时击杀整个进程组。"
        "注意:本工具的结果不计入成功判定,验证一律使用 run_tests。"
    ),
    "run_tests": (
        "运行用户通过 --test-cmd 预配置的测试命令,返回退出码与 stdout/stderr。"
        "这是唯一计入成功判定的验证工具(run_command 的结果不算);"
        "finish(result=\"success\") 时系统还会现场重跑本命令复验,"
        "所以宣称完成前应先确认本工具的最新结果是通过。"
    ),
    "finish": (
        "声明任务结束,这是终止运行的唯一方式。result='success' 表示任务已完成——"
        "系统会当场重跑一次预配置的测试命令复验,复验通过才判定成功,未通过会拒绝"
        "本次声明并要求继续修复;result='failed' 表示确实无法完成,如实申报失败"
        "(这是合法结果)。summary 用中文简要说明做了什么/为何失败。"
    ),
}

EN_DESCRIPTIONS: dict[str, str] = {
    "list_files": (
        "List repository files as a tree. Ignores `.git`, `.venv`, `node_modules`, "
        "`__pycache__`, and `mini-agent-runs`. Depth and entry count are capped."
    ),
    "read_file": (
        "Read a UTF-8 text file from the repository. Supports offset/limit. "
        "Rejects binary files, secret files (`.env`, `*.pem`, `id_rsa*`), and reads "
        "over 2000 lines or 100KB per call. Note: read a file with this tool before "
        "overwriting it with write_file (read-before-write)."
    ),
    "write_file": (
        "Write a complete file inside the repository (full content, not a patch). "
        "You must read an existing file before overwriting it. Creating new files is "
        "allowed; parent directories are created automatically. Paths are confined to "
        "the repository; secret files (`.env` etc.) cannot be overwritten because "
        "they are never readable."
    ),
    "search": (
        "Search repository text with a Python regular expression. Returns up to 50 "
        "matches with file paths and line numbers. Skips `.git` and secret files."
    ),
    "run_command": (
        "Run an allowed command inside the repository (e.g. `python`, `pytest`, "
        "`node`, `npm`, `npx`, `go`, `cargo`, `make`). Use it to inspect behavior or "
        "run scripts. Results do NOT count as verification — use run_tests for that."
    ),
    "run_tests": (
        "Run the repository's configured test command. Returns exit code, stdout, and "
        "stderr. This is the ONLY tool whose result counts as verification for "
        "success; finish(result=\"success\") additionally re-runs this command "
        "on the spot, so only claim success after the latest run passed."
    ),
    "finish": (
        "Declare the task finished. `result` must be `success` or `failed`. On "
        "success, the test command is re-run; success is accepted only if that "
        "verification passes. `failed` is a legal, honest outcome. Include a short "
        "summary."
    ),
}

_DESCRIPTIONS_BY_LANG: dict[str, dict[str, str]] = {
    "zh": ZH_DESCRIPTIONS,
    "en": EN_DESCRIPTIONS,
}

TOOL_DESC_LANGS = tuple(_DESCRIPTIONS_BY_LANG)


def descriptions_for(lang: str) -> dict[str, str]:
    """按语言取描述映射;未知语言是配置错误,直接抛 ConfigError。"""
    try:
        return dict(_DESCRIPTIONS_BY_LANG[lang])
    except KeyError:
        raise ConfigError(
            f"MINI_AGENT_TOOL_DESC_LANG 必须是 {' 或 '.join(TOOL_DESC_LANGS)},收到:{lang!r}"
        ) from None
