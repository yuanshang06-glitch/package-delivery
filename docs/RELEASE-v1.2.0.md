# v1.2.0 · GB 级工程与资料交付

工程与资料包可以采用显式 `large` 模式，打包方和接收方使用同一资源预算。默认小包模式及原有格式保持兼容。

Python 3.11+ · 仅标准库 · MIT

## 这次新增

- **大文件模式**：`--profile large`，配置上限为单文件 8 GiB、总解压 16 GiB。
- **ZIP64 与分块处理**：支持单个条目和整个 ZIP 超过 4 GiB，分块计算 SHA-256。
- **压缩方式可选**：默认 `deflated`；已压缩的资源可用 `--compression stored`。
- **有界接收**：条目、小元数据与 ZIP 目录上限独立保留；接收方显式选择 `large`。

## 快速使用

```sh
python package-delivery/scripts/delivery.py pack --root staging --out engineering.zip --profile large --compression stored
python package-delivery/scripts/delivery.py verify engineering.zip --profile large
python package-delivery/scripts/check_handoff.py engineering.zip --profile large
```

默认模式仍为单文件 64 MiB、总解压 128 MiB。两种模式均最多 512 个条目；`DELIVERY.json` / `MANIFEST.json` / ZIP 目录分别最多 64 KiB / 512 KiB / 4 MiB。

## 安装与升级

```sh
npx skills@1.7.4 add https://github.com/yuanshang06-glitch/package-delivery/tree/v1.2.0/package-delivery --skill package-delivery -a codex -g --copy
```

这是第三方 Vercel skills CLI 提供的可选路径，需要 Node.js ≥ 22.20、npm/npx、Git 与网络。覆盖行为遵循该 CLI 自身规则。

需要离线安装或保留旧版时，下载并解压 Release ZIP，在仓库根目录运行自带安装器：

```sh
python scripts/install.py --client codex --replace --dry-run
python scripts/install.py --client codex --replace
```

自带安装器默认拒绝覆盖；`--replace` 升级时保留同级唯一备份，回执给出路径。首次安装可省略 `--replace`。

## 实际验证

| 范围 | 实际结果 |
| --- | --- |
| Windows · Python 3.11.9 | 原 52 项中 50 通过、2 项 symlink 权限跳过；原 31 项、新增 28 项通过 |
| Windows · Python 3.12.14 | 同样各自运行，结果同上 |
| 远端 CI | Ubuntu / Windows / macOS × Python 3.11 / 3.12 六组通过；最终发布提交另在运行记录中绑定 |
| 实写大文件 | 4097 MiB 与 **8 GiB 单文件**，stored ZIP64、大小与摘要、完整性及接手检查通过 |
| 8 GiB 本机资源 | 打包含内部校验 70.047 秒，子进程峰值 working set 约 19.42 MiB；单次观测，非性能承诺 |
| 384 MiB 混合内容 | stored / deflated 打包及重新读取校验通过 |
| 失败与冲突 | 模拟磁盘满、KeyboardInterrupt 清理半包；两个 CLI 同路径仅一个成功，结果可验证；中段篡改拒绝；8 GiB + 1 字节源文件提前拒绝 |

大文件均使用合成内容。8 GiB 实测为 Windows / Python 3.11 的 stored 路径；16 GiB 总量上限没有完整压力实测，GB 级 deflated 尚未实测。系统文件缓存不计入子进程 working set。强杀、断电与恶意并发修改不在这些模拟检查的保证内。

完整记录见 [v1.2 验证](VALIDATION-v1.2.md)、[8 GiB 原始报告](large-file-stress-8g-v1.2.json)和[稳定性复查](stability-v1.2-results.json)。

## 兼容与边界

数据契约继续使用 `collaborator-delivery/1`、`delivery-manifest/1` 和 `collaborator-traceability/1`。v1.1 包照常校验；旧 v1 包可做完整性检查，没有可追溯扩展时接手检查会拒绝。

本版提供本地单个 ZIP 的交付与核对。没有分卷、断点续传、自动上传或内容级秘密扫描。摘要不认证作者，结构检查不判断产物质量或日志真实性；这些仍需接收方独立验收。原生 IDE 自动发现和长期生产力收益尚未验证。

## 下载

附件为 `package-delivery-v1.2.0.zip` 与 `SHA256SUMS.txt`。从可信来源获取摘要后再核对下载；公开的 v1.1.0 标签与附件保留。
