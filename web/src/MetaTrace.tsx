import "./meta-trace.css";

type TraceVariant = "signature" | "brief" | "spectrum" | "timeline" | "assistant";

/** The original arc-and-dot identity selected in the approved component prototype. */
export function MetaTrace({ variant = "signature", className = "", active = false }: {
  variant?: TraceVariant; className?: string; active?: boolean;
}) {
  const artwork = {
    signature: { viewBox: "0 0 48 48", path: "M4 40 C9 32 17 26 24 23 C31 20 39 14 43 8 L43 10 C39 16 31 22 24 25 C17 28 9 34 10 44 Z", x: 43, y: 9, radius: 3 },
    brief: { viewBox: "0 0 20 20", path: "M3 16 C5 12 9 9 12 7 L12 8 C9 10 5 13 5 17 Z", x: 12, y: 7, radius: 2 },
    spectrum: { viewBox: "0 0 24 24", path: "M3 17 C6 12 11 9 17 7 L17 8 C11 10 6 13 5 18 Z", x: 17, y: 7, radius: 2.5 },
    timeline: { viewBox: "0 0 16 16", path: "M2 12 C3 9 6 7 9 6 L9 7 C6 8 3 10 3 13 Z", x: 9, y: 6, radius: 1.5 },
    assistant: { viewBox: "0 0 32 32", path: "M7 24 C9 20 12 17 16 15 C19 13 23 9 25 7 L25 8 C23 10 20 14 16 16 C12 18 9 21 9 25 Z", x: 25, y: 7, radius: 2 },
  }[variant];
  return <span className={`meta-trace meta-trace-${variant} ${className}`} data-motion-active={variant !== "assistant" && active} aria-hidden="true">
    <svg viewBox={artwork.viewBox} fill="none" focusable="false">
      {variant === "assistant" ? <circle cx="16" cy="16" r="14" stroke="#b8c4d8" strokeWidth="1.5" /> : null}
      <path d={artwork.path} fill={variant === "signature" ? "#4A5B7A" : "currentColor"} opacity={variant === "signature" ? 1 : .65} />
      <circle cx={artwork.x} cy={artwork.y} r={artwork.radius} fill={variant === "signature" || variant === "assistant" ? "#7268A6" : "currentColor"} />
    </svg>
  </span>;
}
