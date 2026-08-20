# Piano MIDI Score

当前版本：`v0.3.7`（版本分级见 [版本与发布规则](docs/versioning.md)，变更记录见 [CHANGELOG](CHANGELOG.md)）。

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
- 提供 Leland 经典、Bravura 现代和 Leland 紧凑三种用户可选 A4 雕版风格；网页会分别解释“记谱整理”与“视觉雕版”的作用，默认推荐“简洁 + 经典出版”。
- 从力度统计自动规划谱面力度记号（相对本曲中位数映射 pp–ff、两小节最短持续、平值力度文件不标注），可手动关闭。
- MuseScore Studio 4 负责最终的 A4 间距、碰撞规避、分页与 PDF 导出。
- MuseScore 不可用时仍返回 MusicXML，并由 OpenSheetMusicDisplay 提供网页回退预览。
- 音频/视频通过 FFmpeg 抽取 44.1 kHz 单声道 PCM；默认用 Transkun 钢琴专用模型，Basic Pitch 作为 Windows 兼容降级路径。
- PDF 乐谱由 Audiveris 光学识别（OMR）为 MusicXML，再经 MuseScore 导出 MIDI 与 A4 PDF；适合印刷清晰的电子版或扫描谱。
- MIDI 速度轨完整导出：渐慢/渐快以 `<sound tempo>` 保留回放，稳定速度平台在谱面标注节拍器记号（如 68 → 129）。
- 快速跑动只在同一拍内出现连续快速起音证据时选择 32 分/64 分网格；稀疏攻击的 20–40 tick 演奏抖动不再诱导超细网格。
- 重复拍组中的相同起音结构会互相校验时值，修复个别音符的短暂延长/缩短；跳音只在重复节奏或局部连续短奏支持时标注，孤立短音保持短时值。
- Librosa 节拍跟踪把模型的秒级起止时间映射到拍点；音符密度只用于节奏证据，不再直接弯曲全曲时间轴。重音列分析再决定 3/4 与 4/4 拍号和强拍相位，弱起进入会在制谱层重组为不完全小节。
- 无调号事件时按滑动窗口做 Krumhansl 调性估计，并用 Viterbi 平滑与短段坍缩只在持续转调处改变调号，避免主属反复造成的调号抖动。
- 音频模式默认按证据自动启用三连音网格，约 5% 小节提供明确三分网格证据即可保留短小但真实的三/六连音段落；只有 32 分音符或更快、且按当前速度换算后交替间隔足够短的二度交替才识别为 `tr`，普通十六分交替保持逐音书写。
- 支持五连音、六连音等多连音记谱，连音组按比率自然跨度精确闭合；音频自动识别出的可见多连音必须至少有 3 个真实起音且发声音多于休止符，证据不足的完整组只保留为隐藏时序容器，以保证回放准确而不显示误导性的括号和数字。
- 同起音且原始释放接近的八度与三音以上和弦会统一书写时值，避免同一音柱被拆成互相冲突的声部、重复符头或不自然的错位时值；原本释放明显不同的复调音仍保持独立。
- 速度坡道自动标注 rit./accel. 文字记号，渐变速度同时以隐藏回放事件保留。
- 同声部重叠与转录微错位自动归并/截尾：转换永远产出结果，仅以质量提示（warning）告知折衷。
- 网页支持 `.wav/.flac/.mp3/.m4a/.ogg/.opus/.aac` 与 `.mp4/.mov/.mkv/.webm`，并可下载转录后的 MIDI；也可直接上传 `.musicxml/.xml/.mxl` 乐谱文件导出 A4 PDF 与 MIDI，或上传 `.pdf` 乐谱经 Audiveris 光学识别为 MusicXML 与 MIDI。
- 上传新文件时，网页会用文件名同步刷新乐谱标题与导出名称；标题、作者/编曲和下载文件名均可在生成前独立编辑，并写入 MusicXML 元数据与最终下载名。

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

