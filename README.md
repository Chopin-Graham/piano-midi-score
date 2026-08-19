# Piano MIDI Score

一个本地运行的钢琴 MIDI/音视频转谱程序。MIDI 不会被机械地逐条画到谱面上，而是先完成自适应量化、左右手分配、演奏时值规范化、声部分离和音高拼写，再由 MuseScore Studio 输出 A4 PDF。钢琴录音或演奏视频可先由 Transkun/Basic Pitch 转录为可下载的中间 MIDI，再进入同一套制谱流水线。

最终可获得：

- MuseScore 雕版的 A4 PDF 与逐页 PNG 预览；
- 可在 MuseScore、Dorico、Sibelius 等软件中继续编辑的 MusicXML；
- 音符数、谱表误放、同声部重叠和页面布局等质量报告。

## 当前能力

- 对单轨混合钢琴 MIDI 使用动态规划分手，并检查最多五键、八度舒适区、九度/十度伸展、大十度绝对上限，以及和弦内部是否存在自然的五指分配。
- 持续音按原始按键时值做物理审核；只有同一 MIDI 通道的延音踏板从冲突点连续覆盖到该音结束时，才允许释放旧手指，谱面时值清理不能掩盖不可达手距。
- 物理左右手与高低音谱表完全解耦；右手低音可以正确写在低音谱表，不再制造大片加线。
- 支持弱起、中途变拍和 6/8、9/8、12/8 附点拍组，避免把复合拍号误写成虚假三连音。
- 将约 0.15 拍以内、释放同步、属于同一只手且跨度不超过大十度的微时差滚奏归并为一个书写和弦；不满足这些条件时保留原始先后关系。
- 同一通道踏板连续覆盖时，可把不足十六分音符的松键缝隙延至强拍或小节线；物理可弹性仍使用清理前的原始按键时值审核。
- 6/8 整小节持续音直接写成一个附点二分音符，不再人为拆成两个附点四分音符并添加冗余延音线。
- 使用 Partitura 1.9 的 Chew–Wu 声部分离和 PS13 音高拼写，避免重复实现成熟算法。
- 在不删除音头的前提下规范演奏型重叠，默认将每个谱表控制在 1–2 个清晰声部。
- 按雕版节奏列和声部密度安排每行小节数，常规目标为 2–6 小节；分页交给 MuseScore 的实际碰撞结果，避免空白页。
- 只对完整极端音区乐句使用 8va/8vb，并同步移写书写音高；过短八度线保留加线，避免孤立标签造成误读。
- 提供 Leland 经典、Bravura 现代和 Leland 紧凑三种用户可选 A4 雕版风格。
- 从力度统计自动规划谱面力度记号（相对本曲中位数映射 pp–ff、两小节最短持续、平值力度文件不标注），可手动关闭。
- MuseScore Studio 4 负责最终的 A4 间距、碰撞规避、分页与 PDF 导出。
- MuseScore 不可用时仍返回 MusicXML，并由 OpenSheetMusicDisplay 提供网页回退预览。
- 音频/视频通过 FFmpeg 抽取 44.1 kHz 单声道 PCM；默认用 Transkun 钢琴专用模型，Basic Pitch 作为 Windows 兼容降级路径。
- PDF 乐谱由 Audiveris 光学识别（OMR）为 MusicXML，再经 MuseScore 导出 MIDI 与 A4 PDF；适合印刷清晰的电子版或扫描谱。
- MIDI 速度轨完整导出：渐慢/渐快以 `<sound tempo>` 保留回放，稳定速度平台在谱面标注节拍器记号（如 68 → 129）。
- 快速跑动按真实密度选择 32 分/64 分网格，同一网格点上的不同音高不再被错误压成和弦。
- 按键时长明显短于书写时值（约 60% 以内）的音自动标注跳音记号，不再写成孤立的短音符。
- Librosa 节拍跟踪把模型的秒级起止时间动态映射到拍点；重音列分析再决定 3/4 与 4/4 拍号和强拍相位，弱起进入会在制谱层重组为不完全小节；随后只删除亚帧或极弱短伪音，保留快速强起音并规范其释放，再清理重复音和同音重叠。
- 无调号事件时按滑动窗口做 Krumhansl 调性估计，并用 Viterbi 平滑与短段坍缩只在持续转调处改变调号，避免主属反复造成的调号抖动。
- 音频模式默认按证据自动启用三连音网格；连续快速二度交替识别为颤音记号；自由速度段落中的快速双音交替识别为震音记号；含休止符的三连音组保证括号完整，MuseScore 可直接载入。
- 支持五连音、六连音等多连音记谱，连音组按比率自然跨度精确闭合；孤立残缺的连音成员会自动改写为可载入的普通时值，杜绝"小节损坏"。
- 速度坡道自动标注 rit./accel. 文字记号，渐变速度同时以隐藏回放事件保留。
- 同声部重叠与转录微错位自动归并/截尾：转换永远产出结果，仅以质量提示（warning）告知折衷。
- 网页支持 `.wav/.flac/.mp3/.m4a/.ogg/.opus/.aac` 与 `.mp4/.mov/.mkv/.webm`，并可下载转录后的 MIDI；也可直接上传 `.musicxml/.xml/.mxl` 乐谱文件导出 A4 PDF 与 MIDI，或上传 `.pdf` 乐谱经 Audiveris 光学识别为 MusicXML 与 MIDI。

