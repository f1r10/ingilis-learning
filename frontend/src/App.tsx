import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useApp } from "./context/AppProvider";
import Layout from "./pages/Layout";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import StudentDashboard from "./pages/StudentDashboard";
import Students from "./pages/Students";
import Groups from "./pages/Groups";
import Settings from "./pages/Settings";

function RequireAuth({ children, admin }: { children: React.ReactNode; admin?: boolean }) {
  const { subject, ready } = useApp();
  if (!ready) return <div className="container muted">…</div>;
  if (!subject) return <Navigate to="/login" replace />;
  if (admin && subject.kind !== "admin") return <Navigate to="/student" replace />;
  if (!admin && subject.kind !== "student") return <Navigate to="/dashboard" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <RequireAuth admin>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/students" element={<Students />} />
          <Route path="/groups" element={<Groups />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/student" element={<StudentDashboard />} />
        </Route>
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
