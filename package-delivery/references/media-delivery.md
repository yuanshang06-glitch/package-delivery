# 音视频交付扩展（media-inspection/1）

本文件描述 `src/media.py`（最终仓库中的 `package-delivery/scripts/media.py`）的使用契约。它是对现有 `collaborator-delivery/1` 打包/校验流程的**可选、显式、只读检查扩展**，不是新的打包器，也不改变原 `DELIVERY.json` 基础格式或原 `delivery.py` 行为。

范围仅包括：

- 读取 `DELIVERY.json` 中显式声明的 `media`；
- 对声明的媒体文件和 companion 做分块 SHA-256；
- 在本地 `ffprobe` 可用时读取技术流信息；
- 生成 `media-inspection/1` 技术检查报告。

不做制作、转码、ASR、下载、上传、播放器、网站，也不自动扫描目录或自动修改 `DELIVERY.json`。

---

## 1. 声明格式

`DELIVERY.json` 在基础字段之外可以带一个可选的 `media` 字段。基础格式允许附加字段，因此不改变 schema。

```json
{
  "media": {
    "schema_version": "collaborator-media/1",
    "assets": [
      {
        "path": "artifacts/final.mp4",
        "kind": "video",
        "require_audio": true,
        "expected": {
          "width": 320,
          "height": 180,
          "min_duration_seconds": 2.5,
          "max_duration_seconds": 3.5
        },
        "companions": [
          {"path": "artifacts/final.srt", "role": "subtitle"}
        ]
      },
      {
        "path": "artifacts/voice.wav",
        "kind": "audio",
        "expected": {"sample_rate": 16000, "channels": 1},
        "companions": []
      }
    ]
  }
}
```

### 1.1 媒体声明规则

- `media` 必须是对象，且只含 `schema_version` 和 `assets`；`schema_version` 必须是 `collaborator-media/1`。
- `assets` 必须是非空数组，最多 32 项。
- asset 必须含 `path` 与 `kind`，可含 `require_audio`、`expected`、`companions`；其他字段拒绝。
- `path` 必须是包内相对路径，且必须**原样**出现在基础 `files` 中。所有 asset 按与基础 `files` 相同的 NFC/casefold 规则保持唯一。
- `kind` 只能是 `video` 或 `audio`。
- `require_audio` 只允许用于 `video`，类型必须是 bool，省略时为 `false`；`audio` 不接受该字段。
- 所有声明只能指向**文件**，不能指向目录或 URL。目录声明、绝对路径、`..`、反斜杠、凭据路径、符号链接/junction 等都由 `delivery.py` 的可信 helper 拒绝。
- 未知的 asset、`expected`、companion 字段一律报错，避免拼写错误被静默跳过。

### 1.2 可信期望值 `expected`

`expected` 可省略或为空对象。允许的键只有：

| 键 | 类型 | 适用 |
| --- | --- | --- |
| `width` | 正整数（bool 不接受） | 仅 `video` |
| `height` | 正整数（bool 不接受） | 仅 `video` |
| `sample_rate` | 正整数（bool 不接受） | 首个真实 audio 流 |
| `channels` | 正整数（bool 不接受） | 首个真实 audio 流 |
| `min_duration_seconds` | 有限正数（bool 不接受） | 任意 |
| `max_duration_seconds` | 有限正数（bool 不接受） | 任意 |

`min_duration_seconds <= max_duration_seconds`。未知键、非有限数、负值、bool 等都会得到 `EXPECTED_INVALID`。

### 1.3 companion

`companions` 可省略或为数组。每项必须是仅含 `path` 和 `role` 的对象：

- `path` 必须出现在基础 `files` 中；
- `role` 只能是 `subtitle`、`transcript`、`editable_source`、`preview`、`cover`；
- 同一 asset 内 companion 路径按 NFC/casefold 不重复；
- 不同 asset 可以共享同一个 companion 文件。

companion 只验证存在、普通文件、字节数与 SHA-256。模块**不**声称字幕语义、转写准确性、时间同步或工程可编辑性已经验收。

---

## 2. Python API

```python
inspect_media(root, *, profile="standard", ffprobe=None, timeout=15) -> dict
MediaError.code
```

