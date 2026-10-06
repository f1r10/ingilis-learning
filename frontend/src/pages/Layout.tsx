import { useTranslation } from "react-i18next";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useApp } from "../context/AppProvider";
import LanguageSwitcher from "../components/LanguageSwitcher";

export default function Layout() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const { branding, subject } = useApp();
  const isAdmin = subject?.kind === "admin";

  async function logout() {
    await api.post("/auth/logout").catch(() => {});
    nav("/login");
  }

  return (
    <>
      <header className="appbar">
        <div className="appbar-inner">
          {branding.logo_url ? (
            <Link to="/dashboard"><img src={branding.logo_url} alt="" style={{ height: 28 }} /></Link>
          ) : null}
          <span className="brand">{branding.short_name || branding.system_name}</span>
          <nav className="nav">
            {isAdmin ? (
              <>
                <NavLink to="/dashboard" className={({ isActive }) => (isActive ? "active" : "")}>{t("nav.dashboard")}</NavLink>
                <NavLink to="/students" className={({ isActive }) => (isActive ? "active" : "")}>{t("nav.students")}</NavLink>
                <NavLink to="/groups" className={({ isActive }) => (isActive ? "active" : "")}>{t("nav.groups")}</NavLink>
                <NavLink to="/questions" className={({ isActive }) => (isActive ? "active" : "")}>{t("nav.questions")}</NavLink>
                <NavLink to="/topics" className={({ isActive }) => (isActive ? "active" : "")}>{t("nav.topics")}</NavLink>
                <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>{t("nav.settings")}</NavLink>
              </>
            ) : (
              <NavLink to="/student" className={({ isActive }) => (isActive ? "active" : "")}>{t("nav.dashboard")}</NavLink>
            )}
          </nav>
          <span className="spacer" />
          <LanguageSwitcher />
          <button className="btn ghost" onClick={logout}>{t("nav.logout")}</button>
        </div>
      </header>
      <main className="container">
        <Outlet />
      </main>
    </>
  );
}
