import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { useApp } from "../context/AppProvider";

const TILES = [
  { to: "/questions", label: "nav.questions" },
  { to: "/vocabulary", label: "nav.vocabulary" },
  { to: "/reading", label: "nav.reading" },
  { to: "/listening", label: "nav.listening" },
  { to: "/media", label: "nav.media" },
  { to: "/topics", label: "nav.topics" },
  { to: "/students", label: "nav.students" },
  { to: "/groups", label: "nav.groups" },
  { to: "/settings", label: "nav.settings" },
];

export default function Dashboard() {
  const { t } = useTranslation();
  const { subject, branding } = useApp();
  const isAdmin = subject?.kind === "admin";

  return (
    <div>
      <h1>{t("dashboard.welcome")}</h1>
      <p className="muted">{isAdmin ? t("dashboard.role_teacher") : t("dashboard.role_student")}</p>

      <div className="card">
        <h2>{branding.system_name}</h2>
        <p className="muted">{isAdmin ? t("dashboard.teacher_start") : t("dashboard.student_start")}</p>
        <div className="row" style={{ flexWrap: "wrap", gap: 12, marginTop: 12 }}>
          {TILES.map((tile) => (
            <Link key={tile.to} to={tile.to} className="card small" style={{ padding: "0.6rem 0.9rem", textDecoration: "none" }}>
              {t(tile.label)}
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
