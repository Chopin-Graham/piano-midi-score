# 开源方案评估

本项目把“从演奏推断乐谱语义”和“专业雕版”拆成两层，分别复用成熟开源项目，而不是重新实现整套声部分离、音高拼写和排版引擎。

## 采用的项目

| 项目 | 版本 | 许可证 | 在本项目中的职责 |
| --- | --- | --- | --- |
| [Partitura](https://github.com/CPJKU/partitura) | 1.9.0 | Apache-2.0 | Chew–Wu 声部分离、PS13 音高拼写 |
| [MuseScore Studio](https://github.com/musescore/MuseScore) | 4.6.3 验证 | GPL-3.0 | MusicXML 导入、碰撞规避、A4 系统布局、PDF/PNG 导出；作为外部 CLI 运行 |
| [OpenSheetMusicDisplay](https://github.com/opensheetmusicdisplay/opensheetmusicdisplay) | 2.1.2 | BSD-3-Clause | MuseScore 不可用时的浏览器 MusicXML 回退预览 |
| [Transkun](https://github.com/Yujia-Yan/Transkun) | 2.0.1 | MIT | 钢琴专用音频转 MIDI 主后端，保留复音音高、起止时间、力度与踏板信息 |
| [Basic Pitch](https://github.com/spotify/basic-pitch) | 0.4.0 | Apache-2.0 | 轻量、跨平台的音频转 MIDI 降级后端 |
| [Librosa](https://github.com/librosa/librosa) | 0.11.0 | ISC | 起音包络与节拍位置估计，用于动态拍点映射 |
| [FFmpeg](https://ffmpeg.org/) | 8.x 验证 | LGPL/GPL（依构建选项） | 从常见音视频容器稳定抽取 44.1 kHz 单声道 PCM |

底层 MIDI 事件解析使用 [mido](https://github.com/mido/mido)，但时间量化、分手、物理门禁和谱表决策属于本项目需要保留可解释性的领域逻辑。

## 音频/视频转 MIDI 的选择

Transkun 作为主路径，因为它针对钢琴复音转录设计，能够直接生成包含踏板与力度的 MIDI，适合本项目“钢琴独奏录音 -> 可编辑演奏事件”的输入假设。模型运行在独立 `.venv-audio` 环境中；Windows 上用官方 wheel 的推理路径，不安装只服务于评测、且缺少 Windows wheel 的 `ncls`。PyTorch/CUDA 与主 FastAPI 环境隔离，避免把数 GB 模型依赖强加给只使用 MIDI 制谱的用户。

Basic Pitch 作为兼容降级方案。它安装更直接、跨平台性较好，并开放 onset/frame 阈值；但它是通用复音转录器，不专门针对高密度钢琴踏板织体，因此自动模式始终优先 Transkun。两种模型都只负责产生秒级演奏事件，本项目不接受其原始 MIDI 直接作为最终谱面。

后处理固定执行以下步骤：

1. 删除钢琴 88 键范围外音符，以及亚帧或极弱的短伪起音；力度明确的 30–50 ms 快速音保留起音，只把释放规范到最小时长；
2. 合并几乎同时的同音重复检测，并修复同音音符时间重叠；
3. 用 Librosa 找到非等距节拍位置，把秒级时间分段线性映射到整数拍；
4. 写出统一 480 ticks/quarter、4/4 初始拍号的标准 MIDI；
5. 交给现有自适应量化、分手、谱表、声部、谱号、八度线和 A4 雕版流程。

这套分层让识别模型可以替换，而制谱质量规则保持一致。若节拍跟踪失败，程序明确警告并保留恒定速度时间线，不会静默制造错误拍点。

没有把 [Demucs](https://github.com/facebookresearch/demucs) 作为纯钢琴视频的默认前处理。它适合从完整混音中尝试提取 piano stem，但分离结果可能残留人声/鼓声串音，也可能削弱钢琴起音；这些伪影会直接放大为错误 MIDI 音符。对已经是钢琴独奏的参考视频，绕过分离能保留更可靠的瞬态。对于乐队混音，Demucs 可作为人工试听后的可选预处理，但当前程序不会把不可靠的 stem 自动伪装成演奏级钢琴谱。

## 可演奏性参考：PianoPlayer

[PianoPlayer](https://github.com/marcomusy/pianoplayer) 3.0.2（MIT）使用五指组合搜索、XXS–XXL 手型、黑键偏置、指序约束和动态移动速度代价。项目已经按其思路补充琴键几何、五指、持续音和快速移动门禁，并复用其“拇指可达范围较大、内指相邻距离较小”的转移模型，枚举左右手单调指序以识别总跨度合格但内部无法成形的和弦。

当前不直接把 PianoPlayer 当作运行时唯一裁决器，原因是它对单 Part 双谱表默认采用 `RH=staff 1 / LH=staff 2` 路由，而本项目明确允许物理右手写在低音谱表；它自身也明确说明指法因人而异，不存在普遍唯一的最佳答案。现阶段只复用可解释的物理约束与低移动代价思想，继续由本项目独立保存 `hand` 和 `staff`，避免给出错误的“唯一最佳指法”。

## 为什么不直接让 MuseScore 导入 MIDI

MuseScore 的 MIDI 导入适合交互式使用，但对单轨混合、带踏板和表达性时间偏差的钢琴演奏，需要依赖导入面板中的人工选择。回归样例实测直接导入后出现了错误谱表归属、过多临时升降号和跨谱表拥挤。

因此本项目不把 MuseScore 当作 MIDI 语义推断器，而是先生成经过质量门禁的 MusicXML，再让 MuseScore 专注于它更成熟的雕版能力。这样可以：

- 在进入渲染器前检查是否丢音或声部重叠；
- 用确定性算法控制左右手和量化；
- 保留 MuseScore 的字体、间距、碰撞规避和分页优势；
- 让 MusicXML 成为可检查、可编辑的稳定中间格式。

八度线同样由语义层先规划，再交给 MuseScore 雕版。项目负责确认作用区间、同步移写书写音高，并用 MusicXML 时间光标精确写入开始/停止点；MuseScore 负责虚线、挂钩、碰撞规避和跨系统续线。这避免重新实现图形排版，也避免把一个视觉标签误当成完整音乐语义。

## Partitura 的使用边界

Chew–Wu 输出作为旋律路径提示，最终仍需按谱表做无重叠区间打包，因为演奏时值可能带有踏板重叠。PS13 输出的是上下文音高拼写，项目再将结果写入 MusicXML。若 Partitura 对异常输入抛出可预期的分析错误，程序会回退到安全分配并返回警告，不会删除音符。

本地参考实现位于安装环境中的：

```text
.venv/Lib/site-packages/partitura/musicanalysis/voice_separation.py
.venv/Lib/site-packages/partitura/musicanalysis/pitch_spelling.py
```

## MuseScore CLI 兼容性

MuseScore 4.6 已移除旧手册中部分 MIDI 导入参数。当前集成只使用 4.6.3 源码仍支持的 `-r`、`-S`、`-j` 参数，并通过 JSON job 同时导出 PDF、PNG 与 measure-position 文件。

参考资料：

- [MuseScore command-line usage](https://handbook.musescore.org/appendix/command-line-usage.md)
- [MuseScore 4.6.3 command line parser source](https://github.com/musescore/MuseScore/blob/v4.6.3/src/app/internal/commandlineparser.cpp)

MuseScore 不作为 Python 库链接进后端，而是用户本机安装的外部程序。发布或重新分发 MuseScore 本体时，仍需单独遵守其 GPL-3.0 许可证要求。

## 未作为主路径的替代方案

| 方案 | 优点 | 未作为当前主路径的原因 |
| --- | --- | --- |
| MuseScore 直接导入 MIDI | 交互方便、已有导入面板 | 无头批处理难以固定导入选项；单轨混合与踏板素材需要人工选择 |
| music21 | 音乐理论分析和 MusicXML 操作成熟 | 不提供面向表达性钢琴 MIDI 的完整分手、物理手型和出版级分页流水线 |
| pretty_midi | MIDI 特征提取简洁 | 主要是事件与分析层，不解决制谱语义和雕版 |
| Verovio / OSMD | 浏览器 SVG 渲染方便 | 适合预览；当前 A4 PDF、字体样式、分页和 measure-position 验证由 MuseScore CLI 更完整地覆盖 |
| LilyPond | 出版级雕版质量很高 | 需要额外维护 LilyPond 语义生成器；MusicXML + MuseScore 更适合用户继续编辑，也已有稳定 CLI 验证链 |
| Onsets and Frames / Magenta | 经典钢琴转录体系、研究资料丰富 | 官方工程栈较旧且部署重量较高；当前 Windows/CUDA 批处理由 Transkun 更容易隔离和维护 |
| Demucs 自动 piano stem | 能处理含其他乐器的混音 | stem 串音和起音损伤会转化为伪音；只作为需人工试听的可选前处理 |

选择标准不是“哪个项目功能最多”，而是让每个成熟组件承担它最擅长的部分：Partitura 处理音乐语义提示，MuseScore 处理雕版，OSMD 处理浏览器回退，本项目只实现钢琴 MIDI 转谱特有且必须可测试的决策层。

## 下载目录真实参考谱校准

当前校准集来自 `C:\Users\ZbXiao\Downloads\参考谱面`，包含 Hanezeve Caradhina、STYX HELIX 和 Unravel 的成对 MIDI/PDF。程序把参考 PDF 的页面与系统位置、参考 MIDI 的上下谱表轨道，以及最终 `ScoreModel`/MusicXML 逐小节对齐，共覆盖 354 小节。参考谱只用于归纳出版规则，不复制受版权保护的具体版面。

本轮从参考谱吸收并固化了以下规则：

- 物理左右手与最终谱表必须分开判断；可信上下轨道是强证据，但不能阻止必要的跨谱表书写；
- 调号、谱号和系统密度都是时间线，不能用全曲单一估计或固定每行小节数代替；
- 八度线可以向同一高低音区内最多一拍的自然引子回溯，但引子只能改善已经合格的乐句起点，不能制造新八度线；
- 三段以上相邻、每段不超过 1.5 拍的短八度片段改用加线，避免每小节重复孤立的 `8`；
- 密集复调必须获得更宽系统，末页允许较短，但普通密度下不能出现单小节孤行。

最新经典风格输出分别为 4 页/23 系统、8 页/46 系统和 8 页/41 系统，全部为 A4、0 个单小节系统、0 个自动复核小节。谱表一致率分别为 99.40%、99.68%、99.53%，手一致率分别为 98.19%、96.95%、99.09%。完整逐小节结果保存在 `artifacts/actual-reference-comparison.md` 与同名 JSON 中；当前可监看 PDF 位于 `tmp/pdfs/live-review/`。
