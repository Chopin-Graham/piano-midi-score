import { useEffect, useRef, useState } from "react";

interface ScorePreviewProps {
  musicxml: string | null;
  previewPngBase64?: string | null;
  pageCount?: number;
}

interface OsmdInstance {
  Zoom: number;
  EngravingRules: {
    StretchLastSystemLine: boolean;
  };
  load(input: string): Promise<void>;
  render(): void;
}

type OsmdConstructor = new (
  container: string | HTMLElement,
  options?: Record<string, unknown>,
) => OsmdInstance;

export function ScorePreview({ musicxml, previewPngBase64, pageCount = 1 }: ScorePreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [rendering, setRendering] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const target = containerRef.current;
    const xml = musicxml;
    if (!target || !xml || previewPngBase64) return;

    async function renderScore(container: HTMLDivElement, scoreXml: string) {
      setRendering(true);
      setRenderError(null);
      container.innerHTML = "";
      try {
        const osmdModule = await import("opensheetmusicdisplay");
        if (cancelled) return;
        const namespace = (osmdModule.default ?? osmdModule) as unknown as {
          OpenSheetMusicDisplay: OsmdConstructor;
        };
        const { OpenSheetMusicDisplay } = namespace;
        const osmd = new OpenSheetMusicDisplay(container, {
          autoResize: true,
          backend: "svg",
          drawTitle: true,
          drawSubtitle: false,
          drawComposer: false,
          drawLyricist: false,
          drawPartNames: false,
          drawMeasureNumbers: true,
          newSystemFromXML: true,
        });
        osmd.Zoom = 0.82;
        osmd.EngravingRules.StretchLastSystemLine = false;
        await osmd.load(scoreXml);
        if (cancelled) return;
        osmd.render();
      } catch (error) {
        if (!cancelled) {
          setRenderError(error instanceof Error ? error.message : "乐谱渲染失败");
        }
      } finally {
        if (!cancelled) setRendering(false);
      }
    }

    void renderScore(target, xml);
    return () => {
      cancelled = true;
      target.innerHTML = "";
    };
  }, [musicxml, previewPngBase64]);

  if (!musicxml) {
    return (
      <div className="score-empty">
        <div className="empty-staves" aria-hidden="true">
          <span />
          <span />
          <span />
          <span />
          <span />
        </div>
        <p>上传 MIDI 后，钢琴谱会在这里出现</p>
        <small>转换过程完全在本机服务中完成</small>
      </div>
    );
  }

  if (previewPngBase64) {
    return (
      <div className="score-stage publication-stage">
        <div className="publication-meta">
          <strong>A4 出版级预览</strong>
          <span>第 1 页 / 共 {pageCount} 页 · MuseScore Studio 雕版</span>
        </div>
        <img
          className="score-page-image"
          src={`data:image/png;base64,${previewPngBase64}`}
          alt="A4 钢琴谱第一页预览"
        />
        {pageCount > 1 && <p className="preview-note">此处显示第一页，下载 A4 PDF 可查看全部页面。</p>}
      </div>
    );
  }

  return (
    <div className="score-stage">
      {rendering && <div className="rendering-badge">正在排版乐谱…</div>}
      {renderError && <div className="inline-error">预览失败：{renderError}</div>}
      <div ref={containerRef} className="score-canvas" aria-label="钢琴谱预览" />
    </div>
  );
}
