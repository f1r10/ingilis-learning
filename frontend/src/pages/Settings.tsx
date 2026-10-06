import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api, type Branding } from "../api/client";

export default function Settings() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [branding, setBranding] = useState<Branding>({ system_name: "", short_name: "" });

  const get = useQuery({ queryKey: ["branding"], queryFn: () => api.get<Branding>("/settings/branding") });
  useEffect(() => { if (get.data) setBranding(get.data); }, [get.data]);

  const save = useMutation({
    mutationFn: () => api.put<Branding>("/settings/branding", { category: "branding", values: branding }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bootstrap"] }),
  });

  const field = (key: keyof Branding, label: string, type = "text") => (
    <div className="field">
      <label>{label}</label>
      <input
        className="input"
        type={type}
        value={(branding[key] as string) ?? ""}
        onChange={(e) => setBranding({ ...branding, [key]: e.target.value })}
      />
    </div>
  );

  return (
    <div style={{ maxWidth: 560 }}>
      <h1>{t("settings.title")}</h1>
      <h2>{t("settings.branding")}</h2>
      <div className="card">
        {field("system_name", t("settings.system_name"))}
        {field("short_name", t("settings.short_name"))}
        {field("accent_color", t("settings.accent"))}
        {field("logo_url", "Logo URL")}
        {field("login_title", "Login title")}
        {field("welcome_message", "Welcome message")}
        <button className="btn" onClick={() => save.mutate()} disabled={!get.data}>
          {save.isSuccess ? "Saved" : t("settings.save")}
        </button>
      </div>
      <p className="muted small">
        Provider settings (AI/OCR/media/retention/backups), exam and practice defaults, and dashboard
        widgets are managed through the same category endpoints (<code>/settings/:category</code>).
      </p>
    </div>
  );
}
