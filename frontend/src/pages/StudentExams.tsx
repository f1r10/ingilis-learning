// The learner's papers: what has been handed to them, whether it can be opened now, and what it
// cost them when they sat it.
//
// This list never decides any of that. `available` and `availability_reason` come from the
// server's own window check - opening time, closing time, the attempt allowance and whether this
// learner already handed this paper in - so a screen that counted the clock by itself would show a
// paper as open for one learner and shut for another. The same rule covers `attempts_left` and the
// best mark: both are read back, never worked out here.
//
// A paper still running is offered as "continue" and never as "start again". The sitting already
// open on it holds the answers given so far, and `/start` would hand back that same sitting rather
// than a second one - so the list points at the token instead of asking the learner to begin twice.
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { studentExamsApi, type LearnerExam } from "../api/exams";
import { when } from "../i18n/format";

export default function StudentExams() {
  const { t } = useTranslation();
  const list = useQuery({ queryKey: ["student-exams"], queryFn: studentExamsApi.list });

  if (list.isError) {
    return (
      <div className="alert error">
        {t("common.could_not_load")} {(list.error as ApiError).message}
      </div>
    );
  }

  const rows = list.data?.items || [];

  return (
    <div className="stack">
      <h1 style={{ margin: 0 }}>{t("student_exams.title")}</h1>
      <p className="muted small">{t("student_exams.hint")}</p>

      {!list.isLoading && !rows.length ? (
        <div className="card muted">{t("student_exams.none")}</div>
      ) : null}

      {rows.map((row) => (
        <ExamCard key={row.id} row={row} />
      ))}
    </div>
  );
}

function ExamCard({ row }: { row: LearnerExam }) {
  const { t } = useTranslation();

  const facts = [
    row.level,
    row.duration_minutes ? t("exams.minutes_n", { n: row.duration_minutes }) : t("student_exams.open_time"),
    t("exams.points_n", { n: row.points }),
    t("exams.questions_n", { n: row.item_count }),
  ];

  return (
    <div className="card stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <strong>{row.title}</strong>
        <span className="spacer" />
        <span className="chip">{t(`exams.reason_${row.availability_reason}`)}</span>
      </div>

      {row.description ? <p className="small" style={{ margin: 0 }}>{row.description}</p> : null}

      <div className="row small muted" style={{ flexWrap: "wrap", gap: 10 }}>
        {facts.filter(Boolean).map((fact) => (
          <span key={fact}>{fact}</span>
        ))}
      </div>

      <div className="row small" style={{ flexWrap: "wrap", gap: 10 }}>
        {row.opens_at && !row.available ? (
          <span className="muted">{t("student_exams.opens_at", { when: when(row.opens_at) })}</span>
        ) : null}
        {row.closes_at ? (
          <span className="muted">{t("student_exams.closes_at", { when: when(row.closes_at) })}</span>
        ) : null}
        {row.max_attempts !== null ? (
          <span className="muted">
            {t("student_exams.attempts_left", {
              left: row.attempts_left ?? 0,
              total: row.max_attempts,
            })}
          </span>
        ) : (
          <span className="muted">{t("student_exams.attempts_unlimited", { used: row.attempts_used })}</span>
        )}
      </div>

      {row.latest_status ? (
        <div className="row small" style={{ flexWrap: "wrap", gap: 8 }}>
          <span className="muted">{t("student_exams.last_sitting", { status: t(`status.${row.latest_status}`) })}</span>
          {row.best_max_score ? (
            <span>
              {t("practice.score_of", {
                got: row.best_score ?? 0,
                total: row.best_max_score,
              })}
            </span>
          ) : null}
          {row.feedback_waiting ? <span className="chip">{t("student_exams.teacher_still_marking")}</span> : null}
          {row.result_visible ? <span className="chip">{t("student_exams.marks_ready")}</span> : null}
        </div>
      ) : null}

      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {/* Continue first: an open sitting is the fact that matters, and a second start would only
            be refused. */}
        {row.resume_token ? (
          <Link className="btn" to={`/student/exams/run?token=${row.resume_token}`} style={{ minHeight: 44 }}>
            {t("student_exams.continue")}
          </Link>
        ) : null}
        <Link className={row.resume_token ? "btn secondary" : "btn"} to={`/student/exams/${row.id}`} style={{ minHeight: 44 }}>
          {row.resume_token ? t("student_exams.details") : t("student_exams.open_paper")}
        </Link>
      </div>
    </div>
  );
}
