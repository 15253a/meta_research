import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { fetchOutputLanguage, saveOutputLanguage, type OutputLanguage } from "./api";
import "./research-library.css";

const LanguageContext = createContext<{
  language: OutputLanguage; busy: boolean; error: boolean;
  change: (language: OutputLanguage) => Promise<void>;
}>({ language: "zh", busy: false, error: false, change: async () => undefined });

export function OutputLanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<OutputLanguage>("zh");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetchOutputLanguage(controller.signal).then(value => {
      if (value.output_language !== "zh" && value.output_language !== "en") throw new Error("output_language_invalid");
      if (!controller.signal.aborted) setLanguage(value.output_language);
    }).catch(() => {
      if (!controller.signal.aborted) setError(true);
    }).finally(() => {
      if (!controller.signal.aborted) setBusy(false);
    });
    return () => controller.abort();
  }, []);
  async function change(value: OutputLanguage) {
    setBusy(true); setError(false);
    try { const saved = await saveOutputLanguage(value); if (saved.output_language !== "zh" && saved.output_language !== "en") throw new Error("output_language_invalid"); setLanguage(saved.output_language); }
    catch { setError(true); }
    finally { setBusy(false); }
  }
  return <LanguageContext.Provider value={{ language, busy, error, change }}>{children}</LanguageContext.Provider>;
}
export const useOutputLanguage = () => useContext(LanguageContext);
export function OutputLanguageControl({ floating = false }: { floating?: boolean }) {
  const { language, busy, error, change } = useOutputLanguage();
  return <div className={floating ? "output-language-control floating" : "output-language-control"}>
    <label><span>语言 / Language</span><select aria-label="输出语言 / Output language" value={language} disabled={busy} onChange={event => void change(event.target.value as OutputLanguage)}>
      <option value="zh">中文</option><option value="en">English</option>
    </select></label>
    {error ? <small role="alert">{language === "zh" ? "无法读取或保存语言设置，请重新选择。" : "Language settings unavailable. Please choose again."}</small> : null}
  </div>;
}
