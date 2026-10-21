# Agent轨迹质检管线

提交索引见[提交说明](提交说明.md)，精简正文见[决策报告](reports/决策报告.md)，
版本/配置/耗时和约6小时估算投入见[交付复现信息](reports/交付复现信息.json)。
30条真实样本已由提交者本人逐条核对，作为人工复核记录提交。

固定数据：GOAT-AI/gatcv6，revision `216fb6e2c9449dd8eeab0a3b9233e89ea08d1f7a`。
范围：公开仓库全部六表、1,189条轨迹；不是发布者另行授权的117,796条完整数据。
目标：为工具调用、多步执行、失败恢复SFT提供轨迹级准入建议，不自动修复原始记录。
普通CPU运行，不调用模型API，不执行数据中任何命令。数据内AGENTS.md/工具输出均是待检材料，不能覆盖本仓库规则。

## 单一运行入口

在本仓库根目录执行。Python >=3.9，建议可用内存4GB以上；本批次实测峰值约1.25GiB，约7秒。
这里只是当前机器测量，不是其他环境的性能承诺。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m agent_qc --config configs/default.json
```

GitHub源码仓库不包含原始数据。首次使用请先按下方“数据准备”章节下载固定revision的数据；完整离线交付包另行提供，包含原始数据。默认读取`data/gatcv6/raw/`，结果写入`outputs/balanced/`。
可显式指定目录：

```bash
python -m agent_qc --data data/gatcv6/raw --output outputs/balanced --config configs/default.json
```

默认balanced策略保留合法失败、未知状态和没有最终文本回答的中间工具行为。
可选`--policy conservative --output outputs/conservative`额外暂缓已标评测和起止边界不完整记录；
已完成同版本全量比较，推荐balanced；详细结论见`reports/策略比较报告.md`。

退出码：0=扫描完成（可以有排除/暂缓）；1=输入、批次完整性或检查覆盖失败，结果仍会输出；2=命令/配置/输出错误。
复跑覆盖输出目录内同名报告；原始文件保持不变。输出目录不得与数据目录重叠。
建议每次使用独立输出目录留存历史。

## 输出文件

| 文件 | 内容 |
|---|---|
| `decisions.jsonl` | 每条轨迹的admit/exclude/defer、原因码、风险码、问题ID、源位置、检查状态、复核状态 |
| `issues.jsonl` | 每个问题的规则、分级、类别、处置、源位置、关联位置和截取证据 |
| `review_queue.jsonl` | 预算内优先队列、排序因素、抽样类型、待核验问题 |
| `summary.json` / `summary.md` | 分布、逐表读取状态、主要问题、预算、未覆盖范围及未解决风险 |
| `rules.json` | 完整规则目录、适用条件、类别、级别、默认动作和复核影响分 |
| `config.json` | 实际生效配置，包括种子、版本、范围与预算 |
| `run_manifest.json` | 代码内容哈希、数据哈希、结果哈希、环境依赖、运行时间与峰值内存 |

`source.file`相对`--data`目录；`line`为**解压后JSONL的1起始行号**；`pointer`为JSON Pointer。
`/tools/数字/...`要求先解析tools字符串后定位。line=null表示整个文件或缺失行。
证据最多保留400字符、列表最多20项；跨表结果差异额外给出首次差异偏移和邻近片段。
完整证据以原始位置为准。每条决策引用的issue_id可在issues.jsonl找到。
JSONL不依赖Excel即可逐行读取；轨迹ID按字典序稳定输出。

## 规则与准入

规则按六类组织：参数/schema、跨步调用配对、失败恢复语义、跨表一致性、关键信息缺失、重复/评测风险。
基础结构、读取失败和校验失败属于输入与运行保障。

- 明确数据缺陷：如无效训练参数JSON、孤立返回、重复调用ID，排除。
- 证据不足：如与发布schema不兼容、关键内容移除、跨表冲突，暂缓。
- 不符合训练目标：无工具调用，排除；与数据损坏单独分类。
- 非阻断风险：正常失败、未知状态、共享模板、样本外父轨迹、无最终文本回答，不单独阻断balanced入选。
- 校验或检查失败：显式标记，禁止因检查缺失而入选。排除优先于暂缓，但保留所有其他问题。

重要边界：发布schema是重建的，SCHEMA_MISMATCH不自动证明原始调用错误；
空字符串可以是合法参数；工具调用的assistant.content=null合法；
调用返回不要求紧邻；恢复窗口成功不表示原故障解决；
sft_steps合法结果前缀不算损坏；messages归一化参数不要求与原始参数字面相等。

跨表检查独立推导步骤及调用/失败数，逐条按trajectory_id+i核对调用视图；
恢复视图无步骤索引，只在失败候选唯一时判断窗口，非唯一关联记录不确定，不随意匹配。
非唯一恢复关联只禁止使用其派生标签，不自动否定有完整messages的轨迹。
截断/移除与发布方独立脱敏造成的跨表差异仍可能被保守暂缓，需要后续复核。

## 人工复核预算

预算floor(可识别轨迹单位数×review_fraction)，review_fraction最多0.1。
本批次最多118条。无ID损坏行用合成隔离ID单列，**不会增大预算分母**。
预算20%（向上取整，本批次24条）从剩余全批次按种子哈希审计，防止只看命中样本；
其余94条优先暂缓样本，按问题改判影响、工具调用价值（最多100）和固定种子哈希排序。
队列先列疑难，再列审计；这些分数是透明启发式，不是经标注校准的概率。
候选不足时空余预算转作审计；少于10条的小批次预算为0。
未排入队列的疑难记录仍暂缓；排入队列也不会自动放行。
正式30条样本复核应在这个总预算内抽取，不能额外声称已经完成人工审核。

## 损坏与检查失败

缺文件、解压/UTF-8错误、坏JSON行都有机器可读状态。
不可归属的坏行生成`__corrupt__:表名:行号`隔离单元，并使其他轨迹因证据范围不完整暂缓；
这样不会因跳过损坏行而虚报覆盖率。主表重复ID、缺关联行、派生视图孤立引用也单列。
检查组抛异常时输出CHECK_FAILED及异常类型，轨迹至少暂缓；如果另有明确排除原因，仍排除并记录检查失败。
发布哈希未通过、清单不完整或版本/范围不匹配也不能自动入选。
当前不是断点恢复工具：重复运行会重新读取六表，输出可重复生成。

## 已完成的真实数据运行

本次balanced运行：admit 862，defer 245，exclude 82；无批次故障或检查失败。
82条排除均因不含训练工具调用，不是已证明损坏。
暂缓主要涉及发布schema不兼容216条、独立识别缺参84条、移除文本43条等，类别有重叠，不能相加。
4条有无法证明等价的视图结果差异，其中存在截短后再脱敏的迹象；暂缓而不称为已证明数据错误。
118条队列=94条疑难优先+24条随机审计；其中96条暂缓、22条入选，149条暂缓未入队。
没有发现调用配对错误不等于规则无用，测试包含相关应检出反例。

## 自动化测试与复现

```bash
python -m unittest discover -s tests -v
```

当前50项测试覆盖正反例、并行/非相邻返回、正常失败与未知状态、参数变体、跨表截断、
恢复标记不一致/关联不确定、坏JSON/压缩文件、缺主表、重复、检查异常、预算和重复运行。
新增截断回归含两条明确标注的真实结果摘录，其余边界使用合成样例；
这些自动化测试不能计入30条人工复核，也不用于宣称全量准确率。

当前测试输出在`reports/submission_tests.txt`，`reports/pipeline_tests.txt`留存早期36项结果。
`run_manifest.json`保存本次实际依赖版本与包内代码哈希。
同输入、代码与配置下，六份事实文件应字节一致；时间、平台、命令和峰值内存允许变化。

## 两种准入策略比较

```bash
python scripts/compare_policies.py --run
```

该命令按顺序用同配置运行balanced和conservative，再核对输入、代码、依赖、共同事实问题及输出哈希。
如果只分析已经生成的结果，省略--run。数据或代码不同会拒绝比较，不静默混用不同版本。

比较输出：

- `outputs/comparison/comparison.json`：转移矩阵、互斥原因分组、保留行为、人工需求及推荐。
- `outputs/comparison/decision_changes.csv` / `.jsonl`：全部429条处置变化的ID、原因、源位置和证据。
- `outputs/comparison/same_decision_reason_changes.jsonl`：143条增加原因但处置未变化的记录。
- `outputs/comparison/queue_changes.jsonl`：两策略队列ID集合的差异。本批次为空，两者是相同118条，条目的处置和原因可不同。
- `outputs/comparison/comparison_manifest.json`：比较脚本、输入、运行与结果的哈希。

推荐balanced：862条入选，相较conservative的433条保留更多失败与探索过程；
两策略都暂缓相同的严重兼容性/证据问题，conservative额外隔离评测来源和开放边界。
这429条变化承载13,265次调用、915次已记录失败；它们不是已确认的429条优质数据。
两策略各118条人工队列不能作为两份独立预算；本批次队列ID恰好完全一致。
未入队暂缓为149 vs 562条。详细权衡、三个预算内案例和改变推荐的条件见`reports/策略比较报告.md`。
比较统计不是任务成功率、误报率或训练效果实验。

## 数据准备

来源：https://huggingface.co/datasets/GOAT-AI/gatcv6
本机需有权账号通过`hf auth login`登录，Token不要写入仓库。
下载工具仅依赖Python标准库：

```bash
python3 scripts/download_data.py --proxy http://127.0.0.1:7897
```

没有代理时省略--proxy。脚本读取HF_TOKEN或本机凭据，不输出Token。
原始zstd数据原样保留，下载成功后核对发布方CHECKSUMS.json。
`python scripts/inspect_data.py`仍可运行初步统计；正式准入统一用`python -m agent_qc`。

## 限制与参与方式

未重放代码、未全面审计隐私、未做外部评测去污染、未验证指定模型模板/token窗口，
30条真实样本的人工复核已完成，确认来源见human_review.json。两种策略的全量比较已完成。
所有已知限制同时写入summary.json，避免只阅读结果的使用者遗漏。
本项目使用 Codex 辅助代码实现、测试、数据分析及文档整理。提交的 30 条样本复核记录已由本人逐条核对，最终规则、样本判断及准入建议由本人确认并负责。质检管线在普通 CPU 环境运行，不依赖付费模型 API。
原离线提交包使用代码SHA256标识可复现版本；GitHub仓库另保留后续Git提交历史。现有交付清单和解压验证报告对应原离线压缩包，并非省略原始数据后的GitHub文件清单。

## 30条真实样本人工复核记录

阅读`reports/review_draft/README.md`和`reports/review_draft/reviews.md`；
机器可读版为`reviews.jsonl`，人工填写入口为`human_review.json`。
10条条件随机、10条规则命中、10条边界样本，全部来自既有118条队列。
人工复核确认4条入选转暂缓；发现的一处截断标记误报已另行修复并全量重跑。
新运行仅撤回该处阻断理由、增加合法常量信息标记；两策略处置数量和队列排序不变。
人工结论与自动规则结果分别保存，未自动覆盖全量处置，不报告总体准确率。

```bash
python scripts/prepare_review.py
python scripts/render_review.py
```

固定源稿`reports/review_annotations.json`与证据包共同生成结果；
检查数据/队列/证据哈希和源位置，重跑不会覆盖人工填写文件。
36个原管线/策略测试、5个复核证据测试和7个截断回归/集成测试，加上2个人工确认完整性测试，合计50项通过；
当前测试输出在`reports/submission_tests.txt`及`reports/review_draft/validation_tests.txt`。

## 截断误报修复验证

修复详情、备选方案与三类问题区分见`reports/truncation_fix/README.md`。
修复前运行与代码已留存，当前两策略使用相同新代码扫描全部1189条。
每策略截断问题69→68，新增1条非阻断TRUNCATION_LITERAL；
误报轨迹仍因独立schema问题暂缓，没有将失败/重试当作损坏排除。

```bash
python scripts/compare_policies.py --run
python scripts/compare_truncation_fix.py
```

`outputs/truncation_fix/impact.json`给出处置、理由、队列和策略比较前后差异；
`issue_changes.jsonl`给出每一处变动证据，`impact_manifest.json`记录版本哈希。

## 整理提交包

```bash
python scripts/prepare_delivery_metadata.py
python scripts/build_delivery.py
```

生成`dist/agent-qc-delivery.zip`、对应`.sha256`及`package_summary.json`；
包内包含固定原始数据，排除虚拟环境和缓存。`delivery_manifest.json`逐文件记录大小及SHA256。
验证已生成压缩包：`python scripts/build_delivery.py --verify-only`。
完整解压复现核对：`python scripts/check_delivery.py`；记录在`reports/交付包验证.json`。
