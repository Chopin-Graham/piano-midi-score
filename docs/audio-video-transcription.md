# 音视频转 MIDI 再转钢琴谱

## 推荐流水线

```text
音频 / 视频
  -> FFmpeg 解码为 44.1 kHz 单声道 WAV
  -> Transkun 钢琴转录（Basic Pitch 降级）
  -> 音域、极短音、重复音、同音重叠清理
  -> Librosa 节拍跟踪与分段线性拍点映射
  -> 重音列分析：3/4 与 4/4 拍号、强拍相位
  -> 可下载的标准 MIDI
  -> MIDI 制谱语义流水线（弱起重组、分段调性、三连音自动识别、颤音记号）
  -> MusicXML 4.0
  -> MuseScore Studio A4 PDF
```

模型输出的是“演奏事件估计”，不是乐谱。把它直接导入 MuseScore 往往会出现伪三连音、短休止符、同音重叠、错误左右手和过密系统，因此仍需完整执行本项目的量化、分手、时值清理、声部分离和雕版门禁。音频模式会把不超过十六分音符的碎小释放间隙延到下一起音或节拍边界；只改变书写释放，不移动或删除任何攻击音。

## 安装

主程序环境保持轻量。音频模型安装到单独目录：

```powershell
cd F:\piano-midi-score
winget install Gyan.FFmpeg
.\scripts\install_audio_backend.ps1 -Backend transkun -TorchBuild cu121
```

CPU 机器可改用：

```powershell
.\scripts\install_audio_backend.ps1 -Backend transkun -TorchBuild cpu
```

需要兼容降级后端时：

```powershell
.\scripts\install_audio_backend.ps1 -Backend basic-pitch -TorchBuild cpu
```

默认自动寻找 `F:\piano-midi-score\.venv-audio\Scripts\python.exe`。若环境在其他位置，设置：

```powershell
$env:PIANO_MIDI_SCORE_AUDIO_PYTHON = "D:\audio-env\Scripts\python.exe"
$env:PIANO_MIDI_SCORE_FFMPEG = "D:\ffmpeg\bin\ffmpeg.exe"
```

## 网页使用

上传支持：

- MIDI：`.mid`、`.midi`，上限 10 MB；
- 音频：`.wav`、`.flac`、`.mp3`、`.m4a`、`.ogg`、`.opus`、`.aac`；
- 视频：`.mp4`、`.mov`、`.mkv`、`.webm`；
- 音视频上限 250 MB。

选项建议：

- 后端：`自动`，已安装 Transkun 时会优先选择它；
- 设备：NVIDIA 显卡选 `CUDA`，否则选 `CPU`；
- 动态节拍对齐：默认开启，自由速度演奏尤其需要；
- 短音释放下限：默认 55 ms；强短音不会被删除，只会把释放规范到该下限，亚帧或极弱伪音才会被过滤；
- 三连音：音频模式默认关闭，确认原曲使用三连音时再开启。

完成后可以分别下载 A4 PDF、MusicXML 和转录 MIDI。建议先试听中间 MIDI：若音高识别本身明显错误，应先调整模型/音源，而不是靠后续制谱规则掩盖。

## 命令行使用

```powershell
.\.venv\Scripts\python.exe scripts\transcribe_media.py `
  "C:\Users\name\performance.mp4" `
  --output "output\audio-video\performance" `
  --backend transkun `
  --device cuda `
  --minimum-note-ms 55 `
  --style clean `
  --engraving-style classic
```

输出包括：

- `*-raw-transcription.mid`：模型原始结果，便于诊断；
- `*-beat-aligned.mid`：清理并映射到动态拍点后的中间 MIDI；
- `*.musicxml`：可编辑乐谱；
- `*-A4.pdf`：最终 A4 乐谱；
- `*-preview.png`：首页预览；
- `*-report.json`：转录、制谱、雕版指标与警告。

## 参考视频验收基线

`C:\Users\ZbXiao\Downloads\参考谱面\参考视频1\425511826_1_0.mp4` 已完成三次端到端转换和一次正在运行服务的真实 multipart API 上传。最终基线使用 Transkun 2.0.1 + CUDA，341.696 秒视频的模型转录约 42 秒；共识别 2436 个攻击音，清理后仍为 2436 个，删除 0 个，260 个强短音只规范释放长度。

第二轮发现音频模型的释放时间会制造 1004 个可见休止符。第三轮加入音频专用书写释放推断，只把不足十六分音符的碎间隙延至下一起音或节拍边界，不移动或删除攻击音：共延长 815 个释放，可见休止符降至 576，A4 页数由 10 页降至 9 页，系统由 53 个降至 49 个，单小节孤行为 0。

最终 PDF 的 9 页联系图和第 9 页 360 dpi 图均未发现裁切、粘连、黑块、空白页或错误终止线。质量状态仍为 `needs_review`：模型释放造成 11 处持续手距风险，最大保持跨度为 40 个半音。这是音乐语义/物理演奏风险，不是版面错误，因此程序不会把该自由演奏转录伪报为完全无需校订的演奏级成品。

## 适用边界

最佳输入是清晰的钢琴独奏、较少环境噪声、不过度混响且没有明显削波的录音。以下情况仍需人工复核：

- 乐队混音、人声或强打击乐覆盖钢琴；
- 极重踏板和长混响导致相邻和声粘连；
- 视频音轨经过强降噪、限幅或低码率压缩；
- 自由速度且节拍跟踪跳拍；
- 快速重复音、刮奏、颤音和未经对齐的双手滚奏。

程序会保留模型识别到的音头并报告风险，不会为了让页面好看而静默删除大段音乐。对于混音素材，可先尝试 Demucs piano stem，但必须试听确认分离质量；它不是默认自动步骤。
