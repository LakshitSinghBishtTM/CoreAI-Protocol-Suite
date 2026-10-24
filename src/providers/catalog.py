"""Catalog of provider labs the key manager knows about.

``kind`` says how CaiPS talks to the lab: a native SDK, an OpenAI-compatible
endpoint (``compat``), a self-hosted server (``local``) or a user-defined one
(``custom``). ``base_url`` and ``model`` are defaults a key entry can override;
check them against the lab's current docs, they change.
"""

from __future__ import annotations

from typing import NamedTuple, Optional


class Lab(NamedTuple):
    id: str
    label: str
    region: str  # "us" | "cn" | "self" | "any"
    kind: str  # native | compat | local | custom
    env_var: str = ""
    base_url: str = ""
    model: str = ""


CATALOG: list[Lab] = [
    # -- United States
    Lab("openai", "OpenAI", "us", "native", "OPENAI_API_KEY"),
    Lab("anthropic", "Anthropic (Claude)", "us", "native", "ANTHROPIC_API_KEY"),
    Lab("gemini", "Google (Gemini)", "us", "native", "GEMINI_API_KEY"),
    Lab("grok", "xAI (Grok)", "us", "native", "GROK_API_KEY"),
    Lab(
        "meta",
        "Meta (Llama API)",
        "us",
        "compat",
        "META_API_KEY",
        "https://api.llama.com/compat/v1",
        "Llama-4-Maverick-17B-128E-Instruct-FP8",
    ),
    Lab(
        "nvidia",
        "NVIDIA (NIM)",
        "us",
        "compat",
        "NVIDIA_API_KEY",
        "https://integrate.api.nvidia.com/v1",
        "meta/llama-3.3-70b-instruct",
    ),
    Lab(
        "perplexity",
        "Perplexity",
        "us",
        "compat",
        "PERPLEXITY_API_KEY",
        "https://api.perplexity.ai",
        "sonar",
    ),
    # -- China
    Lab("deepseek", "DeepSeek", "cn", "native", "DEEPSEEK_API_KEY"),
    Lab(
        "qwen",
        "Alibaba (Qwen)",
        "cn",
        "compat",
        "QWEN_API_KEY",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
    ),
    Lab(
        "moonshot",
        "Moonshot AI (Kimi)",
        "cn",
        "compat",
        "MOONSHOT_API_KEY",
        "https://api.moonshot.ai/v1",
        "kimi-k2-0905-preview",
    ),
    Lab(
        "zhipu",
        "Zhipu AI (GLM / Z.ai)",
        "cn",
        "compat",
        "ZHIPU_API_KEY",
        "https://api.z.ai/api/paas/v4",
        "glm-4.6",
    ),
    Lab(
        "minimax",
        "MiniMax",
        "cn",
        "compat",
        "MINIMAX_API_KEY",
        "https://api.minimax.io/v1",
        "MiniMax-M2",
    ),
    Lab(
        "doubao",
        "ByteDance (Doubao / Ark)",
        "cn",
        "compat",
        "DOUBAO_API_KEY",
        "https://ark.cn-beijing.volces.com/api/v3",
        "",
    ),  # model = your endpoint id
    Lab(
        "baidu",
        "Baidu (ERNIE / Qianfan)",
        "cn",
        "compat",
        "BAIDU_API_KEY",
        "https://qianfan.baidubce.com/v2",
        "ernie-4.5-turbo-128k",
    ),
    Lab(
        "tencent",
        "Tencent (Hunyuan)",
        "cn",
        "compat",
        "TENCENT_API_KEY",
        "https://api.hunyuan.cloud.tencent.com/v1",
        "hunyuan-turbos-latest",
    ),
    Lab(
        "stepfun",
        "StepFun (Step)",
        "cn",
        "compat",
        "STEPFUN_API_KEY",
        "https://api.stepfun.com/v1",
        "step-2-16k",
    ),
    Lab(
        "yi",
        "01.AI (Yi)",
        "cn",
        "compat",
        "YI_API_KEY",
        "https://api.lingyiwanwu.com/v1",
        "yi-lightning",
    ),
    Lab(
        "baichuan",
        "Baichuan",
        "cn",
        "compat",
        "BAICHUAN_API_KEY",
        "https://api.baichuan-ai.com/v1",
        "Baichuan4-Turbo",
    ),
    # -- yours
    Lab("local", "Local model server", "self", "local"),
    Lab("custom", "Custom OpenAI-compatible", "any", "custom"),
]

BY_ID: dict[str, Lab] = {lab.id: lab for lab in CATALOG}
EXTRA_ENV: dict[str, str] = {
    lab.id: lab.env_var for lab in CATALOG if lab.kind == "compat" and lab.env_var
}


def lab(provider: str) -> Optional[Lab]:
    return BY_ID.get(provider)
