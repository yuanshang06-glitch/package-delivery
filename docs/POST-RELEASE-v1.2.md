# v1.2.0 发布后验证

2026-10-25，公开 MIT [v1.2.0 Release](https://github.com/yuanshang06-glitch/package-delivery/releases/tag/v1.2.0)已发布。PR #1 已合并；v1.1.0 标签及附件保留。

## 固定版本

- 标签提交：`e2577f8512a09f8e38674c6aa8056f13fc750af1`。
- ZIP：`package-delivery-v1.2.0.zip`，101,227 字节。
- SHA-256：`b3dcc562e9c87192611d80e5f2a542ecb8366afacbc624c94207e27954eaa0cf`。
- 从公开 Release 重新下载 ZIP 和 SHA256SUMS，摘要与发布原件一致；ZIP 内 42 个源码文件与固定标签逐字节一致。

## 实际安装与接手

- 从 GitHub 克隆固定 v1.2.0 标签，用自带安装器完成隔离目录 dry-run 和实际安装，9 个 skill 文件与发布清单一致。
- skills@1.7.4 实际从固定标签 URL 获取，完成项目级 copy 安装，9 个文件与清单一致。本次省略 `-g`，没有替换真实全局 skill。
- 隔离目录从原始 v1.1.0 升级到 v1.2.0：默认拒绝覆盖，dry-run 不改变旧安装，显式 `--replace` 后的备份与旧目录逐字节一致。
- 上传前从最终 ZIP 独立解压，在 Python3.11.9 与3.12.14 各运行组合套件（28 项新增及原52/31套件 wrapper），安装与最小示例接手通过。

结构化公开复验见[下载及安装记录](public-installation-v1.2-results.json)。

## 固定标签 CI

[v1.2.0 标签运行：六组全部成功](https://github.com/yuanshang06-glitch/package-delivery/actions/runs/40198265133)，实际 headSha 为上述标签提交。Ubuntu / Windows / macOS × Python3.11 /3.12，包含组合回归和安装预览；不是用旧开发分支的运行代替。详见[机器快照](tag-ci-v1.2-results.json)。

## 稳定性与排版

发布前额外实测8GiB单文件stored ZIP64、384MiB两种压缩方式，以及模拟写入失败、同路径冲突、篡改和超限拒绝。原始数据和范围见[本版验证](VALIDATION-v1.2.md)。Claude实际给出发布排版意见，采用首屏交付目录树、主安装路径、折叠备选方式和分层验证记录；技术复核独立进行。

16GiB整包边界、GB级deflated、原生IDE发现、真实全局npx安装、Node22.20最低版本和长期收益仍未完整验证。分卷、断点续传与自动上传尚未实现；单次压力测试不保证长期稳定性。标签与附件保留发布时快照，本记录是后续证据补充。