## 环境要求

- Python 3.10 或更高版本；
- Node.js 20 或更高版本，仅用于构建前端；
- MuseScore Studio 4，用于 A4 PDF 和精确 PNG 预览：`winget install Musescore.Musescore`
- FFmpeg，用于音视频转录时抽取标准 PCM 音频：`winget install Gyan.FFmpeg`
- 可选的 Audiveris（自带 Java 运行时），用于 PDF 乐谱的光学识别 —— 用 `scripts\install_omr.ps1` 自动安装；
- 可选的独立音频模型环境，推荐 NVIDIA CUDA 显卡运行 Transkun —— 用 `scripts\install_audio_backend.ps1` 自动安装（需要 Python 3.10：`winget install Python.Python.3.10`）。

Windows 默认自动查找：

```text
C:\Program Files\MuseScore 4\bin\MuseScore4.exe
```

如果安装在其他位置，可设置环境变量：

```powershell
$env:PIANO_MIDI_SCORE_MUSESCORE = "D:\Apps\MuseScore 4\bin\MuseScore4.exe"
```

## 快速启动

```powershell
git clone https://github.com/Chopin-Graham/piano-midi-score.git
cd piano-midi-score
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm install --prefix frontend
npm run build --prefix frontend
.\scripts\start.ps1 -NoInstall -NoBuild
```

访问 <http://127.0.0.1:8000>，上传 `.mid` 或 `.midi` 文件，或点击“试用内置示例”。页面可以直接下载 A4 PDF 和 MusicXML。

要开放给局域网其他设备访问（手机/平板看谱）：

```powershell
.\scripts\start.ps1 -Lan
```

脚本会列出本机的局域网地址，并提示需要管理员执行的防火墙放行命令。

要识别 PDF 乐谱（输出 MusicXML、MIDI 和重新排版的 A4 PDF），先安装 Audiveris：

```powershell
.\scripts\install_omr.ps1
```

随后重新启动服务，即可在同一上传框中选择 `.pdf` 乐谱。OMR 识别出的文字与符号（速度记号、力度、装饰音等）会直接保留；若识别结果的小节破损到任何打谱软件都无法加载，系统会自动把破损小节补齐/截齐，仍不行则按识别出的音符经语义流水线重新制谱——保证总能拿到可用的 MusicXML、MIDI 和 A4 PDF。复杂谱面仍建议下载 MusicXML 后在 MuseScore 中校对。

要启用音频/视频转录，先安装 FFmpeg，再建立隔离的模型环境：

```powershell
winget install Gyan.FFmpeg
.\scripts\install_audio_backend.ps1 -Backend transkun -TorchBuild cu121
```

随后重新启动服务，即可在同一上传框中选择音频或视频。程序会优先自动选择 Transkun + CUDA；没有 NVIDIA 显卡时可选择 CPU，或安装 Basic Pitch 降级后端：

```powershell
.\scripts\install_audio_backend.ps1 -Backend basic-pitch -TorchBuild cpu
```