随后重新启动服务，即可在同一上传框中选择 `.pdf` 乐谱。结构健康的 OMR 会直接保留识别出的文字与符号（速度记号、力度、装饰音等）；若检测到没有反复记号的伪结局线或严重小节时间轴越界，系统会先移除不安全结构，再按固定小节边界提取音符并重新制谱，避免一个坏小节把后续整首曲子的节奏拖偏。复杂谱面仍建议下载 MusicXML 后在 MuseScore 中校对。

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

音频转录默认采用三连音自动识别：只要约 5% 小节在三连音网格上的拟合明确优于二元网格，就会启用局部探测，以免把短小的真实三连音改写成切分；每个自动候选仍须有至少 3 个真实起音、充分的比率证据和明显的局部拟合优势。确认原曲确有三连音时，也可在网页手动强制开启，或给命令行增加 `--allow-triplets`。

默认使用“简洁”模式，优先得到适合视奏的 2–6 小节系统；需要保留快速演奏中的三十二分细节时可切换到“均衡”或“忠实”。

“简洁 / 均衡 / 忠实”控制节奏、时值和声部整理强度，不是字体主题；“经典出版 / 现代清晰 / 紧凑演奏”只控制字体、字号、间距和分页密度。一般推荐“简洁 + 经典出版”：它保留全部音头，同时优先消除转录抖动、碎休止和多余声部，并保持适合打印与屏幕阅读的标准留白。

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

当前验证基线（2026-08-20，含 OMR 时间轴、转录可读性、弱起相位、自动多连音、变速连续性、高密度跑动、三拍子判定、全页预览与尾部小节边界修复）：后端 218 项测试全部通过、总覆盖率 74%；Ruff 通过；前端 6 项测试、TypeScript 类型检查和生产构建通过。50 首 Mutopia 古典钢琴曲全量验收：50/50 转换成功、0 时值规格不匹配、0 连音括号失衡、50/50 通过 MuseScore 回读（起音 F1 中位数 1.000、均值 0.998、最低 0.965）。Animenz《Kawaki wo Ameku》《Unravel》官方 MIDI 制谱回环验收：MuseScore 回读起音 F1 分别为 0.997 / 0.999（0.1 拍容差，旧基线 0.048）。音频/视频全链路：转录 MIDI 经 MuseScore 回读的起音 F1 为 0.9967（0.12 拍容差，含弱起重组偏移校正），成谱合成音频与原录音的 CENS 色度相似度 0.82；29 个真实下载 MIDI 的全量语义门禁与历史基线一致（0 同声部重叠、0 谱表误放新增）。真实演奏视频转录（含踏板不净、自由速度）完整模式可直接通过 MuseScore 加载制谱；转录制谱的可读性修复：短休止符经缝隙吸收大幅减少、节拍网格的十六分级错位由小节相位搜索纠正、八分速度的旋律摆动不再误判为颤音。PDF 光学识谱（Audiveris）：Kawaki 官谱样例识别出 83 个伪结局线和 6 个严重时间轴异常；按小节共享游标重建后，相对官方 MIDI 的起音 F1 为 0.8626（0.24 拍容差），且后续小节不再累计漂移。

`v0.3.7` 将网页默认预览改为全部 A4 页面。后端保留兼容用的首图字段，同时返回按数字页码排序的完整 PNG 页面数组；前端逐页连续展示并对后续页延迟解码。真实 7 页 MusicXML 回归返回并显示 7/7 页，首图与旧字段保持一致。

`v0.3.6` 修复三拍子被二拍候选错误否决的问题。二拍候选的候选小节线更密，累计重音分数天然较高；现在它只有在三元细分同时支持复合拍时才参与 6/8 判定，不再压制已经显著优于 4/4 的 3/4 候选。下载目录真实视频 `197357121-1-208.mp4` 从错误的 4/4、192 小节修复为 3/4、256 小节，动态速度仍为 172.266 BPM，1857/1857 个音符全部保留。

