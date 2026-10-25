# package-delivery · 成果完整交付

**v1.1.0 · Python 3.11+ · 标准库 · MIT**

给合作者或 AI Agent 派任务后，交回一个能直接接手的包：实际产物、接手说明、验证证据、验收要求对照表和未完成项。

这个项目是一个可移植 Agent Skill，加两个独立检查脚本和本地安装器。没有后台服务，不需要 API Key、付费模型或额外 Python 依赖；任务本身使用什么模型由你决定。

版本与附件见 [v1.1.0 Release](https://github.com/yuanshang06-glitch/package-delivery/releases/tag/v1.1.0)。环境、已测范围和目前短板见下文及[验证记录](docs/VALIDATION.md)；最新远端检查见 [GitHub Actions](https://github.com/yuanshang06-glitch/package-delivery/actions)。

[发布后验证](docs/POST-RELEASE.md)：固定标签Git/npx安装、公开附件摘要检查通过；三系统×两Python的六组CI成功。原生客户端自动发现等限制仍保留。

## 快捷安装

### npx 一条命令

```sh
npx skills@1.7.4 add https://github.com/yuanshang06-glitch/package-delivery/tree/v1.1.0/package-delivery --skill package-delivery -a codex -g --copy
```

这是第三方 [Vercel skills CLI](https://github.com/vercel-labs/skills) 的可选安装路径，需要Node.js≥22.20、npm/npx、Git与网络。命令固定CLI和skill版本，选择Codex的全局范围并复制文件。现有安装的覆盖与更新遵循该CLI行为；需要保留旧目录时，使用下面自带安装器的`--replace`流程。该CLI不属于本项目代码或运行时依赖。

### 已解压本仓库：无需 Git 或网络

在仓库根目录执行（Windows 也可把 `python` 换成 `py -3.11`）：

```sh
python scripts/install.py --client codex --dry-run
python scripts/install.py --client codex
```

默认安装到 `${CODEX_HOME}/skills/package-delivery`，未设置 CODEX_HOME 时安装到 `~/.codex/skills/package-delivery`。只复制 skill，不复制测试、仓库文档或会话日志。默认拒绝覆盖已有安装。

Claude Code 的目录安装可用 `--client claude`；该目标为 `~/.claude/skills/package-delivery`，本次只验证目录安装，尚未验证 Claude 的实际加载行为。其他工具可直接读取 `package-delivery/SKILL.md`，不承诺原生发现。

### 两步安装（Git + Python）

```sh
git clone --depth 1 --branch v1.1.0 https://github.com/yuanshang06-glitch/package-delivery.git package-delivery-v1.1.0
python package-delivery-v1.1.0/scripts/install.py --client codex
```

使用固定版本标签，不下载执行漂移的 main 脚本。安装前可先查看 `scripts/install.py`，或在最后一条命令加 `--dry-run` 预览写入位置。

在有 `$skill-installer` 的 Codex 中，也可发送：

```text
使用 $skill-installer 从 https://github.com/yuanshang06-glitch/package-delivery/tree/v1.1.0/package-delivery 安装 package-delivery。
```

本仓库的安装器和 Codex 自带安装器是两条可选路径，不要连续安装两次。快捷安装的本地测试和 GitHub 下载验证范围见 [验证记录](docs/VALIDATION.md)。

## 环境要求

| 用途 | 要求 |
| --- | --- |
| 阅读 skill、手动组织文件夹 | 能读取 Markdown 和文件的合作者或 Agent |
| 打包、校验、安装器与测试 | Python 3.11+；全部使用标准库；无 pip 安装步骤 |
| 从 GitHub clone 安装 | Git、HTTPS 网络连接 |
| npx 快捷安装（可选） | Node.js≥22.20、npm/npx、Git、HTTPS网络；安装后打包仍需Python3.11+ |
| Codex 原生安装 | 支持本地 skills 的 Codex 环境；发现时机由客户端决定 |
| 任务实际运行 | 另按任务书提供依赖、账号、设备或运行环境；本 skill 不代装 |

本机验证范围会写在 [VALIDATION.md](docs/VALIDATION.md)，不把预设 CI 矩阵写成已通过结果。

## 给合作者派任务

填好 [任务单模板](package-delivery/references/task-brief.md)，提供 skill 文件夹或对方可访问的位置，再加一句：

> 请按 package-delivery/SKILL.md 完成交付。逐条对应验收要求、产物和实际验证；未执行的检查明确写原因。交回完整 ZIP，最后的 ZIP 摘要放包外回执，自测不代替接收方验收。

交付结构：

```text
result.zip
├── START-HERE.md       接手入口、最少操作步骤和短板
├── DELIVERY.json       完成状态、文件白名单、需求与证据对照
├── MANIFEST.json       实际入包文件长度和 SHA-256
├── artifacts/          源文件、可编辑成果和必要资源
└── evidence/           真实检查记录，区分本次执行、继承、未运行
```

## 打包与接收

```sh
python package-delivery/scripts/delivery.py pack --root staging --out result.zip
python package-delivery/scripts/delivery.py verify result.zip
python package-delivery/scripts/check_handoff.py result.zip
```

最后一条检查 v1.1 的需求—产物—证据引用、成果摘要绑定与返修关联；不执行包内命令。接收方使用自己信任的脚本核对包外摘要，通过后在新目录接手，并按任务书独立验收。

旧 v1 包仍可用 `delivery.py verify` 检查；没有可追溯扩展时，`check_handoff.py` 会明确拒绝，不能误报为 v1.1 验证通过。

格式与样例见 [交付格式](package-delivery/references/delivery-format.md)、[v1.1 可追溯扩展](package-delivery/references/traceability.md) 和 [最小示例](examples/README.md)。

## 更新与回滚

```sh
python scripts/install.py --client codex --replace --dry-run
python scripts/install.py --client codex --replace
```

仅 `--replace` 允许升级。旧目录保留为同级唯一 `package-delivery.backup-...`，安装回执给出确切路径。回滚时先关闭正在使用该 skill 的任务，把当前 `package-delivery` 重命名为另一个保留目录，再把回执里的旧备份重命名为 `package-delivery`；不要覆盖或删除原备份。

## 目前短板

- 检查引用与摘要的一致性，不能判断需求文字是否准确、产物质量是否合格，不能证明日志真的由对应命令产生。
- 包内摘要不认证作者身份；包外 SHA-256 也依赖可信来源。安装清单只校验收到的本地源，不提供签名或供应链身份认证。
- 只拦截明显凭据文件名，不做内容级秘密扫描；交付前仍要核对文件范围。
- 默认最多 512 个 ZIP 文件、单文件 64 MiB、总解压 128 MiB；暂不支持大模型权重、大视频或分卷交付。
- 原生客户端自动发现、跨系统实际运行与公网下载的已测/未测范围以验证记录为准。
- 只支持可信本地操作者，不提供恶意执行者隔离，也不对安装期间断电或被其他程序修改文件提供事务保证。
- 当前没有足够日常任务对照数据，不能宣称能提升多少生产力。

## 版本与迭代

采用语义版本号。脚本或旧包兼容性破坏时升主版本；新增兼容能力升次版本；修复升补丁。包格式版本单独管理，不能用 skill 版本代替数据契约版本。

见 [CHANGELOG](CHANGELOG.md)、[ROADMAP](docs/ROADMAP.md)、[设计参考](docs/REFERENCES.md) 和 [发布说明](docs/RELEASE-v1.1.0.md)。此项目仅提供本地交付工具；发布新版本需独立验收。

## 开发与测试

```sh
python -B tests/test_delivery.py --tool package-delivery/scripts/delivery.py
python -B tests/test_release.py --checker package-delivery/scripts/check_handoff.py --installer scripts/install.py
```

测试使用自造临时目录，不安装到真实用户目录。GitHub Actions 会在发布上传后执行，其运行结果不会被本地报告提前宣称通过。
