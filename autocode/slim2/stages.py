"""精简版 v2 五段对话的阶段配置（唯一真源）。

模板只回答"说什么"，本模块回答"怎么调"：温度、输出预算、超时、重试。
分两处放的代价是可能不同步，所以有测试强制校验：

- `template` 必须在 `prompts.TEMPLATE_NAMES` 里；
- `version` 必须与模板首行 `<!-- prompt: <name> vN ... -->` 一致。

温度口径（依据见 docs/prompt-standards.md）：
- 抽取/复核 0.0：同输入要同输出，可复现、可审计；
- 生成 0.3：需要输入多样性，但格式与分层要稳，不能用 0.7 那种发散温度；
- 复核阶段一律 0.0，且与生成阶段不同上下文。

`max_tokens` 按最坏情况估：D2/D3 可能一次处理 40 条，4096 会被截断，
截断的 JSON 会被判结构不合格，白白多花一次调用。

`model` 留空表示跟随客户端全局配置。若要把复核段单独换成推理模型（降低同模型自我偏好），
只改这里，模板不用动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from ..prompts import loader


@dataclass(frozen=True)
class StageSpec:
    code: str          # D1~D5，日志与前端展示用
    template: str      # prompts 模板名
    version: str       # 与模板首行版本号一致
    temperature: float
    max_tokens: int
    timeout_s: int
    retries: int       # 结构不合格/网络失败时的额外尝试次数
    stage: str         # 落 llm_calls.stage 的口径
    model: str = ""    # 空 = 用客户端全局配置；复核段可单独换模型以降自我偏好
    #: 推理预算：`default` 跟随客户端；复核段可设 `disabled`。
    #: 实测 D5 复核 4 条用例时，模型把 16384 token 全花在 reasoning 上，
    #: 输出仍被截断（finish_reason=length），整次提交白跑。复核是逐条对账，
    #: 不是难题，关掉思考既省 token 又不会把预算烧空。
    thinking: str = "default"


#: 切题只做"分块 + 打标"，没有推理链可走；开着 thinking 会把 8k 输出预算全烧光
#: （实测 completion_tokens 正好撞到上限、正文为空）。关掉后既稳又快。
SEGMENT = StageSpec(
    "D1", loader.SLIM_SEGMENT, "v5", 0.0, 8192, 120, 1, "slim_segment", thinking="disabled"
)
#: 输入上限放宽到单条 20 KB / 整批 60 KB 后，D2 的输出（它要把输入原样写进 JSON）
#: 也需要更大的预算。deepseek-flash 带 reasoning：实测 32768 会在无契约的首次生成里
#: 被思考过程吃光（题 28：数据生成阶段直接报 max_tokens 截断），65536 是 API 接受的档位。
DATASET = StageSpec("D2", loader.SLIM_DATASET, "v8", 0.3, 131072, 300, 1, "slim_dataset")
#: 复核要读整批输入 + 逐条重算，同样带推理；16384 在 60 KB 批次上偏紧。
DATASET_REVIEW = StageSpec(
    "D3", loader.SLIM_DATASET_REVIEW, "v2", 0.0, 65536, 300, 1, "slim_dataset_review"
)
#: D1R 是第一步，不是"校验工序"：先写代码答案，再用它跑出每条输入的期望输出。
#: 温度 0.0：同一道题重复跑应得到同一份代码，便于复现与审计。
REFERENCE = StageSpec(
    "D1R", loader.SLIM_REFERENCE, "v3", 0.0, 8192, 180, 1, "slim_reference"
)
#: D0 题面补全：只补输入输出格式与范围。温度 0.2——需要一点措辞自由度，
#: 但绝不能改题意，低温度下最稳。
REPAIR = StageSpec("D0", loader.SLIM_REPAIR, "v1", 0.2, 16384, 120, 1, "slim_repair")
ADVERSARIAL = StageSpec("D4", loader.SLIM_ADVERSARIAL, "v4", 0.3, 32768, 300, 1, "slim_adversarial")
ADVERSARIAL_REVIEW = StageSpec(
    "D5", loader.SLIM_ADVERSARIAL_REVIEW, "v1", 0.0, 4096, 120, 1, "slim_adversarial_review",
    thinking="disabled",
)

#: 图片导入不是"第 D 段"：它发生在切题之前，把图片转成文本，按题不计数。
#: 所以不放进 `STAGES`（那个表的每项都计入单题预算），只作为独立阶段导出。
#: 温度 0.0：同一张图重复转写应当得到同一份文本，否则切出来的题面每次都不一样。
VISION = StageSpec(
    "D0", loader.SLIM_VISION, "v1", 0.0, 8192, 180, 1, "slim_vision", thinking="disabled"
)

#: 执行规格（D1N）：只在 D2 判"题面没有标准输入格式/答案顺序不唯一"时补跑一次。
STAGES: Dict[str, StageSpec] = {
    "segment": SEGMENT,
    "dataset": DATASET,
    "dataset_review": DATASET_REVIEW,
    "reference": REFERENCE,
    "adversarial": ADVERSARIAL,
    "adversarial_review": ADVERSARIAL_REVIEW,
}

#: 单题预算：五段合计的调用次数上限（含各段 1 次重试）。
MAX_CALLS_PER_PROBLEM = sum(item.retries + 1 for item in STAGES.values())

#: 生成段必须过复核才入库；复核段不参与"再生成"计数。
GENERATION_STAGES: Tuple[StageSpec, ...] = (DATASET, ADVERSARIAL)
REVIEW_STAGES: Tuple[StageSpec, ...] = (DATASET_REVIEW, ADVERSARIAL_REVIEW)
