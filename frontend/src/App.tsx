import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useApp } from "./context/AppProvider";
import Layout from "./pages/Layout";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import StudentDashboard from "./pages/StudentDashboard";
import Students from "./pages/Students";
import Groups from "./pages/Groups";
import Settings from "./pages/Settings";
import Questions from "./pages/Questions";
import QuestionEditor from "./pages/QuestionEditor";
import Topics from "./pages/Topics";
import Vocabulary from "./pages/Vocabulary";
import VocabularyEditor from "./pages/VocabularyEditor";
import StudentVocabulary from "./pages/StudentVocabulary";
import Reading from "./pages/Reading";
import ReadingEditor from "./pages/ReadingEditor";
import Listenings from "./pages/Listenings";
import ListeningEditor from "./pages/ListeningEditor";
import MediaLibrary from "./pages/MediaLibrary";
import StudentReading from "./pages/StudentReading";
import StudentListening from "./pages/StudentListening";

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
          <Route path="/questions" element={<Questions />} />
          <Route path="/questions/new" element={<QuestionEditor />} />
          <Route path="/questions/:id" element={<QuestionEditor />} />
          <Route path="/vocabulary" element={<Vocabulary />} />
          <Route path="/vocabulary/new" element={<VocabularyEditor />} />
          <Route path="/vocabulary/:id" element={<VocabularyEditor />} />
          <Route path="/topics" element={<Topics />} />
          <Route path="/reading" element={<Reading />} />
          <Route path="/reading/new" element={<ReadingEditor />} />
          <Route path="/reading/:id" element={<ReadingEditor />} />
          <Route path="/listening" element={<Listenings />} />
          <Route path="/listening/new" element={<ListeningEditor />} />
          <Route path="/listening/:id" element={<ListeningEditor />} />
          <Route path="/media" element={<MediaLibrary />} />
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
          <Route path="/student/vocabulary" element={<StudentVocabulary />} />
          <Route path="/student/reading" element={<StudentReading />} />
          <Route path="/student/listening" element={<StudentListening />} />
        </Route>
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