也可以不启动网页，直接完成音视频到 MIDI、MusicXML、A4 PDF 的整条流水线：

```powershell
.\.venv\Scripts\python.exe scripts\transcribe_media.py `
  "C:\path\to\piano-performance.mp4" `
  --output "output\audio-video\my-performance" `
  --backend transkun --device cuda
```

音频转录默认采用三连音自动识别：只有相当比例的小节在三连音网格上的拟合显著优于二元网格时才启用，以避免模型时间误差造成伪三连音；确认原曲确有三连音时，也可在网页手动强制开启，或给命令行增加 `--allow-triplets`。

默认使用“简洁”模式，优先得到适合视奏的 2–6 小节系统；需要保留快速演奏中的三十二分细节时可切换到“均衡”或“忠实”。

## 验证

```powershell
.\.venv\Scripts\python.exe -m ruff check backend scripts
.\.venv\Scripts\python.exe -m pytest --cov
.\.venv\Scripts\python.exe scripts\benchmark.py
npm test --prefix frontend
npm run typecheck --prefix frontend
npm run build --prefix frontend
npm audit --prefix frontend --audit-level=moderate
```

当前验证基线（2026-08-20，含 OMR 文字层修复、三级回退与转录可读性修复后）：后端 167 项测试全部通过、总覆盖率 82%；Ruff 通过；前端 5 项测试、TypeScript 类型检查和生产构建通过。50 首 Mutopia 古典钢琴曲全量验收：50/50 转换成功、0 时值规格不匹配、0 连音括号失衡、50/50 通过 MuseScore 回读（起音 F1 中位数 1.000、均值 0.998、最低 0.965）。Animenz《Kawaki wo Ameku》《Unravel》官方 MIDI 制谱回环验收：MuseScore 回读起音 F1 分别为 0.976 / 0.998（0.1 拍容差，旧基线 0.048）。音频/视频全链路：转录 MIDI 经 MuseScore 回读的起音 F1 为 0.9967（0.12 拍容差，含弱起重组偏移校正），成谱合成音频与原录音的 CENS 色度相似度 0.82；29 个真实下载 MIDI 的全量语义门禁与历史基线一致（0 同声部重叠、0 谱表误放新增）。真实演奏视频转录（含踏板不净、自由速度）完整模式可直接通过 MuseScore 加载制谱；转录制谱的可读性修复：短休止符经缝隙吸收大幅减少、节拍网格的十六分级错位由小节相位搜索纠正、八分速度的旋律摆动不再误判为颤音。PDF 光学识谱（Audiveris）：50/50 语料成功识别，修复随附 Tesseract 语言包后文字/速度/力度记号可正常识别；破损小节经三级回退（直接雕版 → 小节配平 → 语义重排）全部可出谱，含商业排版的演奏官谱。

复杂回归样例位于 `artifacts/regression-expressive-piano.mid`，最终验收产物位于 `output/pdf/` 和 `artifacts/`。

## 质量边界

程序会阻止静默丢音、同声部时间重叠、明显的极端谱表误放和错误页面尺寸。默认按标准全尺寸键盘保守验收：八度以内为常规，九度为伸展，小/大十度为极限伸展并优先换手，十一度及以上不得作为单手同时和弦接受；即使总跨度只有八度，也会检查内声部能否分配给五个不同手指。持续音、通道级踏板覆盖和清理前的原始按键时值同样进入门禁。中等手型参考值以内为自然手型，轻微超出标为高难，超出约一个白键宽度仍无可行指序才进入人工复核。任意自由演奏 MIDI 的音乐语义并不总是唯一，因此无法数学保证所有输入都完全无需人工校订；对于自由速度、复杂复调、跨手演奏、滚奏意图或缺失踏板释放的素材，建议在下载 MusicXML 后做最后一次音乐性审核。

更多说明：

- [项目架构](docs/architecture.md)
- [谱面质量规则](docs/quality-rules.md)
- [完整钢琴制谱规则手册](docs/piano-notation-rulebook.md)
- [开源方案评估](docs/open-source-evaluation.md)
- [音视频转 MIDI 方案与使用说明](docs/audio-video-transcription.md)
- [三轮视觉验收记录](docs/visual-qa.md)
