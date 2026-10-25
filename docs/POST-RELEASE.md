# v1.1.0 发布后验证

日期：2026-10-25。仓库已公开，许可MIT，[v1.1.0 Release](https://github.com/yuanshang06-glitch/package-delivery/releases/tag/v1.1.0)已发布ZIP和SHA256SUMS。

## 固定版本与下载

- 标签提交：`29ae7ebddc3f9496dc69948e448231c40ceafca1`。
- Release ZIP：65,936字节，SHA-256 `4dba99fb700476e8231e80dd07490c5725b7f4196731e02f8643e05cffe60160`。
- 从公开Release重新下载ZIP和SHA256SUMS，摘要一致；包内29个文件与固定标签Git源码逐字节一致。
- 通过Git克隆v1.1.0后，用自带安装器在隔离目录完成dry-run及真实安装；8个skill文件与清单一致。
- skills@1.7.4实际从GitHub的固定标签URL获取并完成项目级copy安装，8个skill文件与清单一致。测试省略-g，没有修改真实全局技能目录。

结构化记录见[public-installation-results.json](public-installation-results.json)。

## 远端CI

[固定v1.1.0源码复验：六组全部成功](https://github.com/yuanshang06-glitch/package-delivery/actions/runs/40177030693)。当前CI配置通过workflow_dispatch的source_ref=v1.1.0检出固定标签，六组日志均记录上述原始提交；每组实际执行52项基础测试和31项新测试。

| 系统 | Python 3.11 | Python 3.12 |
| --- | --- | --- |
| Ubuntu | 成功 | 成功 |
| Windows | 成功 | 成功 |
| macOS | 成功 | 成功 |

初次push的Windows/Linux任务通过，macOS任务因系统临时目录使用`/var`链接别名而触发安装器祖先链接拒绝。main中的CI配置改用runner真实临时目录，再对原始v1.1.0复跑；没有放宽安装器规则，也没有移动公开标签或替换Release附件。

工作流提交与实际被测提交分别记录在[post-release-results.json](post-release-results.json)，不能把最初失败的run描述成通过。

macOS本地手动跑安装测试时，若默认TMPDIR包含系统链接别名，请将TMPDIR指向可写的真实目录，例如先用`python -c 'import tempfile,os; print(os.path.realpath(tempfile.gettempdir()))'`查出路径，再为测试进程设置该真实目录。保留默认链接拒绝规则。

## 仍未验证

原生客户端自动发现、npx全局安装对真实用户目录的影响、Node22.20最低版本和长期生产力收益仍未实测。CI通过不能证明交付日志真实性、产物质量、作者身份或没有内容级秘密；其余大小及本地信任边界沿用README。

标签里的文档保留发布前快照；main中的本记录补充发布后结果。下一次产品修改使用新版本，不改写v1.1.0。
