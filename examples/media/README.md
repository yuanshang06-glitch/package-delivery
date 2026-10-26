# 视频与音频交付示例

这是 v1.3 开发候选的合成样本用法。检查器只读取本地媒体技术字段，不制作或修改媒体。下面的 FFmpeg 命令仅用于自行生成测试素材；正式交付继续使用任务书授权的成果。

## 准备小样本

先建立全新的 `staging/artifacts` 与 `staging/evidence` 目录。需要本地 FFmpeg 才能执行这些样本生成命令；接收方只做技术探测时仅需 ffprobe。

```sh
ffmpeg -nostdin -hide_banner -loglevel error -n -f lavfi -i testsrc2=size=320x180:rate=10:duration=3 -f lavfi -i sine=frequency=440:sample_rate=48000:duration=3 -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac -shortest -movflags +faststart staging/artifacts/final.mp4
ffmpeg -nostdin -hide_banner -loglevel error -n -f lavfi -i sine=frequency=440:sample_rate=16000:duration=3 -c:a pcm_s16le staging/artifacts/voice.wav
```

这些是测试图案和音调，没有使用用户录音、下载影片或 ASR。编码器可用性由本机 FFmpeg 构建决定；本轮实际版本和检查结果见[验证记录](../../docs/VALIDATION-v1.3.md)。

## 加入交付声明

按[基础格式](../../package-delivery/references/delivery-format.md)准备 `START-HERE.md` 和完整 `DELIVERY.json`，把主媒体、实际存在的配套文件及报告路径列入 `files`。在基础 JSON 里加入：

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
        "companions": []
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

这是要合并进基础元数据的片段，不是完整 `DELIVERY.json`。有字幕、内容记录或工程文件时，按[媒体交付格式](../../package-delivery/references/media-delivery.md)在对应 asset 的 `companions` 关联；文件存在与摘要正确不代表同步或可编辑性已验收。

## 发送方检查

在仓库根目录运行；报告路径必须是新文件，其父目录先建立：

```sh
python package-delivery/scripts/media.py inspect --root staging --out staging/evidence/media-report.json
```

技术检查通过退出 `0`，失败退出 `1`，缺工具或必要信息未知退出 `2`。若需显式指定可信工具，增加 `--ffprobe /absolute/path/to/ffprobe`。普通模式上限不变，大文件须由双方明确增加 `--profile large`。

检查后按实际结果填写 `checks` 和[可追溯记录](../../package-delivery/references/traceability.md)，绑定报告所对应的产物摘要；有任务要求的必要检查未完成时保留 `partial` / `blocked` 和缺口。脚本不会自动改写完成状态。

```sh
python package-delivery/scripts/delivery.py pack --root staging --out new-media-delivery.zip --compression stored
python package-delivery/scripts/delivery.py verify new-media-delivery.zip
python package-delivery/scripts/check_handoff.py new-media-delivery.zip
```

## 接收方复核

先用可信脚本核对 ZIP 和包外摘要，再在全新目录接手。针对解压目录重新执行 `media.py inspect`，将输出写入新报告，不覆盖生产者报告。之后按任务书核对播放器兼容、画质、听感、字幕/内容记录同步和可编辑源文件。

MP4 / WAV / MP3 已做本轮合成样本技术检查的范围，以实际验证记录为准；其他允许的容器、真实用户工程和 GB 级媒体探测尚未完整覆盖。ffprobe 探测容器及流元数据，不等于完整解码扫描或内容验收。
