import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import { useApp } from "../context/AppProvider";
import LanguageSwitcher from "../components/LanguageSwitcher";

export default function Login() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const { branding, setSubject } = useApp();

  const [tab, setTab] = useState<"teacher" | "student">("teacher");
  const [recover, setRecover] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [accessKey, setAccessKey] = useState("");
  const [code, setCode] = useState("");

  async function submitTeacher() {
    const info = await api.post<{ subject_type: string }>("/auth/admin/login", { username, password });
    setSubject({ kind: "admin", id: "", username });
    void info;
    nav("/dashboard");
  }

  async function submitRecovery() {
    await api.post("/auth/admin/recovery/login", { username, code });
    setSubject({ kind: "admin", id: "", username });
    nav("/dashboard");
  }

  async function submitStudent() {
    await api.post("/auth/student/login", { access_key: accessKey });
    const me = await api.get<any>("/auth/me/student");
    setSubject({ kind: "student", id: me.id, name: me.name, surname: me.surname, username: me.username, ui_language: me.ui_language });
    nav("/student");
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (tab === "teacher" && recover) await submitRecovery();
      else if (tab === "teacher") await submitTeacher();
      else await submitStudent();
    } catch (err: any) {
      setError(err?.message || t("common.error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div
        className="login-visual"
        style={branding.login_image_url ? { backgroundImage: `url(${branding.login_image_url})` } : undefined}
      >
        <div style={{ textAlign: "center" }}>
          {branding.logo_url && <img src={branding.logo_url} alt="" style={{ maxWidth: 160, marginBottom: 16 }} />}
          <h1>{branding.welcome_message || branding.system_name}</h1>
          {branding.login_instructions && <p className="muted">{branding.login_instructions}</p>}
        </div>
      </div>

      <div className="login-form">
        <form className="login-card" onSubmit={onSubmit}>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
            <strong>{branding.login_title || branding.system_name}</strong>
            <LanguageSwitcher />
          </div>

          <div className="tabs">
            <div className={`tab ${tab === "teacher" ? "active" : ""}`} onClick={() => { setTab("teacher"); setRecover(false); }}>
              {t("login.teacher_tab")}
            </div>
            <div className={`tab ${tab === "student" ? "active" : ""}`} onClick={() => setTab("student")}>
              {t("login.student_tab")}
            </div>
          </div>

          {tab === "teacher" && (
            <>
              <div className="field">
                <label>{t("login.username")}</label>
                <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
              </div>
              {!recover ? (
                <div className="field">
                  <label>{t("login.password")}</label>
                  <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
                </div>
              ) : (
                <div className="field">
                  <label>{t("login.recover_code")}</label>
                  <input className="input mono" value={code} onChange={(e) => setCode(e.target.value)} placeholder="XXXX-XXXX-XXXX" />
                </div>
              )}
              <div className="row small">
                <a href="#" onClick={(e) => { e.preventDefault(); setRecover(!recover); }}>
                  {recover ? t("login.back") : t("login.recover")}
                </a>
              </div>
            </>
          )}

          {tab === "student" && (
            <div className="field">
              <label>{t("login.access_key")}</label>
              <input className="input mono" value={accessKey} onChange={(e) => setAccessKey(e.target.value)} placeholder="username@…" autoComplete="off" />
            </div>
          )}

          {error && <div className="alert error">{error}</div>}

          <button className="btn" disabled={busy} style={{ width: "100%", marginTop: 8 }}>
            {busy ? t("common.loading") : t("login.sign_in")}
          </button>

          {branding.support_text && <p className="muted small" style={{ marginTop: 16 }}>{branding.support_text}</p>}
          {branding.footer && <p className="muted small">{branding.footer}</p>}
        </form>
      </div>
    </div>
  );
}
