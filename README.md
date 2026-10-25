# package-delivery

把任务成果整理成一个接收方能直接接手、可以独立核对的交付包。

`v1.2.0` · Python 3.11+ · 仅标准库 · MIT

给合作者或 AI Agent 派任务后，交回实际产物、接手说明、验证证据、需求与产物对照表，以及明确列出的未完成项。

本项目是一个本地 Agent Skill，附带两个检查脚本和一个安装器。没有后台服务，不需要 API Key 或额外 Python 依赖；任务本身用什么模型由你决定。它检查交付包的结构、摘要和引用是否一致，**不判断成果质量，也不证明作者身份或日志真实性**。详见[范围与限制](#范围与限制)。

## 交回什么

```text
result.zip
├── START-HERE.md     接手入口、最少操作步骤、已知短板
├── DELIVERY.json     完成状态、文件白名单、需求—证据对照
├── MANIFEST.json     每个入包文件的长度与 SHA-256
├── artifacts/        源文件、可编辑成果、必要资源
└── evidence/         检查记录，区分本次执行、继承、未运行
```

## 快速上手

### 1. 安装

```sh
npx skills@1.7.4 add https://github.com/yuanshang06-glitch/package-delivery/tree/v1.2.0/package-delivery --skill package-delivery -a codex -g --copy
```

这条命令固定了 CLI 与 skill 的版本，安装到 Codex 全局范围并复制文件。它依赖第三方 [Vercel skills CLI](https://github.com/vercel-labs/skills)，需要 Node.js ≥ 22.20、npm/npx、Git 和网络。该 CLI 不是本项目的运行时依赖，覆盖已有安装时按该 CLI 自身的行为处理。

**没有 Node，或需要保留旧版本时**，在已解压的仓库根目录使用自带安装器（Windows 可用 `py -3.11` 代替 `python`）：

```sh
python scripts/install.py --client codex --dry-run   # 预览写入位置
python scripts/install.py --client codex
```

自带安装器的行为如下：

- 默认安装到 `${CODEX_HOME}/skills/package-delivery`；未设置 `CODEX_HOME` 时，安装到 `~/.codex/skills/package-delivery`。
- 只复制 skill 本身，默认拒绝覆盖已有安装。
- 从 v1.1.0 升级时加 `--replace`，旧目录会备份为同级的 `package-delivery.backup-…`，确切路径写在安装回执里。具体见[更新与回滚](#更新与回滚)。

<details>
<summary>其他安装方式</summary>

**Git 固定标签：**

```sh
git clone --depth 1 --branch v1.2.0 https://github.com/yuanshang06-glitch/package-delivery.git package-delivery-v1.2.0
python package-delivery-v1.2.0/scripts/install.py --client codex
```

**Codex `$skill-installer`：**

```text
使用 $skill-installer 从 https://github.com/yuanshang06-glitch/package-delivery/tree/v1.2.0/package-delivery 安装 package-delivery。
```

**Claude Code：** 使用 `--client claude`，安装到 `~/.claude/skills/package-delivery`。目前只验证了目录安装，尚未验证实际加载行为。

**其他工具：** 可以直接读取 `package-delivery/SKILL.md`，不承诺能被原生发现。

几种方式任选其一，不要重复安装。

</details>

### 2. 派任务

填好[任务单模板](package-delivery/references/task-brief.md)，把 skill 所在位置告诉对方，再附上一句：

> 请按 package-delivery/SKILL.md 完成交付。逐条对应验收要求、产物和实际验证；未执行的检查要写明原因。交回完整 ZIP，最终的 ZIP 摘要放在包外回执中。自测不能代替接收方验收。

### 3. 打包与接收

```sh
python package-delivery/scripts/delivery.py pack --root staging --out result.zip
python package-delivery/scripts/delivery.py verify result.zip
python package-delivery/scripts/check_handoff.py result.zip
```

`check_handoff.py` 检查需求—产物—证据的引用关系、成果摘要绑定和返修关联，不执行包内任何命令。接收方应先用自己信任的脚本核对包外摘要，通过后在新目录中接手，再按任务书独立验收。

### 4. GB 级工程与资料

体积较大时，打包方和接收方都需要**显式**加上 `--profile large`。工具不会根据包内声明自动放宽上限。

```sh
python package-delivery/scripts/delivery.py pack --root staging --out engineering.zip --profile large --compression stored
python package-delivery/scripts/delivery.py verify engineering.zip --profile large
python package-delivery/scripts/check_handoff.py engineering.zip --profile large
```

| 配置上限 | 默认 | `large` |
| --- | --- | --- |
| 单文件 | 64 MiB | 8 GiB |
| 总解压大小 | 128 MiB | 16 GiB |
| 条目数 | 512 | 512 |
| DELIVERY.json / MANIFEST.json / ZIP 目录 | 64 KiB / 512 KiB / 4 MiB | 同左 |

表格列出配置上限，已验证的尺寸与环境单独记录。large 模式支持 `deflated` 和 `stored` 两种压缩方式，采用分块读写和 ZIP64。磁盘预算和压缩方式的选择见[大文件交付](package-delivery/references/large-files.md)，实测尺寸与环境见 [v1.2 验证记录](docs/VALIDATION-v1.2.md)。

## 环境要求

| 用途 | 要求 |
| --- | --- |
| 阅读 skill、手动整理文件夹 | 能读取 Markdown 和文件的合作者或 Agent |
| 打包、校验、安装与测试 | Python 3.11+，全部使用标准库，无需 pip |
| npx 安装（可选） | Node.js ≥ 22.20、npm/npx、Git、HTTPS 网络 |
| Git 安装（可选） | Git、Python 3.11+、HTTPS 网络 |
| 运行实际成果 | 按任务书另行提供依赖、设备或服务 |

无 Node 的安装方式可以完全离线使用已解压的 Release ZIP。原生客户端发现时机由客户端决定，目前不承诺所有 IDE 自动加载。

## 更新与回滚

在新版本仓库根目录执行：

```sh
python scripts/install.py --client codex --replace --dry-run
python scripts/install.py --client codex --replace
```

只有 `--replace` 允许升级。旧目录保留为同级唯一的 `package-delivery.backup-…`，回执给出确切路径。回滚时先关闭使用该 skill 的任务，将当前目录重命名保留，再把备份重命名为 `package-delivery`。保留原备份。

## 范围与限制

- **内容判断**：检查引用、长度与摘要的一致性；成果质量、需求是否准确、日志是否由对应命令产生，需要接收方独立判断。
- **可信来源**：SHA-256 不认证作者身份，包外摘要依赖可信来源；本项目没有签名体系。
- **隐私**：拦截明显的凭据文件名，交付前仍需核对范围；没有内容级秘密扫描。
- **传输**：本地单个 ZIP，不支持分卷、断点续传或自动网盘上传。
- **运行前提**：面向可信本地操作者，不隔离恶意执行者；强杀或断电后需人工检查残留。
- **证据边界**：容量上限、实际压力测试与长期可靠性分别报告；目前没有足够日常任务对照来量化生产力收益。

## 验证状态

版本记录按实际执行保留，避免把配置或历史证据当成本次结果。

| 记录 | 内容 |
| --- | --- |
| [v1.2 验证](docs/VALIDATION-v1.2.md) | 两 Python 兼容性、真实大文件、失败处理与六组 CI |
| [GitHub Actions](https://github.com/yuanshang06-glitch/package-delivery/actions) | Ubuntu / Windows / macOS × Python 3.11 / 3.12 |
| [v1.1 发布后记录](docs/POST-RELEASE.md) | 固定标签下载、安装与历史 CI |
| [发布说明](docs/RELEASE-v1.2.0.md) | 本版新增、升级方式与实测边界 |

旧 `v1` 包仍可做完整性校验；没有可追溯扩展时，接手检查会返回拒绝。数据契约保持 `collaborator-delivery/1`、`delivery-manifest/1` 和 `collaborator-traceability/1`。见[交付格式](package-delivery/references/delivery-format.md)、[可追溯扩展](package-delivery/references/traceability.md)及[最小示例](examples/README.md)。

## 开发与测试

组合入口包含新增 28 项及运行原 52 / 31 项的两个兼容性 wrapper；不要重复统计 wrapper：

```sh
python -B tests/test_large_delivery.py --tool package-delivery/scripts/delivery.py --checker package-delivery/scripts/check_handoff.py --base-tests tests/test_delivery.py --release-tests tests/test_release.py --installer scripts/install.py
```

GB 压力测试需显式指定全新目录、容量和报告路径，准备至少两倍文件大小的可用磁盘。普通 CI 使用小 fixture：

```sh
python -B tests/benchmark_large.py --work-dir ../large-file-fixture --size-mib 8192 --report ../large-file-result.json
```

脚本只清理自己生成的大文件，保留报告；Windows 指标为各子进程的峰值 working set。详见[版本记录](CHANGELOG.md)、[后续迭代](docs/ROADMAP.md)和[设计参考](docs/REFERENCES.md)。
