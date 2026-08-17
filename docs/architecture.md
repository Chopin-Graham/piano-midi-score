# 项目架构

## 数据流

```text
音频 / 视频
  -> FFmpeg 44.1 kHz mono PCM
  -> Transkun（主）/ Basic Pitch（降级）
  -> 亚帧/极弱伪音、重复音、同音重叠清理；强短音保留起音
  -> Librosa 动态节拍映射
  -> 标准钢琴 MIDI

MIDI（上传或转录所得）
  -> mido 事件解析
  -> 每四分音符 480 divisions 的统一时间轴
  -> 变量小节时间轴与复合拍号拍组
  -> 按小节自适应量化
  -> 轨道证据 / 动态规划物理双手分配
  -> 独立的高低音谱表分配
  -> 原始按键时值的物理可演奏性快照
  -> 演奏时值可读性规范化
  -> Partitura Chew–Wu 声部分离
  -> Partitura PS13 音高拼写
  -> 动态谱号与 8va/8vb 规划
  -> 语义质量门禁
  -> MusicXML 4.0
  -> MuseScore Studio 4 A4 雕版
  -> PDF + 首屏 PNG + MusicXML
```

MuseScore 不可用时，流程在 MusicXML 处安全降级，网页使用 OpenSheetMusicDisplay 渲染回退预览。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `midi_parser.py` | 解析音符、轨道、通道、速度、速度变化、拍号、调号和 CC64 踏板事件 |
| `quantizer.py` | 逐小节比较二分、三连音等候选网格，兼顾时间误差与谱面复杂度 |
| `hand_splitter.py` | 物理双手分配、五指/大十度门禁、通道级踏板覆盖、持续音前瞻与回溯修复 |
| `staff_assigner.py` | 独立选择高低音谱表，最小化加线、和弦拆分和频繁换谱表 |
| `piano_rules.py` | 标准钢琴音域、手距、琴键几何、五指规则和踏板覆盖区间的单一事实来源 |
| `duration_simplifier.py` | 将踏板或演奏造成的轻微重叠规范成可写时值；音频模式还把碎小释放间隙延至下一起音/节拍边界，始终保留全部音头 |
| `voices.py` | 以 Partitura Chew–Wu 结果作为旋律路径提示，再做无重叠区间打包 |
| `spelling.py` | 使用 Partitura PS13 生成上下文相关的音名和临时升降号 |
| `clefs.py` | 在小节边界或必要的小节内部规划高音/低音谱号变化，检查跨边界持续音并抑制频繁切换 |
| `ottava.py` | 检测真正处于同一极端音区的乐句，规划 8va/8vb 与少量 15ma/15mb；过短或混合音区素材保留加线 |
| `quality.py` | 检查音符数、同声部重叠、极端谱表误放，以及清理前原始持续音的不可演奏跨度 |
| `musicxml.py` | 生成双谱表 MusicXML、休止符、连音线、踏板线、谱号变化、八度线和显式系统提示；用 `forward/backup` 精确锚定小节中部事件 |
| `engraver.py` | 调用 MuseScore CLI，应用用户选择的雕版样式，验证 A4、页数和每系统小节数，生成 PDF/PNG |
| `media_transcription.py` | FFmpeg 音频抽取、Transkun/Basic Pitch 调度、节拍估计、转录音符清理与标准 MIDI 输出 |
| `audio_worker.py` | 在隔离音频环境中运行 Librosa 节拍分析，避免模型依赖污染主服务环境 |
| `main.py` | FastAPI 上传边界、参数校验、线程池执行、Base64 输出与前端托管 |

## 左右手分配

轨道不是天然的“手”。程序只在以下情况下硬采用轨道：

1. 两个主要轨道分别明确命名为 Right Hand / Left Hand、RH / LH、右手 / 左手等；
2. 恰好两个主要轨道的中位音高、分位音高与音域间隔都显示它们确实分离。

其余输入按同时起音聚类，枚举每个和弦的分割位置，再以动态规划综合优化：

- 左右手舒适音域；
- 单手和弦跨度；
- 相邻起音之间的手部连续性；
- 宽音域和弦应分给两手；
- 极低音不得进入高音谱表，极高音不得进入低音谱表。

物理手不会再由谱号强制决定。和弦先满足每手最多 5 键、最大大十度，再检查仍在按住的旧音；必要时对早先持续音做回溯换手。只有旧音所在通道的 CC64 从冲突点连续覆盖到旧音结束时，分手器才允许释放该手指。随后 `staff_assigner.py` 才根据加线代价选择谱表，因此右手可以在低音谱表、左手也可以在高音谱表。

## 声部与时值

Partitura 的 Chew–Wu 算法提供旋律路径提示。项目自己的区间打包层只负责把互不重叠的路径合并为可写声部；当音乐确实需要更多声部时会完整保留并发出提示，不会为了满足配置上限而截短或删除音符。

