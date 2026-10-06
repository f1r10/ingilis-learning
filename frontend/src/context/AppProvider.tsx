import React, { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Bootstrap, type Branding, type UiConfig } from "../api/client";
import { applyLanguage } from "../i18n";

export type Subject =
  | { kind: "admin"; id: string; username: string; display_name?: string | null }
  | { kind: "student"; id: string; name: string; surname: string; username: string; ui_language?: string | null }
  | null;

interface AppContextValue {
  branding: Branding;
  ui: UiConfig;
  subject: Subject;
  ready: boolean;
  setSubject: (s: Subject) => void;
}

const DEFAULT_BRANDING: Branding = { system_name: "Learning Platform", short_name: "Learn" };
const DEFAULT_UI: UiConfig = {
  enabled_ui_languages: ["en"],
  default_ui_language: "en",
  learning_languages: ["en"],
  translation_languages: [],
};

const AppContext = createContext<AppContextValue | null>(null);

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [subject, setSubject] = useState<Subject>(null);
  // True only once the session probe below has answered. `ready` cannot simply mean
  // "the branding arrived": bootstrap usually wins the race against /auth/me/admin, and
  // a guard that is released in between reads a valid session as no session and bounces a
  // signed-in teacher to the login page on every hard reload of a deep link.
  const [probed, setProbed] = useState(false);

  const bootstrap = useQuery({
    queryKey: ["bootstrap"],
    queryFn: () => api.get<Bootstrap>("/auth/bootstrap"),
    staleTime: 60_000,
  });

  const branding = bootstrap.data?.branding ?? DEFAULT_BRANDING;
  const ui = bootstrap.data?.ui ?? DEFAULT_UI;

  // Restore the session on first load by probing both role endpoints.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me = await api.get<any>("/auth/me/admin");
        if (!cancelled) setSubject({ kind: "admin", id: me.id, username: me.username, display_name: me.display_name });
      } catch {
        try {
          const me = await api.get<any>("/auth/me/student");
          if (!cancelled) {
            setSubject({ kind: "student", id: me.id, name: me.name, surname: me.surname, username: me.username, ui_language: me.ui_language });
            if (me.ui_language) applyLanguage(me.ui_language);
          }
        } catch {
          /* not logged in */
        }
      }
      if (!cancelled) setProbed(true);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Apply branding + accent + default language once known.
  useEffect(() => {
    if (branding.accent_color) document.documentElement.style.setProperty("--accent", branding.accent_color);
    if (branding.favicon_url) {
      const link = document.getElementById("favicon") as HTMLLinkElement | null;
      if (link) link.href = branding.favicon_url;
    }
    document.title = branding.system_name || "Learning Platform";
  }, [branding]);

  useEffect(() => {
    if (!localStorage.getItem("ui_lang")) applyLanguage(ui.default_ui_language || "en");
  }, [ui.default_ui_language]);

  const value = useMemo(
    () => ({ branding, ui, subject, ready: !bootstrap.isLoading && probed, setSubject }),
    [branding, ui, subject, bootstrap.isLoading, probed],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used within AppProvider");
  return ctx;
}
