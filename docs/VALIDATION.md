# v1.1.0 本地验证记录

日期：2026-10-25。阶段：发布前候选，不等同于公开发布或生产环境验收。

## 实际检查

DeepSeek实现，Codex独立核对冻结提交。首轮发现错误枚举类型诊断、需求产物未被关联验证覆盖两项，返修后增加5项回归检查。

| 测试套件 | Python 3.11.9 / Windows | Python 3.12.14 / Windows |
| --- | --- | --- |
| v1打包/完整性助手52项 | 50通过，2普通文件symlink权限跳过 | 50通过，2普通文件symlink权限跳过 |
| v1.1可追溯检查与安装器31项 | 31通过，无跳过 | 31通过，无跳过 |

以上是相同套件在两个版本各跑一次，不是166个不同测试。实际目录junction拒绝检查已运行，不用跳过项冒充通过。原v1打包助手SHA-256保持 `8dc23f1ca3d11e98d2d04a3506b4e040deae38ea5a34c6db5955cdf4af7d7569`。

原始相对命令、退出码与计数保存在[validation-results.json](validation-results.json)。复跑：

```sh
python -B tests/test_delivery.py --tool package-delivery/scripts/delivery.py
python -B tests/test_release.py --checker package-delivery/scripts/check_handoff.py --installer scripts/install.py
```

独立黑盒另外核对：结构可追溯往返、不执行声明命令、11种错误/过期/无关证据拒绝、继承与partial/not_run诚实接收、安装dry-run零写入、首次安装、拒绝覆盖、逐字节备份、手动回滚与源文件篡改拒绝。它们是有限的合成一致性检查，不证明日志真实性。

## 快捷安装验证

- `skills@1.7.4`在本地仓库发现且实际按`--skill package-delivery -a codex --copy -y`安装到隔离项目，成功。没有使用全局`-g`测试，未改真实技能目录。
- 自带安装器在临时目录真实安装、升级及手动回滚；dry-run没有创建目标父目录，默认覆盖被拒绝，旧目录字节完整保留。
- 快捷安装所用Node为24.18.0、npm11.16.0；CLI声明最低Node22.20。最低Node本身尚未实测。
- 可运行示例实际生成检查证据，并通过pack、verify与可追溯检查。

## 尚未验证

公开仓库和固定标签下载（待所有者预览后发布）、远端GitHub Actions、Linux/macOS实际运行、Codex/Claude对本候选的原生自动发现、长期生产力收益。CI文件已配置三系统/两Python矩阵，但配置存在不代表已运行通过。

## 信任边界

摘要和结构检查不认证作者，也不证明命令真的执行；不运行交付包内代码，不做内容级秘密扫描。安装清单校验本地源一致性，不能代替可信下载渠道和签名。已知尺寸上限、可信本地操作者和备份恢复边界见README。