在声部分离前，时值规范化只处理演奏型重叠：同一声部候选的音符若因踏板或按键释放稍晚而覆盖后一个起音，会把前一音结束位置对齐到合理重触点。音头数量在此过程中必须保持不变。质量门禁另行保留规范化前的物理时值快照，因此这一步不能把原本不可达的持续手距洗成“可演奏”。

## 八度线语义

八度线不是单纯的视觉标签。检测到 8va/8vb 后，MusicXML 中的书写音高会同步移写，确保“书写音高 + 八度线”仍等于原始 MIDI 音高。小节中部的开始与停止位置通过 MusicXML 时间光标写入，不依赖 MuseScore 对 `offset` 的不完整处理；跨系统续线使用 `(8va)/(8vb)`，并保留虚线和末端挂钩。

候选区间必须满足：该谱表在区间内所有发声音都属于同一极端音区。单一事件只有持续至少一个四分音符、且高音达到 MIDI 92 或低音接近 MIDI 24 时才使用短八度线。八分音符级别的瞬时极端和弦保留加线，因为 MuseScore 会把过短八度线压缩成孤立标签，反而容易误读。

为让标签落在自然乐句开端，已经合格的八度线可以向同一高低音带内回溯最多一拍的引子音；资格判断仍只看原始极端核心，因此引子不能通过增加音头数制造新八度线。若同一方向连续出现至少三段、每段不超过 1.5 拍的短片段，则整组改用加线，避免每小节重复一个 `8`。三套 MuseScore 样式同时为八度线文本、音头和横梁保留额外安全距离。

## 排版策略

MusicXML 按雕版后的节奏列、谱表列、最大和弦音头数与并发声部数估算系统宽度，而不是按原始 MIDI 音符数粗略计数。普通钢琴织体通常得到每系统 2–6 小节，稀疏引子最多 8 小节；真正高密度小节可以独占系统。末系统若只剩一个普通小节，会在宽度允许时与前一系统重新平衡。MuseScore 首次渲染后还会读取 measure-position 数据；只有候选版页数不增加且低密度孤行或页面平衡严格改善时，才接受第二次重排。

三种雕版风格使用不同系统容量：`classic=27`、`modern=25`、`compact=30`。程序只给出换系统提示，不提前强制分页；MuseScore 根据实际系统高度自动分页，避免自动换行与预设分页叠加后产生空白页。

MuseScore 使用三套 A4 样式完成最终雕版：

- `classic`：Leland 乐符 + Edwin 文字，作为默认出版风格；
- `modern`：Bravura 乐符 + Edwin 文字，间距略宽；
- `compact`：Leland 乐符 + Edwin 文字，较小谱表与更紧凑页边距。

对应资源为 `piano_a4.mss`、`piano_a4_modern.mss` 和 `piano_a4_compact.mss`。集成只调用 MuseScore 4.6 仍支持的 `-r`、`-S`、`-j` 参数，不依赖已移除的旧版 `-s/-m/-w` 参数。渲染有 90 秒超时，失败时保留 MusicXML，不让请求无限挂起。

## API 输出

`POST /api/convert` 返回：

- `musicxml` 与建议文件名；
- `pdf_base64` 与 A4 PDF 文件名（MuseScore 可用时）；
- `preview_png_base64`，与 PDF 第一页一致；
- `analysis`，包含量化、左右手、声部、音高拼写、质量与雕版指标；
- `warnings`，用于提示输入歧义或安全降级。

核心转换保持确定性；Web 层只负责 I/O 和并发边界。

`POST /api/convert-media` 额外接收 `transcription_options_json`，支持后端、CPU/CUDA、节拍对齐和短音阈值选择；响应除上述内容外还返回 `midi_base64`、中间 MIDI 文件名及 `analysis.transcription`。上传上限为 250 MB，模型任务有一小时保护超时。

## 目录

- `backend/app/core/`：转换与雕版领域层；
- `backend/app/resources/`：MuseScore 样式和导入配置；
- `backend/tests/`：算法、格式、API 与性能测试；
- `frontend/`：React 界面、精确 PNG 预览与 OSMD 回退；
- `scripts/`：启动、样例生成和基准测试；
- `output/audio-video/`：完整音视频转录的 MIDI、MusicXML、PDF 和 JSON 报告；
- `artifacts/`：回归 MIDI、MusicXML 与预览图；
- `artifacts/actual-reference-comparison.md`：三份真实参考谱共 354 小节的逐小节对比报告；
- `tmp/pdfs/live-review/`：当前候选 PDF、MusicXML 与逐页视觉验收 PNG；
- `output/pdf/`：最终 A4 PDF 验收产物。
