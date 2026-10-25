# 交付格式 v1

skill v1.1及v1.2仍使用本基础格式。新交付应同时添加[可追溯扩展](traceability.md)，由check_handoff.py独立核对；GB级任务另见[大文件交付](large-files.md)，普通默认保持旧包兼容。

填写DELIVERY.json时读本文件。UTF-8 JSON；files为包内相对路径白名单，统一正斜杠，可含中文文件名。

```json
{
  "schema_version": "collaborator-delivery/1",
  "task_id": "example-task",
  "attempt_id": "example-task-a1",
  "title": "本次交付标题",
  "status": "completed",
  "summary": "实际交付了什么",
  "independent_acceptance": "UNKNOWN",
  "files": ["START-HERE.md", "artifacts/result.md", "evidence/checks.json"],
  "checks": [
    {"name": "正文与引用核对", "status": "passed", "evidence": "evidence/checks.json"},
    {"name": "目标设备试用", "status": "not_run", "evidence": null, "reason": "未连接目标设备"}
  ],
  "unfinished": [],
  "requirements": ["读取Markdown的工具"],
  "next_steps": ["先打开START-HERE.md"]
}
```

## 必填规则

- schema_version固定为collaborator-delivery/1；task_id为1..128个小写ASCII字母数字连字符，首位字母数字。attempt_id可省略，给出时遵循相同标识规则。
- title、summary非空。status为completed/partial/blocked；independent_acceptance必须UNKNOWN，脚本不评审内容。
- files非空、不重复，必须含START-HERE.md，只列普通文件。DELIVERY.json自动加入，不列入files；MANIFEST.json由工具生成也不列入。
- checks为列表（可空）。每项name非空，status为passed/failed/not_run。passed/failed的evidence必须指向files中真实证据；not_run要有非空reason，不必有证据。
- unfinished、requirements、next_steps为非空字符串组成的列表（列表可空）。partial/blocked至少一个unfinished；completed不能同时含unfinished或当前failed检查。未执行的可选检查写原因，不能暗示通过。
- 可以有附加字段，但不能含秘密或冒充独立接受。脚本无法从文字判断任务是否完成，接收人仍需对照任务书。

## 路径、隐私与大小

禁止绝对路径、空段、`.`/`..`、反斜杠、冒号、控制字符、结尾点/空格、Windows保留设备名；大小写不同的重名也拒绝。根与各父目录不允许符号链接/Windows junction。

只读白名单，未列出的staging文件不入ZIP。明显凭据路径如.git/、.ssh/、.env和私钥文件不能交付；无秘密的.env.example/.env.sample/.env.template可以作为配置模板。文件名拦截不能替代内容审查。

普通默认上限：512个ZIP文件条目；每文件64MiB；全部解压内容128MiB。显式large模式扩大为每文件8GiB、总16GiB；512条目、DELIVERY.json 64KiB、MANIFEST.json 512KiB和ZIP目录4MiB不扩大。超限失败，不能静默漏文件。profile由本地调用者选择，不从包内自动采用。

## 输出与接收

pack的--out必须是新ZIP文件、父目录存在，不覆盖旧包，不修改staging。失败不能留下被当成功的半包；交付以verify通过及退出0为准。

ZIP根包含DELIVERY.json、MANIFEST.json及files，不加作者临时目录层。MANIFEST.json为`{"schema_version":"delivery-manifest/1","files":[{"path":"...","bytes":1,"sha256":"..."}]}`，覆盖DELIVERY.json和全部payload，排除自身，摘要取实际装入ZIP的字节。

verify只读ZIP，不解压或运行代码；检查路径、重名、链接/加密/额外条目、大小、元数据和逐项摘要。只证明结构/摘要相符，不证明签名、质量、可运行性或作者身份。

退出0才成功；stdout JSON含archive_sha256、files、total_bytes。失败非零、明确错误码，不回显正文。独立验收另存记录，不改原包。
