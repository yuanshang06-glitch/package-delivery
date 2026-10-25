# 最小示例

本目录的staging包含一个无敏感内容的文本成果、实际验证记录和v1.1元数据。它演示格式，不代表真实外包任务或生产环境验证。

在仓库根目录运行（输出文件必须不存在）：

```sh
python package-delivery/scripts/delivery.py pack --root examples/staging --out example-result.zip
python package-delivery/scripts/delivery.py verify example-result.zip
python package-delivery/scripts/check_handoff.py example-result.zip
```

此示例的证据和绑定摘要在准备候选版本时实际生成，环境记录为那次运行环境。复制示例用于新任务时必须替换实际产物、命令、环境、结果与摘要。
