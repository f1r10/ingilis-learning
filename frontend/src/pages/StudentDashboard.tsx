import { useTranslation } from "react-i18next";
import { useApp } from "../context/AppProvider";

export default function StudentDashboard() {
  const { t } = useTranslation();
  const { subject, branding } = useApp();
  const name = subject?.kind === "student" ? subject.name : "";

  return (
    <div>
      <h1>{t("dashboard.welcome")}, {name}</h1>
      <p className="muted">{t("dashboard.placeholder")}</p>
      <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
        {["Catalogs", "Practice", "Exams", "Vocabulary", "Favorites", "Progress"].map((m) => (
          <span key={m} className="card small muted" style={{ padding: "0.6rem 0.9rem" }}>{m}</span>
        ))}
      </div>
      <p className="muted small" style={{ marginTop: 24 }}>{branding.footer}</p>
    </div>
  );
}
