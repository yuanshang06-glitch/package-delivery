# 可追溯扩展 collaborator-traceability/1

在基础 DELIVERY.json 添加 traceability。它保持 collaborator-delivery/1 包格式不变，由同目录可信 check_handoff.py 在基础完整性验证通过后核对。此检查不执行命令，也不判定需求描述、日志和证据类型的真实性。

## 最小结构

```json
{
  "traceability": {
    "schema_version": "collaborator-traceability/1",
    "criteria": [
      {"id": "AC1", "requirement": "说明与实际产物一致且可以接手", "status": "met", "artifacts": ["artifacts/result.txt"], "verification_ids": ["V1"], "reason": null}
    ],
    "verifications": [
      {
        "id": "V1",
        "kind": "unit",
        "execution": "current",
        "status": "passed",
        "command": ["python", "check_result.py"],
        "cwd": ".",
        "environment": {"os": "实际系统", "python": "实际版本"},
        "exit_code": 0,
        "evidence": "evidence/check.txt",
        "subjects": [{"path": "artifacts/result.txt", "sha256": "替换为该产物实际64位小写十六进制摘要"}],
        "reason": null,
        "source": null
      }
    ],
    "revision": null
  }
}
```

此片段需合并到基础元数据；示意命令、环境和摘要必须替换成实际记录，不能原样宣称实测。可运行样例见仓库examples/。

## 逐条需求

- criteria和verifications均非空；各自id唯一，1..64 ASCII字母数字、连字符或下划线，首位字母数字。
- status为met/unmet/not_run；未满足或未运行时reason非空。
- met至少关联一个已声明产物与一个验证，关联验证必须全部passed。
- met列出的每个产物，必须被所关联验证的subjects并集覆盖；验证了其他文件不能支持本条产物已验证。
- 基础status=completed时所有criteria须met；存在必要未满足项时用partial/blocked，并填写基础unfinished。
- artifacts和证据引用必须在基础files白名单内，不引用DELIVERY.json或MANIFEST.json。不要只列容易通过的要求而遗漏原任务书要求；这一点由接收方对照任务书评审。

## 验证记录

- kind为static/unit/integration/simulated/live/manual，仅描述证据类型，不代表天然强弱顺序。
- execution为current/inherited/not_run。current是本轮实际执行；inherited是引用已有记录，须source指向包内声明的来源证据，reason说明来源与限制；不得改写为重跑。
- current/inherited必须有非空command argv、environment字符串键值、整数exit_code、声明的evidence及至少一个subjects。passed要求exit_code=0；failed要求非0；bool不能当退出码。
- subjects逐项绑定包内产物path及实际sha256，必须与清单一致。检查后若修改产物，需要对修改后的版本重新验证。
- cwd为`.`或可移植的包内相对目录；不能记录只有作者电脑存在的绝对路径。
- not_run要求status=not_run、exit_code=null、非空reason；command可null或待执行argv，environment可空，evidence/source可null，subjects可空。不把未运行写成通过。
- 人工核对可用kind=manual，command描述实际操作步骤，exit_code记录人工核对结果约定；明确它是人工记录，不能伪装为进程输出。

## 返修

首包revision=null。返修示例：

```json
{
  "previous_archive_sha256": "上一包实际64位小写十六进制摘要",
  "changes": [
    {"criterion_id": "AC1", "description": "依据反馈补齐可编辑源文件", "verification_ids": ["V2"]}
  ]
}
```

changes非空，引用当前已有criterion和verification。返修关联验证应为本次执行current；确实未运行可引用明确的not_run，不能只引用inherited。脚本只核对上一包摘要格式，不自动获取或认证上一包。

## 给人的对照表

在START-HERE.md呈现：

| 要求 | 实际产物 | 验证记录 | 未满足/未运行原因 |
| --- | --- | --- | --- |
| AC1：具体可核对要求 | 相对路径 | V1及证据路径 | 无或实际原因 |

脚本结构校验通过、worker completed和接收方ACCEPTED是不同结论。独立验收另存，绑定原始ZIP摘要，不改worker包内UNKNOWN。
