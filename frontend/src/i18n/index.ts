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
  })
  // A language restored from storage arrives with init and fires no change event, so the document
  // picks it up here rather than waiting for somebody to touch the switcher.
  .then(() => {
    document.documentElement.lang = i18n.language;
  });

// `<html lang>` is what a screen reader pronounces with and what a browser spells-checks against, so
// it has to name the language the interface is really in. Setting it here means every path that
// changes the language sets it, and an Azerbaijani screen never announces itself as English.
i18n.on("languageChanged", (code) => {
  document.documentElement.lang = code;
});

export function applyLanguage(code: string) {
  i18n.changeLanguage(code);
  localStorage.setItem("ui_lang", code);
}

export default i18n;