- `root`：staging 根目录（含 `DELIVERY.json`）。
- `profile`：`standard` 或 `large`，通过 `delivery.get_limits()` 选择。只有调用者显式传 `large` 才会使用大文件上限，模块不会根据文件大小推断 profile。
- `ffprobe`：可选的本地可信 ffprobe 可执行文件**绝对路径**；省略时使用 `shutil.which("ffprobe")`。自动发现的结果若位于本次被检查的 `root` 内（按规范化实路径、大小写和 symlink/junction 别名判断），会被拒绝而不会启动任何进程，报告为 `not_run`；接收方应显式传入可信的 `--ffprobe`。
- `timeout`：每次子进程 1–60 秒的有限数字（含 `-version` 调用），bool 不接受。

公开函数只返回报告，不写文件。CLI 负责报告输出。

`MediaError` 的 `code` 是固定、机器可读的错误码；消息和 CLI stderr 不回显路径、元数据正文或子进程 stderr。

---

## 3. CLI

```bash
python media.py inspect \
  --root staging \
  --out evidence/new-report.json \
  [--profile standard|large] \
  [--ffprobe /trusted/path/ffprobe] \
  [--timeout 15]
```

### 3.1 报告路径规则

- `--out` 必填；父目录必须已存在、是真实目录、不是链接，并且可写。
- 输出路径必须**尚不存在**；已有文件（包括旧报告）会得到 `OUTPUT_EXISTS`，不会覆盖。
- 输出不能是本次检查的输入路径：`DELIVERY.json`、任一 media asset、任一 companion。若该路径已存在，先得到 `OUTPUT_EXISTS`；若它是不存在的输入路径（例如尚未生成的媒体或 companion 声明），得到 `OUTPUT_COLLISION`。
- 推荐写在 staging 之外的新目录，例如 `evidence/media-report-<attempt>.json`；确保 `evidence/` 已存在且报告文件尚未生成。
- 若报告要进入交付包，先生成报告，再由发送方把报告复制到 staging 并把其路径加入 `DELIVERY.json` 的 `files`，最后执行 `delivery.py pack`。`media.py` 不会自动编辑 `DELIVERY.json`。
- 基础 `files` 中尚未生成的非 media 文件不会被扫描；这是为“报告先生成、后打包”的流程留出的空间。但任何已存在的输入文件都不会被本工具修改。

### 3.2 写入与退出行为

- 输入元数据/声明/参数无效时不写报告。
- 在输入有效、但技术检查为 `failed` 或 `not_run` 时，报告仍会保存。
- 写入使用 `O_EXCL` 创建；写入失败只清理本次创建的部分文件，不删除旧文件。
- 成功生成报告时 stdout 只输出：

```json
{"technical_status":"passed","assets":2}
```

- 输入或 IO 错误时 stderr 只输出固定 JSON：

```json
{"error":"MEDIA_MISSING"}
```

- 退出码：
  - `0`：报告 `technical_status` 为 `passed`；
  - `1`：`failed`，或输入/参数/IO 错误；
  - `2`：`not_run`（例如本机没有 ffprobe，或时长等必需信息无法确定）。

---

## 4. 报告结构

```json
{
  "schema_version": "media-inspection/1",
  "independent_acceptance": "UNKNOWN",
  "profile": "standard",
  "technical_status": "passed",
  "tool": {
    "name": "ffprobe",
    "available": true,
    "version": "ffprobe version 8.1.2-essentials_build-www.gyan.dev ..."
  },
  "assets": [
    {
      "path": "artifacts/final.mp4",
      "kind": "video",
      "bytes": 12345,
      "sha256": "<64 hex>",
      "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
      "duration_seconds": 3.0,
      "streams": [
        {
          "index": 0,
          "codec_type": "video",
          "codec_name": "h264",
          "width": 320,
          "height": 180,
          "sample_rate": null,
          "channels": null,
          "duration": 3.0,
          "attached_pic": false
        }
      ],
      "checks": [
        {"name": "file", "status": "passed"},
        {"name": "probe", "status": "passed"},
        {"name": "streams", "status": "passed"},
        {"name": "duration", "status": "passed"},
        {"name": "source_unchanged", "status": "passed"}
      ],
      "companions": [
        {
          "path": "artifacts/final.srt",
          "role": "subtitle",
          "bytes": 100,
          "sha256": "<64 hex>",
          "status": "passed"
        }
      ],
      "technical_status": "passed"
    }
  ],
  "manual_checks": [
    {"name": "visual_quality", "status": "not_run", "reason": "..."},
    {"name": "audio_quality", "status": "not_run", "reason": "..."},
    {"name": "subtitle_or_transcript_sync", "status": "not_run", "reason": "..."},
    {"name": "editable_project_integrity", "status": "not_run", "reason": "..."}
  ]
}
```

