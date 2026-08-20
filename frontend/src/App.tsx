import { ChangeEvent, DragEvent, useMemo, useState } from "react";

import packageMetadata from "../package.json";
import { convertDemo, convertMedia, convertMidi } from "./api";
import { ScorePreview } from "./components/ScorePreview";
import {
  classifyInputFilename,
  defaultScoreMetadata,
  FILE_INPUT_ACCEPT,
  normalizeOutputFilename,
  uploadLimitBytes,
} from "./lib/files";
import { downloadBase64, downloadText, formatBytes, formatKey } from "./lib/format";
import type {
  ConversionOptions,
  ConversionResponse,
  EngravingStyle,
  MinimumNote,
  ScoreStyle,
  TranscriptionBackend,
  TranscriptionDevice,
  TranscriptionOptions,
} from "./types";

const DEFAULT_OPTIONS: ConversionOptions = {
  style: "clean",
  engraving_style: "classic",
  minimum_note: "auto",
  allow_triplets: true,
  hand_split: "auto",
  prefer_track_hints: true,
  max_voices_per_staff: 2,
  include_pedal: true,
  include_dynamics: true,
  infer_key: true,
  time_numerator: null,
  time_denominator: null,
  title: null,
  author: null,
  output_filename: null,
};

const DEFAULT_TRANSCRIPTION_OPTIONS: TranscriptionOptions = {
  backend: "auto",
  device: "auto",
  align_beats: true,
  minimum_note_ms: 55,
  onset_threshold: 0.5,
  frame_threshold: 0.3,
};

const SCORE_STYLE_GUIDANCE: Record<ScoreStyle, { title: string; detail: string }> = {
  clean: {
    title: "推荐 · 简洁",
    detail: "优先消除转录抖动、碎休止与多余声部，最适合音频、视频和直接打印。",
  },
  balanced: {
    title: "均衡",
    detail: "保留更多演奏细节，同时做适度节奏整理，适合质量较好的 MIDI。",
  },
  faithful: {
    title: "忠实",
    detail: "尽量保留原始时值与声部，便于专业复核，但谱面可能更密、更碎。",
  },
};

const ENGRAVING_STYLE_GUIDANCE: Record<
  EngravingStyle,
  { title: string; detail: string }
