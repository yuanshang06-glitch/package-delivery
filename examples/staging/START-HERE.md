# 最小示例：可接手文本成果

本示例交付一个UTF-8文本及检查入口。先读artifacts/result.txt；若要复核，在解压根目录运行 `python artifacts/check_result.py`。需要Python3.11+，无第三方库。

| 验收要求 | 产物 | 验证 | 缺口 |
| --- | --- | --- | --- |
| AC1：文本包含约定内容且接手命令能读取 | artifacts/result.txt、artifacts/check_result.py | V1，evidence/check.json | 无 |

验证在准备示例时实际运行，具体版本和结果见evidence/check.json。它仅检查示例文本，没有验证任何生产业务。当前无未完成项；接收方可独立复跑。摘要和最终ZIP检查在包外，不把自测视为独立接受。