报告只保存技术字段：

- 顶层只有声明版本、独立验收标记、profile、技术状态、工具信息、assets 和 manual checks；
- asset 只保存 `path`、`kind`、`bytes`、`sha256`、`format_name`、`duration_seconds`、`streams`、`checks`、`companions`、`technical_status`；
- stream 只保存 `index`、`codec_type`、`codec_name`、`width`、`height`、`sample_rate`、`channels`、`duration`、`attached_pic`；
- 不保存 `tags`、artist/author、GPS、本机 filename 或其他元数据正文；
- 工具信息只记录可用性与实际 `ffprobe` 版本首行，不记录工具绝对路径。

### 4.1 技术检查语义

- `video` 必须存在至少一个 `codec_type == "video"` 且**不是** `attached_pic` 的视频流；只有封面图的音频/MP3 不能算视频交付通过。
- `audio` 必须存在至少一个音频流。
- `video` 且 `require_audio=true` 时缺音轨为 failed。
- 主视频流取最低 `index` 的非封面视频流；主音频流取最低 `index` 的音频流。`expected` 中的 `width`/`height` 只比较主视频流，`sample_rate`/`channels` 只比较主音频流；报告最多列出 64 个音视频流。
- 时长优先取有效的 `format.duration`，缺失、`"N/A"` 或 `0` 时回退到主视频/主音频流的 `duration`（流时长 `0` 同样视为未知）；仍无法确定时 `duration_seconds` 为 `null`，时长检查为 `not_run`，整体不能声称技术通过。
- 缺失字段记录 `null`，不编造 0；非有限或非法数字、非法 JSON、重复 JSON 键、`NaN`/`Infinity` 等为 `PROBE_OUTPUT_INVALID`/failed。
- `expected` 字段有声明时逐项比较：实际值缺失为 `not_run`（`EXPECTED_UNKNOWN`），实际值不匹配为 `failed`（`EXPECTED_MISMATCH`）。
- 缺失文件、损坏文件、超出 profile 上限、probe 失败都会在报告中记为失败，不会被跳过。
- 每个媒体文件和 companion 都分块计算 SHA-256；同一路径的共享 companion 只读取/计数一次。单文件和“所有唯一实际读取文件”的总量受所选 profile 限制，不会整读超过上限的 GB 文件。
- 在 probe 前后比较媒体文件的 stat 身份、大小和 `mtime_ns`；检测到变化记 `SOURCE_CHANGED`。这是诚实检测，不是恶意并发隔离。

### 4.2 报告中的检查码

`checks`/`companions` 可能包含以下非异常检查码：

- `TOOL_MISSING`：本机没有可用的 ffprobe；
- `TOOL_UNTRUSTED`：自动发现的 ffprobe 位于被检查 `root` 内（含规范实路径/大小写/junction 别名），已在启动 `-version` 或 probe 前拒绝；不会回显该路径，需改用显式可信工具；
- `SOURCE_UNAVAILABLE`：文件缺失/不可用，无法 probe；
- `DURATION_UNKNOWN`：无法确定时长；
- `EXPECTED_UNKNOWN` / `EXPECTED_MISMATCH`：期望值无法比较 / 不匹配；
- `NO_VIDEO_STREAM` / `NO_AUDIO_STREAM`：缺少要求的流；
- `AUDIO_REQUIRED_MISSING`：`require_audio=true` 但缺音轨；
- `COMPANION_FAILED`：至少一个 companion 未通过文件/SHA 检查；
- `STREAM_LIMIT`：音视频流超过 64 个，只报告前 64 个；
- `FILE_MISSING`、`FILE_NOT_REGULAR`、`FILE_LIMIT`、`TOTAL_LIMIT`、`SOURCE_CHANGED`、`IO_ERROR` 等文件检查码。

---

## 5. ffprobe 依赖与降级

`ffprobe` 是可选本地工具，不是运行时依赖：

