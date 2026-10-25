# 设计参考

2026-10-25 阅读以下项目原始文档后提炼设计思路；未复制其实现代码或安装整个框架。本项目不宣称兼容 BagIt 或 RO-Crate 格式。

- [obra/superpowers implementer-prompt](https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/implementer-prompt.md)：明确实际实现、改动文件、验证与疑虑回报。
- [verification-before-completion](https://github.com/obra/superpowers/blob/main/skills/verification-before-completion/SKILL.md)：结论应由实际证据支持；逐条核对需求，测试通过不自动代表需求满足。
- [LibraryOfCongress/bagit-python](https://github.com/LibraryOfCongress/bagit-python/blob/master/README.rst)：清单与摘要、缺失/额外/不匹配文件的区分。现有 v1 助手已有完整性校验，本轮保持其接受字节。
- [ResearchObject/ro-crate-py](https://github.com/ResearchObject/ro-crate-py/blob/master/README.md)：用结构化关系描述成果及来源；本项目采用更小的任务—产物—证据关联。
- [carlitose/agent-skills verification record](https://github.com/carlitose/agent-skills/blob/main/verification-audit/references/verification-record.md)：证据绑定候选版本并标明证据类型，避免将模拟、继承或局部证据扩大为未验证结论。

上述链接指向阅读时默认分支，未来可能改变；它们是设计出处，不是运行时依赖。

## 可选安装与客户端文档

- [vercel-labs/skills](https://github.com/vercel-labs/skills/blob/main/README.md) 与 [CLI命令](https://github.com/vercel-labs/skills/blob/main/docs/commands.md)：本次npm查询与本地发现测试使用skills@1.7.4；Node最低22.20。它是可选外部安装工具，与本项目Python标准库脚本分开。
- 本机Codex自带skill-installer的说明和脚本：支持指定GitHub URL或repo/path/ref，拒绝覆盖已有目录；未读取其凭据。
- [OpenAI skills文档](https://learn.chatgpt.com/docs/build-skills)：阅读了官方技能构建说明；本项目不额外包装插件市场或后台服务。
