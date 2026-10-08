// The page before a paper: what it asks, how long it gives, and the button that starts it.
//
// Nothing here is a question. The brief is the server's own statement of the rules - length,
// marks, the window, the attempt allowance and what the teacher switched on - because a learner who
// reads the rules from a screen that guessed them would be agreeing to something else. The moment
// they press start the paper is dealt and its order is frozen, so this page is the only place the
// rules are shown before the sitting exists.
//
// The history underneath is the learner's own: every sitting of this paper, what it scored, and
// whether a mark is still waiting on the teacher. A finished sitting links to its result through
// the token the server gave, which is the same key the runner uses - a learner never names an
// attempt id, they present the paper they were handed.
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { studentExamsApi } from "../api/exams";
import type { RunnerRules } from "../api/exams";
import { when } from "../i18n/format";

export default function StudentExam() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const { id = "" } = useParams();

  const brief = useQuery({ queryKey: ["student-exam", id], queryFn: () => studentExamsApi.brief(id) });
  const history = useQuery({ queryKey: ["student-exam-history", id], queryFn: () => studentExamsApi.history(id) });

  const start = useMutation({
    mutationFn: () => studentExamsApi.start(id),
    onSuccess: (read) => nav(`/student/exams/run?token=${read.token}`, { replace: true }),
    // A refusal needs no message of its own: the server's sentence names the rule that refused
    // (not yet open, closed, no attempts left, the one sitting already handed in) and `isError`
    // prints it below.
  });

  if (brief.isError) {
    return (
      <div className="stack">
        <Link className="btn secondary" to="/student/exams">
          ‹ {t("student_exams.back")}
        </Link>
        <div className="alert error">
          {t("common.could_not_load")} {(brief.error as ApiError).message}
        </div>
      </div>
    );
  }

  if (!brief.data) return <div className="card muted">{t("common.loading")}</div>;
  const row = brief.data;

  return (
    <div className="stack">
      <Link className="btn secondary" to="/student/exams" style={{ alignSelf: "flex-start" }}>
        ‹ {t("student_exams.back")}
      </Link>

      <h1 style={{ margin: 0 }}>{row.title}</h1>
      {row.description ? <p style={{ margin: 0 }}>{row.description}</p> : null}

      <div className="card stack">
        <div className="row small muted" style={{ flexWrap: "wrap", gap: 12 }}>
          {row.level ? <span>{row.level}</span> : null}
          <span>{t("exams.questions_n", { n: row.item_count })}</span>
          <span>{t("exams.points_n", { n: row.points })}</span>
          {row.duration_minutes ? (
            <span>{t("student_exams.minutes", { n: row.duration_minutes })}</span>
          ) : (
            <span>{t("student_exams.open_time")}</span>
          )}
        </div>

        <div className="small">
          {row.opens_at ? <div>{t("student_exams.opens_at", { when: when(row.opens_at) })}</div> : null}
          {row.closes_at ? (
            <div>{t("student_exams.closes_at", { when: when(row.closes_at) })}</div>
          ) : null}
          {row.must_finish_before_close ? (
            <div className="muted">{t("student_exams.must_finish_before_close")}</div>
          ) : null}
          {row.passing_score !== null ? (
            <div className="muted">{t("student_exams.passing_at", { pct: row.passing_score })}</div>
          ) : null}
          {row.max_attempts === null ? (
            <div className="muted">{t("student_exams.attempts_unlimited", { used: row.attempts_used })}</div>
          ) : (
            <div className="muted">
              {t("student_exams.attempts_left", { left: row.attempts_left ?? 0, total: row.max_attempts })}
            </div>
          )}
        </div>
      </div>

      <RulesList rules={row.rules} />

      {start.isError ? (
        <div className="alert error">{(start.error as ApiError).message}</div>
      ) : null}

      {row.resume_token ? (
        <Link className="btn" to={`/student/exams/run?token=${row.resume_token}`} style={{ minHeight: 44 }}>
          {t("student_exams.continue")}
        </Link>
      ) : (
        <button
          type="button"
          className="btn"
          style={{ minHeight: 44 }}
          disabled={!row.available || start.isPending}
          onClick={() => start.mutate()}
        >
          {start.isPending ? t("common.loading") : t("student_exams.start")}
        </button>
      )}
      {!row.available && !row.resume_token ? (
        <p className="small muted">{t(`exams.reason_${row.availability_reason}`)}</p>
      ) : null}
      {row.available ? <p className="small muted">{t("student_exams.start_hint")}</p> : null}

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("student_exams.your_sittings")}</h2>
        {history.data?.attempts.length ? (
          history.data.attempts.map((attempt) => (
            <div className="row small" key={attempt.id} style={{ flexWrap: "wrap", gap: 8, borderBottom: "1px solid var(--border)" }}>
              <span>{t("student_exams.sitting_n", { n: attempt.attempt_number })}</span>
              <span className="chip">{t(`status.${attempt.status}`)}</span>
              {attempt.max_score ? (
                <span>
                  {t("practice.score_of", { got: attempt.score ?? 0, total: attempt.max_score })}
                </span>
              ) : (
                <span className="muted">{t("student_exams.no_mark_yet")}</span>
              )}
              {attempt.needs_review ? <span className="muted">{t("student_exams.waiting_n", { n: attempt.needs_review })}</span> : null}
              {attempt.passed === true ? <span>{t("student_exams.passed")}</span> : null}
              {attempt.passed === false ? <span>{t("student_exams.failed")}</span> : null}
              <span className="spacer" />
              {attempt.status === "in_progress" && attempt.token ? (
                <Link className="btn ghost" to={`/student/exams/run?token=${attempt.token}`}>
                  {t("student_exams.continue")}
                </Link>
              ) : attempt.token ? (
                <Link className="btn ghost" to={`/student/exams/run?token=${attempt.token}`}>
                  {t("student_exams.see_result")}
                </Link>
              ) : null}
            </div>
          ))
        ) : (
          <div className="small muted">{t("student_exams.not_sat_yet")}</div>
        )}
      </div>
    </div>
  );
}

/** Each rule stated once, in the learner's words, and only when the paper actually carries it.
 * A rule that is off is not mentioned: a page that listed every switch would read like a warning. */
function RulesList({ rules }: { rules: RunnerRules }) {
  const { t } = useTranslation();
  const lines: string[] = [];

  if (!rules.allow_previous) lines.push(t("student_exams.rule_no_back"));
  if (rules.restrict_copy_paste) lines.push(t("student_exams.rule_no_copy"));
  if (rules.monitor_tab_switch) {
    lines.push(
      rules.tab_switch_limit
        ? t("student_exams.rule_watches_limit", {
            n: rules.tab_switch_limit,
            action: t(`exams.action_${rules.tab_switch_action || "warn"}`),
          })
        : t("student_exams.rule_watches", {
            action: t(`exams.action_${rules.tab_switch_action || "warn"}`),
          }),
    );
  }
  lines.push(
    rules.resume_after_disconnect
      ? t("student_exams.rule_resumes")
      : t("student_exams.rule_no_resume"),
  );
  if (!rules.auto_submit_on_expiry) lines.push(t("student_exams.rule_no_auto_submit"));
  lines.push(
    rules.feedback_timing === "immediate"
      ? t("student_exams.feedback_now")
      : t("student_exams.feedback_later"),
  );

  return (
    <div className="card stack">
      <h2 style={{ margin: 0 }}>{t("student_exams.rules")}</h2>
      <ul className="small" style={{ margin: 0 }}>
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </div>
  );
}