- 省略 `--ffprobe` 时尝试 `shutil.which("ffprobe")`；
- 自动发现得到的候选若位于被检查 `root` 内（规范实路径、大小写不敏感比较及 symlink/junction 别名均算“内”），会在启动 `-version` 或 probe **之前**拒绝：不启动任何子进程，报告 `not_run`，检查码 `TOOL_UNTRUSTED`，原因文本不含绝对路径，只说明需要显式可信工具。这防止接收方在解包目录中自动运行包内同名程序；
- 显式传入的 `--ffprobe` 仍按既有契约视为调用者信任的本地工具，不做上述自动发现拦截；
- 显式传入时必须是绝对路径；不存在的路径、目录等视为工具不可用；
- 工具不可用时报告 `tool.available=false`、所有 probe/时长/期望检查为 `not_run`，CLI 退出码 `2`；
- 不会下载、安装、从包内读取工具路径或执行包内命令；不会修改 PATH，也不会为寻找替代工具而扫描其他目录。

实际调用固定为（无 shell 拼接）：

```text
<ffprobe> -v error -protocol_whitelist file \
  -format_whitelist mov,matroska,avi,wav,mp3,flac,ogg,aac \
  -probesize 5000000 -analyzeduration 5000000 \
  -show_entries format=format_name,duration:stream=index,codec_type,codec_name,width,height,sample_rate,channels,duration:stream_disposition=attached_pic \
  -of json -i <absolute-file>
```

限制：

- 每次子进程（含 `-version`）都有 `--timeout` 上限；
- stdout 硬上限 1 MiB，stderr 硬上限 64 KiB；由两条读线程按块收集，超限立即 kill 并回收，不使用无界 `communicate()`；
- `shell=False`；Windows 隐藏控制台；不修改系统环境；
- `-protocol_whitelist file` 禁止 URL/网络协议，`-format_whitelist` 限制解复用器；
- `probesize=5,000,000` 字节、`analyzeduration=5,000,000` 微秒是技术探测预算，不是解码完整性扫描，也不是恶意媒体沙箱。

---

## 6. profile 与文件上限

`profile` 只通过 `delivery.get_limits()` 选择，且只有调用者显式传 `large` 才使用大文件上限：

| 限制 | standard | large |
| --- | --- | --- |
| 单文件 | 64 MiB | 8 GiB |
| 所有唯一实际读取文件总量 | 128 MiB | 16 GiB |
| `DELIVERY.json` | 64 KiB | 64 KiB |

超出单文件上限记 `FILE_LIMIT`，超出总量记 `TOTAL_LIMIT`，都不会继续读取该文件。`DELIVERY.json` 仍由可信 helper 以 64 KiB 上限读取。

---

## 7. 发送方与接收方命令顺序

### 7.1 发送方

1. 将最终视频/音频和 companion 放入 staging，例如 `staging/artifacts/`。
2. 编辑 `staging/DELIVERY.json`：把所有媒体与 companion 路径加入 `files`，并添加 `media` 声明。不要手工改 schema 版本。
3. 生成报告到 staging 之外的新路径，父目录先创建：

   ```bash
   python media.py inspect \
     --root staging \
     --out evidence/media-report.json \
     --profile standard
   ```

   ffprobe 不在 PATH 时可加 `--ffprobe /trusted/path/ffprobe`。

4. 查看报告。若 `technical_status` 为 `failed` 或 `not_run`，按报告中的 checks 修正；需要重新检查时使用**新的**输出路径（工具不会覆盖旧报告）。
5. 如需把报告放入交付包：复制报告到 staging 的新路径，在 `DELIVERY.json` 的 `files` 中声明它，然后运行原有打包：

   ```bash
   python delivery.py pack --root staging --out dist/task.zip
   ```

### 7.2 接收方

1. 先按原流程校验并解包交付 ZIP（`delivery.py verify` / `check_handoff.py`）。
2. 在解包目录上显式运行本扩展，报告写到解包目录之外的新路径：

   ```bash
   python media.py inspect \
     --root extracted \
     --out evidence/received-media-report.json \
     --profile standard \
     --ffprobe /trusted/path/ffprobe
   ```

3. 核对报告中的 `technical_status`、asset/companion 的 `bytes` 与 `sha256`、`duration_seconds` 和 `streams`。
4. 如报告声明 `large`，接收方也必须显式使用 `--profile large`；不存在自动推断。
5. 技术检查通过后，仍必须人工完成第 8 节列出的检查；`technical_status=passed` 不等于内容合格或独立验收。

---

