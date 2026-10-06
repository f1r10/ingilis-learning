import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import en from "./locales/en.json";
import az from "./locales/az.json";
import ru from "./locales/ru.json";
import tr from "./locales/tr.json";

// Interface is localised; learning content belongs to whatever language(s) the
// teacher configures. The student chooses their own interface language.
i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      az: { translation: az },
      ru: { translation: ru },
      tr: { translation: tr },
    },
    fallbackLng: "en",
    interpolation: { escapeValue: false },
    detection: { order: ["localStorage", "navigator"], caches: ["localStorage"] },
  });

export function applyLanguage(code: string) {
  i18n.changeLanguage(code);
  localStorage.setItem("ui_lang", code);
  document.documentElement.lang = code;
}

export default i18n;
