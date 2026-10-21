# 质检运行汇总

轨迹单位：1189；策略：balanced。

| 处理建议 | 数量 |
|---|---:|
| admit | 862 |
| exclude | 82 |
| defer | 245 |

人工队列 118/118；未入队暂缓 149。

| 规则 | 命中轨迹 | 问题条数 |
|---|---:|---:|
| VIEW_TRUNCATED | 745 | 5593 |
| TEMPLATE_RISK | 674 | 674 |
| RECOVERY_HEURISTIC | 452 | 452 |
| PUBLISHER_FLAG | 448 | 448 |
| EVAL_RISK | 422 | 422 |
| OPEN_BOUNDARY | 321 | 321 |
| STATUS_UNKNOWN | 301 | 301 |
| SCHEMA_MISMATCH | 216 | 3648 |
| PARAMS_INCOMPLETE | 84 | 1055 |
| NO_TOOL_TARGET | 82 | 82 |
| PARENT_OUTSIDE | 43 | 43 |
| WITHHELD | 43 | 95 |
| RECOVERY_UNCERTAIN | 36 | 194 |
| CROSS_VALUE | 4 | 4 |
| RECORDED_TRUNCATION | 2 | 69 |

未覆盖范围及未解决风险：

- 未执行原始命令、未重放代码、未核验视觉产物；不保证任务成功或代码正确。
- 工具schema由发布者重建；兼容性检查不证明原始工具语义正确。
- 无指定目标模型/模板/tokenizer，不检查实际token窗口或训练框架损失掩码。
- 没有目标评测集；is_eval与共享开头只作为风险线索，未完成外部去污染或模糊去重。
- 不是全面隐私审计；保留原始脱敏占位符，不尝试恢复隐藏内容。
- error_recovery无步骤索引；非唯一匹配明确为不确定，窗口成功不是因果恢复。
- 本轮整轨迹准入，无自动裁剪、补写参数或人工判决；队列是待复核建议。
- 发布方独立脱敏可能改变视图前缀；无法证明等价的跨表差异暂缓，可能产生误报。