## 8. manual checks 与限制

报告中的 `manual_checks` 始终为 `not_run`，原因包括：

- `visual_quality`：画面质量、构图、闪烁、花屏等需要人眼确认；
- `audio_quality`：音量、削波、噪声、听感等需要人耳确认；
- `subtitle_or_transcript_sync`：字幕/转写语义与时间同步需要人工核对；
- `editable_project_integrity`：工程文件、素材引用、可编辑性需要人工核对。

其它限制：

- 本模块只验证技术字段和文件完整性，不验证内容是否正确、合法或符合需求；
- `technical_status` 只总结技术检查；人工检查未做 **不等于** 独立验收通过；
- 报告固定写 `independent_acceptance: "UNKNOWN"`，任何使用方都不能把 `technical_status=passed` 当作独立 ACCEPTED；
- 不做恶意媒体隔离：不承诺抵御恶意并发修改、恶意 ffprobe 输入或解码漏洞；
- 不读取凭据、环境配置、用户资料或其他会话；
- 不联网、不安装依赖、不额外调用模型/agent；
- 不修改全局技能、版本、`src/delivery.py`、`check_handoff.py` 或旧测试。

---

## 9. 错误码速查

### 声明/输入错误（不生成报告）

- `INVALID_ARGUMENT`：命令行参数结构错误；
- `INVALID_PROFILE`：profile 不是 `standard`/`large`；
- `INVALID_TIMEOUT`：timeout 不是 1–60 的有限数字；
- `INVALID_FFPROBE`：显式 ffprobe 不是绝对路径或不是路径字符串；
- `ROOT_INVALID`：root 不存在、不是目录或是链接；
- `METADATA_MISSING` / `METADATA_INVALID` / `METADATA_LIMIT` / `METADATA_ACCEPTANCE_CLAIM`：`DELIVERY.json` 缺失、无效、超出 64 KiB，或声明了不允许的验收结论；
- `MEDIA_MISSING`：没有 `media` 字段；
- `MEDIA_INVALID`：`media`/`assets` 结构、schema 或数量无效；
- `ASSET_INVALID`：asset 字段、kind、`require_audio`、路径不在 `files`、声明为目录等；
- `ASSET_DUPLICATE`：asset 路径重复；
- `EXPECTED_INVALID`：`expected` 字段/类型/范围无效；
- `COMPANION_INVALID`：companion 字段、role、路径不在 `files`、同 asset 内重复等；
- `PATH_INVALID` / `PATH_SECRET` / `PATH_UNSAFE`：相对路径规则、明显凭据路径、链接/junction 等；
- `OUTPUT_INVALID` / `OUTPUT_EXISTS` / `OUTPUT_PARENT_MISSING` / `OUTPUT_UNSAFE` / `OUTPUT_COLLISION`：输出路径非法、已存在、父目录缺失/链接/不可写，或与输入路径冲突；
- `IO_ERROR`：无法读取或写入。

### 报告内失败/未运行码

文件与探针检查使用 `FILE_MISSING`、`FILE_NOT_REGULAR`、`FILE_LIMIT`、`TOTAL_LIMIT`、`SOURCE_CHANGED`、`PROBE_LAUNCH`、`PROBE_TIMEOUT`、`PROBE_OUTPUT_LIMIT`、`PROBE_FAILED`、`PROBE_OUTPUT_INVALID`、`TOOL_MISSING`、`TOOL_UNTRUSTED`、`DURATION_UNKNOWN`、`EXPECTED_UNKNOWN`、`EXPECTED_MISMATCH`、`NO_VIDEO_STREAM`、`NO_AUDIO_STREAM`、`AUDIO_REQUIRED_MISSING`、`COMPANION_FAILED`、`STREAM_LIMIT`、`SOURCE_UNAVAILABLE`。

---

## 10. 测试说明

聚焦测试位于 `tests/test_media.py`：

```bash
python tests/test_media.py --tool src/media.py
```

最终仓库可使用：

```bash
python tests/test_media.py --tool package-delivery/scripts/media.py
```

测试使用 mock 的 ffprobe JSON，并用少量真实 Python 子进程验证 bounded runner 的超时和 stdout/stderr 上限，不依赖 ffmpeg 安装，不读取用户音视频。真实合成 MP4/WAV/MP3 的独立接收由协调方执行；本扩展的 worker 不声称已完成真实媒体实测。
