import { useTranslation } from "react-i18next";
import { useApp } from "../context/AppProvider";

export default function Dashboard() {
  const { t } = useTranslation();
  const { subject } = useApp();
  const isAdmin = subject?.kind === "admin";

  return (
    <div>
      <h1>{t("dashboard.welcome")}{isAdmin ? "" : ""}</h1>
      <p className="muted">{isAdmin ? t("dashboard.role_teacher") : t("dashboard.role_student")}</p>

      <div className="card">
        <h2>{t("nav.dashboard")}</h2>
        <p className="muted">{t("dashboard.placeholder")}</p>
        <div className="row" style={{ flexWrap: "wrap", gap: 12, marginTop: 12 }}>
          {["Question Bank", "Vocabulary", "Reading", "Listening", "Catalogs", "Exams", "Import", "Analytics", "Monitoring"].map((m) => (
            <span key={m} className="card small muted" style={{ padding: "0.4rem 0.7rem" }}>{m}</span>
          ))}
        </div>
      </div>
    </div>
  );
}
