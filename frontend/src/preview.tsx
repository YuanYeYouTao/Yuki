import { useEffect, useRef, useState } from "react";

export function DesktopWindow({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="desktop-window"
      onClose={close}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      aria-label={title}
    >
      <header className="desktop-title">
        <strong>{title}</strong>
        <button type="button" className="btn-secondary" onClick={close}>
          关闭
        </button>
      </header>
      <div className="desktop-body">{children}</div>
    </dialog>
  );
}

export function MediaPreview({
  url,
  title,
  compact = false,
}: {
  url: string;
  title: string;
  compact?: boolean;
}) {
  const [state, setState] = useState<{
    src: string;
    image: boolean;
    error: string;
  } | null>(null);
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(
    typeof IntersectionObserver === "undefined",
  );
  useEffect(() => {
    if (visible || !root.current) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "320px" },
    );
    observer.observe(root.current);
    return () => observer.disconnect();
  }, [visible]);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    let objectUrl = "";
    setState(null);
    fetch(url, { credentials: "same-origin", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok)
          throw new Error(
            response.status === 404
              ? "原件已过期或不存在"
              : response.status === 403
                ? "无权查看原件"
                : `媒体读取失败（${response.status}）`,
          );
        const blob = await response.blob();
        objectUrl = URL.createObjectURL(blob);
        setState({
          src: objectUrl,
          image: blob.type.startsWith("image/"),
          error: "",
        });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setState({
            src: "",
            image: false,
            error: error instanceof Error ? error.message : "媒体读取失败",
          });
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url, attempt, visible]);
  return (
    <div ref={root} className={`media-preview ${compact ? "compact" : ""}`}>
      {!visible && <span className="small">滑动到此处加载媒体…</span>}
      {visible && !state && <span className="small">正在加载媒体…</span>}
      {state?.error && (
        <span role="status" className="media-error">
          {state.error}{" "}
          <button
            type="button"
            className="file-open"
            onClick={() => setAttempt(attempt + 1)}
          >
            重试
          </button>
        </span>
      )}
      {state?.src &&
        (state.image ? (
          <button
            className="media-thumb"
            type="button"
            onClick={() => setOpen(true)}
            aria-label={`预览${title}`}
          >
            <img
              src={state.src}
              alt={title}
              loading="lazy"
              onError={() =>
                setState({ src: "", image: false, error: "图片格式无法显示" })
              }
            />
          </button>
        ) : (
          <a href={state.src} download={title}>
            下载 {title}
          </a>
        ))}
      {open && state?.src && (
        <DesktopWindow title={title} close={() => setOpen(false)}>
          <img className="media-full" src={state.src} alt={title} />
        </DesktopWindow>
      )}
    </div>
  );
}
