# 大文件交付（1.2.0 开发版）

适合几百MiB到几GiB的工程、资料和已压缩资源。默认普通模式保持原上限；双方显式选择large才扩大本地读写预算，不根据包内声明自动提高。

## 容量与环境

| 项目 | standard（默认） | large（显式启用） |
| --- | --- | --- |
| 单文件 | 64 MiB | 8 GiB |
| 总解压内容（含元数据和清单） | 128 MiB | 16 GiB |
| ZIP条目 | 512 | 512 |
| DELIVERY.json / MANIFEST.json | 64 KiB / 512 KiB | 64 KiB / 512 KiB |
| ZIP中央目录 | 4 MiB | 4 MiB |

Python3.11+标准库；接收方也需要支持此模式的新版本。GiB=1024³字节。上述是配置上限，实际已测尺寸、环境和资源占用另见本版验证记录，不能把上限写成每个边界都已压力验证。

## 双方命令

```sh
python package-delivery/scripts/delivery.py pack --root staging --out engineering.zip --profile large --compression stored
python package-delivery/scripts/delivery.py verify engineering.zip --profile large
python package-delivery/scripts/check_handoff.py engineering.zip --profile large
```

stored直接存储，不重复压缩已压缩的ZIP、图片、视频等资源；文本/源码可用默认deflated。改变压缩方式不改变清单中原文件的摘要。大文件用ZIP64，接收方解压工具也需支持ZIP64，尤其文件或档案超过4GiB时。

接手说明写明需要large、总解压大小、最大文件大小、建议磁盘空间、压缩方式、实际验证与缺口。发送最终ZIP的外部回执记录模式及SHA-256；包内大小/模式不能替收件人授权消耗资源。

## 磁盘、耗时和内存

payload分块读写和计算SHA-256，不将整个GB文件加载到内存。ZIP目录和两种小元数据仍有独立上限。验证要读取完整内容，耗时随总量增长；pack末尾还会实际verify，不把“写完”当校验通过。

规划磁盘时至少考虑staging原件、输出ZIP和接收解压后的各一份，另留临时空间。不可压缩内容的stored ZIP通常接近原总量；不能假定压缩一定节省磁盘。写入中断或失败应返回非零并清理本次半包，旧输出不覆盖。进程强杀或断电后仍需人工确认残留；不承诺崩溃事务恢复。

## 边界

本轮是本地单个ZIP的GB级支持。分卷、断点续传、网盘/对象存储上传与自动下载、大于16GiB整包及内容级秘密扫描仍需后续任务。现有路径、链接、白名单、固定元数据上限和独立验收规则继续适用。

若不适合把资源放进ZIP，可在任务书明确外部获取地址、版本、字节数、摘要、访问条件和有效期，并说明尚未离线接收；本版不自动获取这些资源，也不将仅有链接当作完整离线交付。
