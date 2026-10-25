# v1.2.0 大文件开发版验证

本版是开发分支，尚未创建v1.2.0正式Release。目标场景是几百MB到几GB的工程/资料包；配置上限为large单文件8GiB、总解压16GiB，standard默认保持64MiB/128MiB。

## 验证口径

原52项打包检查和31项可追溯/安装检查保持原样，另加小fixture的大文件模式、stored/deflated、ZIP64和目录资源上限检查。降低ZIP64阈值的测试不是GB实测。

GB压力脚本见tests/benchmark_large.py，必须显式给新工作目录、容量及报告路径；普通CI不自动生成大文件。它检查真实输入和stored ZIP长度、清单摘要、ZIP64记录、显式large校验/可追溯检查、standard拒绝，以及Windows子进程峰值working set。合成内容不代表真实用户工程或随机数据的压缩比。

## 本地独立验收：通过

2026-10-25，Windows，Python3.11.9和3.12.14分别运行：原52项打包检查（50通过，2项普通文件symlink因权限跳过）、原31项可追溯/安装检查全部通过，新增28项大文件检查全部通过。原两套测试及安装器没有修改。详细命令、退出码、数量和跳过原因见[机器记录](validation-v1.2-results.json)。

CI组合入口报告30个外层case，其中2个是运行原52/31项的兼容性wrapper；其余28个为新增检查，不能把wrapper重复计为额外产品能力。

额外独立探针验证stored/deflated、不可变profile上限及非法参数拒绝。审查发现ZIP尾记录选择的两种歧义，经过两次窄返修，注释伪签名和目录offset数字字段伪签名均在ZipFile构建前拒绝；读取请求最大65557字节，没有绕过4MiB中央目录上限。实际数字字段变体使用约96MiB稀疏fixture，和下述实写GB测试分开。

skill格式检查通过；1.2.0清单包含9个skill文件，隔离目录dry-run和实际安装通过，没有替换全局现用skill。

## 超过4GiB实际读写：通过

见[原始压力报告](large-file-stress-v1.2.json)。Windows/Python3.11.9，以显式large模式实际写入4097MiB（4,296,015,872字节）的合成文件，stored ZIP为4,296,018,605字节。源文件/ZIP成员/清单的大小及SHA-256一致，ZIP64 EOCD及locator存在，提取版本45。large完整性与可追溯检查通过；standard打包和接收明确拒绝，拒绝后没有半包。

| 操作 | 耗时 | 峰值working set |
| --- | --- | --- |
| large打包，含内部完整校验 | 33.047秒 | 19.08MiB |
| large显式校验 | 10.032秒 | 19.04MiB |
| large可追溯检查 | 9.859秒 | 19.43MiB |

这是单台机器本次观测，不能承诺其他磁盘/工程有相同性能。Windows指标为各子进程PeakWorkingSetSize，不包含系统文件缓存。内容是重复字节模式，stored保证ZIP实际体积超过4GiB；不代表真实工程或随机数据的压缩比。测试生成的大文件已清理，仅保留报告。

复现脚本见[benchmark_large.py](../tests/benchmark_large.py)。必须显式给新工作目录、容量及报告路径，预计需要至少两倍fixture大小的磁盘。普通CI不生成GB数据。

## 远端CI

本分支配置Ubuntu/Windows/macOS × Python3.11/3.12六组检查。当前尚待推送后实际执行；矩阵配置不是已通过证据。后续结果另附具体commit和run链接。

## 仍有边界

单文件8GiB/总16GiB是配置上限，没有逐一完成这些上限的压力实测。实际GB压力只覆盖Windows/Python3.11的stored路径；deflated和其他系统由小fixture回归覆盖。原生IDE发现、真实用户工程接手尚未验证。文件条目仍最多512，小元数据上限保留。校验需要完整读取内容，磁盘/CPU耗时随大小增长；不做分卷、断点续传、自动外部上传下载或恶意并发隔离。结构校验、作者身份、日志真实性和产物质量仍是不同结论。公开v1.1.0标签及附件未修改。