> = {
  classic: {
    title: "推荐 · 经典出版",
    detail: "标准字号与留白，打印和屏幕阅读都稳定，适合大多数钢琴谱。",
  },
  modern: {
    title: "现代清晰",
    detail: "符号更醒目、间距略宽，适合屏幕阅读或初学者查看。",
  },
  compact: {
    title: "紧凑演奏",
    detail: "缩小字号和间距以减少页数，适合长曲；复杂多声部可能显得拥挤。",
  },
};

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [options, setOptions] = useState<ConversionOptions>(DEFAULT_OPTIONS);
  const [transcriptionOptions, setTranscriptionOptions] =
    useState<TranscriptionOptions>(DEFAULT_TRANSCRIPTION_OPTIONS);
  const [result, setResult] = useState<ConversionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const inputKind = file ? classifyInputFilename(file.name) : null;
  const isMedia = inputKind === "media";
  const isPdf = inputKind === "pdf";
  const fixedSplit = typeof options.hand_split === "number";
  const outputStem = normalizeOutputFilename(
    options.output_filename,
    file ? defaultScoreMetadata(file.name).outputFilename : "score",
  );
  const previewTitle = result?.analysis.title ?? options.title ?? "等待生成";
  const previewAuthor = result?.analysis.author ?? options.author;

  const canConvert = Boolean(file) && !loading;
  const keyLabel = useMemo(() => {
    if (!result?.analysis.key) return "—";
    return formatKey(result.analysis.key.tonic_pitch_class, result.analysis.key.mode);
  }, [result]);
  const qualityLabel = result?.analysis.quality
    ? {
        excellent: "优秀",
        playable_but_demanding: "可弹·高难",
        needs_review: "需复核",
      }[result.analysis.quality.status]
    : "—";

  function chooseFile(nextFile: File | null) {
    if (!nextFile) return;
    const nextKind = classifyInputFilename(nextFile.name);
    if (!nextKind) {
      setError("请选择 MIDI、MusicXML、PDF 乐谱或常见音视频文件");
      return;
    }
    if (nextFile.size > uploadLimitBytes(nextKind)) {
      setError(
        nextKind === "media"
          ? "音视频文件不能超过 250 MB"
          : nextKind === "pdf"
            ? "PDF 文件不能超过 50 MB"
            : "MIDI 文件不能超过 10 MB",
      );
      return;
    }
    setFile(nextFile);
    setResult(null);
    setError(null);
    const metadata = defaultScoreMetadata(nextFile.name);
    setOptions((current) => ({
      ...current,
      title: metadata.title,
      output_filename: metadata.outputFilename,
      allow_triplets: nextKind === "media" ? false : current.allow_triplets,
      include_pedal: nextKind !== "media",
    }));
  }

  function onFileInput(event: ChangeEvent<HTMLInputElement>) {
    chooseFile(event.target.files?.[0] ?? null);
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files?.[0] ?? null);
  }

  async function runConversion() {
    if (!file) return;
    setLoading(true);
    setError(null);
    try {
      setResult(
        isMedia
          ? await convertMedia(file, options, transcriptionOptions)
          : await convertMidi(file, options),
      );
    } catch (conversionError) {
      setResult(null);
      setError(conversionError instanceof Error ? conversionError.message : "转换失败");
    } finally {
      setLoading(false);
    }
  }

  async function runDemo() {
    setLoading(true);
    setError(null);
    setFile(null);
    const demoOptions = {
      ...options,
      title: "Piano Demo",
      output_filename: "demo-piano",
    };
    setOptions(demoOptions);
    try {
      setResult(await convertDemo(demoOptions));
    } catch (conversionError) {
      setResult(null);
      setError(conversionError instanceof Error ? conversionError.message : "示例转换失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark" aria-hidden="true">♩</div>
        <div>
          <h1>Piano MIDI Score</h1>
          <p>从 MIDI、音频或视频整理出真正可读的钢琴谱</p>
        </div>
        <div className="privacy-pill">
          v{packageMetadata.version} · 本地处理 · A4 PDF + MusicXML
        </div>
      </header>

      <main className="workspace">
        <aside className="control-panel">
          <section>
            <div className="section-heading">
              <span>01</span>
              <div><h2>选择输入文件</h2><p>MIDI 直接制谱，音视频先转录为 MIDI</p></div>
            </div>
            <label
              className={`drop-zone ${dragging ? "dragging" : ""} ${file ? "has-file" : ""}`}
              onDragEnter={() => setDragging(true)}
              onDragLeave={() => setDragging(false)}
              onDragOver={(event) => event.preventDefault()}
              onDrop={onDrop}
            >
              <input type="file" accept={FILE_INPUT_ACCEPT} onChange={onFileInput} />
              <span className="upload-icon">↥</span>
              {file ? (
                <>
                  <strong>{file.name}</strong>
                  <small>{formatBytes(file.size)} · 点击可更换</small>
                </>
              ) : (
                <>
                  <strong>拖入文件或点击选择</strong>
                  <small>MIDI 10 MB · PDF 乐谱 50 MB · 音视频 250 MB</small>
                </>
              )}
            </label>
            <button type="button" className="demo-button" disabled={loading} onClick={runDemo}>
              没有 MIDI？试用内置示例
            </button>
          </section>

          <section className="metadata-section">
            <div className="section-heading">
              <span>02</span>
              <div><h2>作品信息</h2><p>标题写入乐谱，文件名用于下载</p></div>
            </div>
            <div className="metadata-card">
              <label className="field metadata-title-field">
                <span>乐谱标题</span>
                <input
                  type="text"
                  maxLength={120}
                  value={options.title ?? ""}
                  placeholder="选择文件后自动同步"
                  onChange={(event) =>
                    setOptions({ ...options, title: event.target.value || null })
                  }
                />
                <small className="field-hint">选择新文件时会自动更新，之后仍可自由修改。</small>
              </label>
              <div className="metadata-grid">
                <label className="field">
                  <span>作者 / 编曲</span>
                  <input
                    type="text"
                    maxLength={120}
                    value={options.author ?? ""}
                    placeholder="例如：F. Chopin"
                    onChange={(event) =>
                      setOptions({ ...options, author: event.target.value || null })
                    }
                  />
                </label>
                <label className="field">
                  <span>导出文件名</span>
                  <input
                    type="text"
                    maxLength={120}
                    value={options.output_filename ?? ""}
                    placeholder="例如：夜曲-最终版"
                    onChange={(event) =>
                      setOptions({
                        ...options,
                        output_filename: event.target.value || null,
                      })
                    }
                  />
                </label>
              </div>
              <div className="output-preview" aria-live="polite">
                <span>导出预览</span>
                <strong>{outputStem}.musicxml</strong>
                <small>{outputStem}-A4.pdf</small>
              </div>
            </div>
          </section>

          {isPdf && (
            <section className="transcription-section">
              <div className="section-heading">
                <span>03</span>
                <div><h2>PDF 乐谱识别</h2><p>光学识别（OMR）为 MusicXML 与 MIDI</p></div>
              </div>
              <div className="transcription-notice">
                PDF 由本机 Audiveris 引擎识别；印刷清晰的乐谱效果最佳。识别结果会同时生成 A4
                排版 PDF 与 MIDI，复杂谱面建议在 MuseScore 中做最后校对。
              </div>
            </section>
          )}

          {isMedia && (
            <section className="transcription-section">
              <div className="section-heading">
                <span>03</span>
                <div><h2>音频转录</h2><p>钢琴专用模型 + 动态节拍对齐</p></div>
              </div>
              <div className="transcription-notice">
                推荐使用 Transkun + CUDA。完整视频可能需要数分钟，转录后可下载中间 MIDI。
              </div>
              <div className="two-column-fields">
                <label className="field">
                  <span>识别引擎</span>
                  <select
                    value={transcriptionOptions.backend}
                    onChange={(event) =>
                      setTranscriptionOptions({
                        ...transcriptionOptions,
                        backend: event.target.value as TranscriptionBackend,
                      })
                    }
                  >
                    <option value="auto">自动（优先 Transkun）</option>
                    <option value="transkun">Transkun · 钢琴专用</option>
                    <option value="basic_pitch">Basic Pitch · 兼容降级</option>
                  </select>
                </label>
                <label className="field">
                  <span>计算设备</span>
                  <select
                    value={transcriptionOptions.device}
                    onChange={(event) =>
                      setTranscriptionOptions({
                        ...transcriptionOptions,
                        device: event.target.value as TranscriptionDevice,
                      })
                    }
                  >
                    <option value="auto">自动选择</option>
                    <option value="cuda">CUDA 显卡</option>
                    <option value="cpu">CPU</option>
                  </select>
                </label>
              </div>
              <div className="toggle-list compact-toggles">
                <Toggle
                  label="动态节拍对齐"
                  checked={transcriptionOptions.align_beats}
                  onChange={(align_beats) =>
                    setTranscriptionOptions({ ...transcriptionOptions, align_beats })
                  }
                />
              </div>
              <label className="range-field media-range">
                <span>短音释放下限 {Math.round(transcriptionOptions.minimum_note_ms)} ms（保留起音）</span>
                <input
                  type="range"
                  min="30"
                  max="160"
                  step="5"
                  value={transcriptionOptions.minimum_note_ms}
                  onChange={(event) =>
                    setTranscriptionOptions({
                      ...transcriptionOptions,
                      minimum_note_ms: Number(event.target.value),
                    })
                  }
                />
              </label>
            </section>
          )}

          <section>
            <div className="section-heading">
              <span>{isMedia || isPdf ? "04" : "03"}</span>
              <div><h2>记谱与版面风格</h2><p>记谱决定如何整理节奏，雕版只改变视觉排版</p></div>
            </div>
            <div className="segmented" role="group" aria-label="谱面风格">
              {(["clean", "balanced", "faithful"] as ScoreStyle[]).map((style) => (
                <button
                  key={style}
                  type="button"
                  className={options.style === style ? "active" : ""}
                  onClick={() => setOptions({ ...options, style })}
                >
                  {{ clean: "简洁", balanced: "均衡", faithful: "忠实" }[style]}
                </button>
              ))}
            </div>
            <div className="style-explanation" aria-live="polite">
              <strong>{SCORE_STYLE_GUIDANCE[options.style].title}</strong>
              <span>{SCORE_STYLE_GUIDANCE[options.style].detail}</span>
            </div>

            <label className="field">
              <span>雕版字体与间距</span>
              <select
                value={options.engraving_style}
                onChange={(event) =>
                  setOptions({
                    ...options,
                    engraving_style: event.target.value as EngravingStyle,
                  })
                }
              >
                <option value="classic">经典出版（推荐）· Leland / Edwin</option>
                <option value="modern">现代清晰 · Bravura / Edwin</option>
                <option value="compact">紧凑演奏 · Leland / Edwin</option>
              </select>
            </label>
            <div className="style-explanation engraving-explanation" aria-live="polite">
              <strong>{ENGRAVING_STYLE_GUIDANCE[options.engraving_style].title}</strong>
              <span>{ENGRAVING_STYLE_GUIDANCE[options.engraving_style].detail}</span>
            </div>
            <p className="style-recommendation">
              一般推荐“简洁 + 经典出版”；它不会隐藏音头，只会减少转录噪声并保持舒展排版。
            </p>

            <label className="field">
              <span>最小时值</span>
              <select
                value={options.minimum_note}
                onChange={(event) =>
                  setOptions({ ...options, minimum_note: event.target.value as MinimumNote })
                }
              >
                <option value="auto">自动判断</option>
                <option value="eighth">八分音符</option>
                <option value="sixteenth">十六分音符</option>
                <option value="thirty_second">三十二分音符</option>
              </select>
            </label>

            <div className="toggle-list">
              <Toggle
                label={isMedia ? "强制三连音（默认自动识别）" : "识别三连音"}
                checked={options.allow_triplets}
                onChange={(allow_triplets) => setOptions({ ...options, allow_triplets })}
              />
              <Toggle
                label="保留踏板线"
                checked={options.include_pedal}
                onChange={(include_pedal) => setOptions({ ...options, include_pedal })}
              />
              <Toggle
                label="力度记号"
                checked={options.include_dynamics}
                onChange={(include_dynamics) => setOptions({ ...options, include_dynamics })}
              />
              <Toggle
                label="优先使用轨道分手"
                checked={options.prefer_track_hints}
                onChange={(prefer_track_hints) => setOptions({ ...options, prefer_track_hints })}
              />
              <Toggle
                label="固定左右手分界"
                checked={fixedSplit}
                onChange={(enabled) => setOptions({ ...options, hand_split: enabled ? 60 : "auto" })}
              />
            </div>
            {fixedSplit && (
              <label className="range-field">
                <span>分界音高：MIDI {options.hand_split}</span>
                <input
                  type="range"
                  min="45"
                  max="72"
                  value={options.hand_split as number}
                  onChange={(event) =>
                    setOptions({ ...options, hand_split: Number(event.target.value) })
                  }
                />
              </label>
            )}
          </section>

          {error && <div className="error-box">{error}</div>}
          <button className="convert-button" disabled={!canConvert} onClick={runConversion}>
            {loading ? (
              <><span className="spinner" />{isMedia ? "正在转录、分析与排版" : isPdf ? "正在识别乐谱、排版" : "正在分析与排版"}</>
            ) : (
              isMedia ? "转录并生成钢琴谱" : isPdf ? "识别 PDF 并生成钢琴谱" : "生成钢琴谱"
            )}
          </button>
          <p className="local-note">文件仅交给本机服务处理；转录模型和 FFmpeg 均在本机运行。</p>
        </aside>

        <section className="preview-panel">
          <div className="preview-toolbar">
            <div className="preview-title-block">
              <span className="eyebrow">SCORE PREVIEW</span>
              <h2>{previewTitle}</h2>
              {previewAuthor ? <p>作者 · {previewAuthor}</p> : null}
            </div>
            <div className="toolbar-actions">
              <button
                type="button"
                className="primary-action"
                disabled={!result?.pdf_base64 || !result.pdf_filename}
                onClick={() =>
                  result?.pdf_base64 && result.pdf_filename
                    ? downloadBase64(result.pdf_filename, result.pdf_base64, "application/pdf")
                    : undefined
                }
              >
                下载 A4 PDF
              </button>
              <button
                type="button"
                disabled={!result}
                onClick={() => result && downloadText(result.filename, result.musicxml, "application/vnd.recordare.musicxml+xml")}
              >
                下载 MusicXML
              </button>
              <button
                type="button"
                disabled={!result?.midi_base64 || !result.midi_filename}
                onClick={() =>
                  result?.midi_base64 && result.midi_filename
                    ? downloadBase64(result.midi_filename, result.midi_base64, "audio/midi")
                    : undefined
                }
              >
                下载转录 MIDI
              </button>
            </div>
          </div>

          {result && (
            <div className="analysis-strip">
              <Metric label="小节" value={result.analysis.measure_count} />
              <Metric label="音符" value={result.analysis.note_count} />
              <Metric label="拍号" value={result.analysis.meter ?? "—"} />
              <Metric label="调性" value={keyLabel} />
              <Metric label="质量" value={qualityLabel} />
              <Metric label="版面" value={result.analysis.engraving?.page_size ?? "MusicXML"} />
              <Metric label="页数" value={result.analysis.engraving?.page_count ?? "—"} />
              {result.analysis.key_signatures && result.analysis.key_signatures.length > 1 && (
                <Metric label="转调" value={`${result.analysis.key_signatures.length - 1} 处`} />
              )}
              {result.analysis.transcription?.detected_meter ? (
                <Metric
                  label="检测拍号"
                  value={String(result.analysis.transcription.detected_meter)}
                />
              ) : null}
              {result.analysis.ornaments &&
              (result.analysis.ornaments.trills > 0 ||
                result.analysis.ornaments.grace_notes > 0) ? (
                <Metric
                  label="装饰音"
                  value={`颤音 ${result.analysis.ornaments.trills} / 倚音 ${result.analysis.ornaments.grace_notes}`}
                />
              ) : null}
              {result.analysis.transcription && (
                <Metric
                  label="转录"
                  value={`${result.analysis.transcription.backend} / ${result.analysis.transcription.device}`}
                />
              )}
              {result.analysis.omr && (
                <Metric label="识别" value={String(result.analysis.omr.engine)} />
              )}
            </div>
          )}

          {result?.warnings.length ? (
            <details className="warnings">
              <summary>{result.warnings.length} 条转换提示</summary>
              <ul>{result.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
            </details>
          ) : null}

          <ScorePreview
            musicxml={result?.musicxml ?? null}
            previewPngBase64={result?.preview_png_base64 ?? null}
            previewPngsBase64={result?.preview_pngs_base64 ?? null}
            pageCount={result?.analysis.engraving?.page_count ?? 1}
          />
        </section>
      </main>
    </div>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="toggle-row">
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <i aria-hidden="true" />
    </label>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>;
}