`v0.3.5` 的真实视频专项回归继续使用下载目录中的 `苍鸟.mp4`：当前纯音频候选以 129.199 BPM 胜出，自适应候选因节拍层级跳变与反转过多被拒绝；默认模式保留 3908/3908 个音符。旧量化路径在快速上下文中产生 20 组由不同起音挤成的短音程双音，联合起音量化后降为 0；13 个每拍含 7 个以上起音的片段全部保留原始起音列，最密片段为 10/10。高阶加花使用可打印的 32/64 分二元网格近似，不伪造无法精确闭合的 7/9 连音，也不靠补休止符把音强塞进一拍。补丁版另修复不能整除拍组的五连音网格把尾音舍入到最后小节之外的问题；网页可选的 12 组多连音节奏组合均未越界，末拍连续音也不会因尾部保护而合成假双音。参考谱约 120 小节且以完整 4/4 开始，因此当前 129.199 BPM / 182 小节与半小节弱起仍记录为独立的 3:2 节拍层级、片头静音判定问题，不能视为参考谱级校对完成。

`v0.3.3` 的真实视频专项回归使用 `40869366033-1-192.mp4`：旧结果把 4/4 第三拍的较重低音误当强拍，形成三拍弱起（相位 3.75）；修复后选择相位 1.75，首小节为一拍弱起并写成 `implicit=yes`。最终 MusicXML/MuseScore 回转保留 2960/2960 个音符，起音 F1 为 0.9939、平均起音误差为 0.02229 拍；统一 106 个同起八度/和弦音的书写时值。谱面只显示 29 个有真实起音支持的多连音，83 个证据不足但回放必需的组隐藏为技术时序容器；可见组中 0 个休止符占多数、0 个少于三个发声音。可见休止符为 102，连音括号失衡为 0，MuseScore 正常回读并输出 8 页 A4 PDF。

`v0.3.0` 的 Kawaki 演奏转录专项回归使用 `rawtest` 官谱/MIDI与缓存原始转录交叉检查：旧 IOI 时间轴输出 76→129→113→129→186→129 BPM 的伪速度段，回放仅 245.06 秒；修复后输出稳定 129.199 BPM，回放 259.54 秒（原始转录 258.54 秒）。自动网格为 3 个八分、121 个十六分和 13 个三十二分小节，不再生成问题产物中的 656 个连音标签；236 个跳音元素降至 23 个有上下文支持的跳音列，错误 `tr` 从 1 处降至 0，MuseScore 可直接载入并以 0.989 起音 F1 回读。

复杂回归样例位于 `artifacts/regression-expressive-piano.mid`，最终验收产物位于 `output/pdf/` 和 `artifacts/`。

## 质量边界

程序会阻止静默丢音、同声部时间重叠、明显的极端谱表误放和错误页面尺寸。默认按标准全尺寸键盘保守验收：八度以内为常规，九度为伸展，小/大十度为极限伸展并优先换手，十一度及以上不得作为单手同时和弦接受；即使总跨度只有八度，也会检查内声部能否分配给五个不同手指。持续音、通道级踏板覆盖和清理前的原始按键时值同样进入门禁。中等手型参考值以内为自然手型，轻微超出标为高难，超出约一个白键宽度仍无可行指序才进入人工复核。任意自由演奏 MIDI 的音乐语义并不总是唯一，因此无法数学保证所有输入都完全无需人工校订；对于自由速度、复杂复调、跨手演奏、滚奏意图或缺失踏板释放的素材，建议在下载 MusicXML 后做最后一次音乐性审核。

更多说明：

- [项目架构](docs/architecture.md)
- [版本与发布规则](docs/versioning.md)
- [变更记录](CHANGELOG.md)
- [谱面质量规则](docs/quality-rules.md)
- [完整钢琴制谱规则手册](docs/piano-notation-rulebook.md)
- [开源方案评估](docs/open-source-evaluation.md)
- [音视频转 MIDI 方案与使用说明](docs/audio-video-transcription.md)
- [三轮视觉验收记录](docs/visual-qa.md)
