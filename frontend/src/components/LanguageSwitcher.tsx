import { useTranslation } from "react-i18next";
import { applyLanguage } from "../i18n";
import { useApp } from "../context/AppProvider";

const LABELS: Record<string, string> = { az: "AZ", en: "EN", ru: "RU", tr: "TR" };

export default function LanguageSwitcher() {
  const { ui } = useApp();
  const { i18n } = useTranslation();
  const options = ui.enabled_ui_languages?.length ? ui.enabled_ui_languages : ["en"];

  // Keep it quiet: when only one language is enabled, do not force multilingual UI.
  if (options.length <= 1) return null;

  return (
    <select
      className="input"
      style={{ width: "auto", padding: "0.3rem 0.5rem", minHeight: 40 }}
      value={i18n.language?.slice(0, 2) || "en"}
      onChange={(e) => applyLanguage(e.target.value)}
      aria-label="Language"
    >
      {options.map((code) => (
        <option key={code} value={code}>
          {LABELS[code] || code}
        </option>
      ))}
    </select>
  );
}
