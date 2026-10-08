import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import "./research-motion.css";

const preferenceKey = "meta-research:still-picture";
const MotionContext = createContext({ still: false, reduced: false, visible: true, toggle: () => {} });

export function ResearchMotionProvider({ children }: { children: ReactNode }) {
  const [still, setStill] = useState(() => {
    try { return localStorage.getItem(preferenceKey) === "true"; } catch { return false; }
  });
  const [reduced, setReduced] = useState(() => matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const media = matchMedia("(prefers-reduced-motion: reduce)");
    const mediaChanged = () => setReduced(media.matches);
    const visibilityChanged = () => setVisible(document.visibilityState !== "hidden");
    media.addEventListener("change", mediaChanged);
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => {
      media.removeEventListener("change", mediaChanged);
      document.removeEventListener("visibilitychange", visibilityChanged);
    };
  }, []);
  useEffect(() => {
    document.documentElement.dataset.researchMotion = still || reduced || !visible ? "still" : "enabled";
  }, [still, reduced, visible]);
  const toggle = () => setStill(previous => {
    const next = !previous;
    try { localStorage.setItem(preferenceKey, String(next)); } catch { /* The preference remains usable in this page. */ }
    return next;
  });
  return <MotionContext.Provider value={{ still, reduced, visible, toggle }}>{children}</MotionContext.Provider>;
}

export function ResearchMotionControl() {
  const { still, reduced, toggle } = useContext(MotionContext);
  return <button type="button" className="research-motion-control" aria-label="静止画面"
    aria-pressed={still || reduced} onClick={toggle}
    title={reduced ? "系统已要求减少动态；静止画面不会暂停研究或停止更新" : "只停止画面动态，研究和内容更新继续"}>
    <span aria-hidden="true">{still || reduced ? "Ⅱ" : "≈"}</span><span>静止画面</span>
  </button>;
}

/** Region visibility affects decoration; it does not suspend the research. */
export function useResearchMotion<T extends HTMLElement = HTMLDivElement>(running: boolean) {
  const { still, reduced, visible } = useContext(MotionContext);
  const ref = useRef<T>(null);
  const [intersecting, setIntersecting] = useState(false);
  useEffect(() => {
    const region = ref.current;
    if (!region) return;
    const observer = new IntersectionObserver(entries => setIntersecting(entries.some(entry => entry.isIntersecting)));
    observer.observe(region);
    return () => observer.disconnect();
  }, []);
  return { ref, visible: visible && intersecting, active: running && !still && !reduced && visible && intersecting };
}
