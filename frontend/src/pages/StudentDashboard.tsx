import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { useApp } from "../context/AppProvider";

export default function StudentDashboard() {
  const { t } = useTranslation();
  const { subject, branding } = useApp();
  const name = subject?.kind === "student" ? subject.name : "";

  return (
    <div>
      <h1>{t("dashboard.welcome")}, {name}</h1>
      <p className="muted">{t("dashboard.student_start")}</p>
      <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
        <Link to="/student/vocabulary" className="card small" style={{ padding: "0.6rem 0.9rem", textDecoration: "none" }}>
          {t("vocabulary.my_words")}
        </Link>
      </div>
      <p className="muted small" style={{ marginTop: 24 }}>{branding.footer}</p>
    </div>
  );
}
