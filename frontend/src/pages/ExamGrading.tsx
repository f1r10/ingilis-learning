// The marking room: what the engine could not decide, one answer at a time.
//
// An essay, a summary or a speaking-style answer reaches this screen because no rule in the
// backend could call it right or wrong. That is why the queue is made of *answers* rather than
// sittings: a teacher who marks one line of one paper moves that paper's total, and re-marking it
// later is a correction with a record, not a rewrite.
//
// Three things this screen refuses to invent:
//
// * **The scale the learner was asked in.** `max_score` comes from the version pinned to the
//   sitting, so a question the bank has since rewritten is still marked out of what it was worth
//   on the day. The backend refuses a mark above that number and says so.
// * **An assistant's opinion that was never produced.** `ai_suggestion` is empty until Phase 12's
//   adapters actually generate one; nothing here shows a probable mark, because a queue that
//   looked like it had help would push a teacher to agree with a number nobody calculated.
// * **Evidence from a tab count.** The switch line is the browser reporting that the page became
//   invisible, and the server's own sentence about it is printed unchanged. It is not a finding
//   about what was opened, and the wording says so.
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { examsApi, gradingApi, type AttemptDetail, type ReviewRow } from "../api/exams";
import { span, when } from "../i18n/format";

const PAGE_SIZE = 25;

export default function ExamGrading() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [open, setOpen] = useState<string | null>(null);

  const examId = params.get("exam_id") || "";
  const studentId = params.get("student_id") || "";
  const reviewed = params.get("reviewed") === "1";
  const page = Number(params.get("page") || 1);
  const attemptId = params.get("attempt") || "";

  const setFilter = (patch: Record<string, string | number | boolean>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => {
      if (value === "" || value === undefined || value === false) next.delete(key);
      else next.set(key, value === true ? "1" : String(value));
    });
    if (!("page" in patch)) next.delete("page");
    setParams(next);
  };

  const summary = useQuery({ queryKey: ["grading-summary"], queryFn: gradingApi.summary });
  const queue = useQuery({
    queryKey: ["grading-queue", examId, studentId, reviewed, page],
    queryFn: () => gradingApi.queue({ exam_id: examId, student_id: studentId, reviewed, page, page_size: PAGE_SIZE }),
  });
  const attempt = useQuery({
    queryKey: ["grading-attempt", attemptId],
    queryFn: () => examsApi.attempt(attemptId),
    enabled: Boolean(attemptId),
  });

  const pages = Math.max(1, Math.ceil((queue.data?.total ?? 0) / PAGE_SIZE));
  const nameOf = (id: string) => {
    const row = (queue.data?.items || []).find((item) => item.student_id === id);
    return row?.student_name || id.slice(0, 8);
  };

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["grading-queue"] });
    qc.invalidateQueries({ queryKey: ["grading-summary"] });
    qc.invalidateQueries({ queryKey: ["exams"] });
  };

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <h1 style={{ margin: 0 }}>{t("grading.title")}</h1>
        <span className="spacer" />
        <Link className="btn secondary" to="/exams">
          ‹ {t("exams.back")}
        </Link>
      </div>
      <p className="muted small">{t("grading.hint")}</p>

      {summary.isError ? (
        <div className="alert error">{t("common.could_not_load")} {(summary.error as ApiError).message}</div>
      ) : null}

      <div className="two-col">
        <div className="card stack">
          <h2 style={{ margin: 0 }}>{t("grading.papers_waiting")}</h2>
          {summary.data?.exams.length ? (
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {summary.data.exams.map((entry) => (
                <button
                  key={entry.exam_id}
                  type="button"
                  className={`chip ${examId === entry.exam_id ? "on" : ""}`}
                  onClick={() => setFilter({ exam_id: examId === entry.exam_id ? "" : entry.exam_id })}
                >
                  {entry.title || entry.exam_id.slice(0, 8)} · {t("grading.pending_n", { n: entry.pending })}
                </button>
              ))}
            </div>
          ) : (
            <div className="small muted">{t("grading.nothing_waiting")}</div>
          )}
        </div>
        <div className="card stack">
          <h2 style={{ margin: 0 }}>{t("grading.learners_waiting")}</h2>
          {summary.data?.students.length ? (
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {summary.data.students.map((entry) => (
                <button
                  key={entry.student_id}
                  type="button"
                  className={`chip ${studentId === entry.student_id ? "on" : ""}`}
                  onClick={() => setFilter({ student_id: studentId === entry.student_id ? "" : entry.student_id })}
                >
                  {entry.name || entry.student_id.slice(0, 8)} · {t("grading.pending_n", { n: entry.pending })}
                </button>
              ))}
            </div>
          ) : (
            <div className="small muted">{t("grading.nothing_waiting")}</div>
          )}
        </div>
      </div>

      <div className="row small muted" style={{ gap: 12, flexWrap: "wrap" }}>
        <span>{t("grading.pending_total", { n: summary.data?.pending ?? 0 })}</span>
        <span>{t("grading.reviewed_today", { n: summary.data?.reviewed_today ?? 0 })}</span>
      </div>

      {attemptId ? (
        <AttemptPanel
          attempt={attempt.data}
          error={attempt.error as ApiError | null}
          loading={attempt.isLoading}
          onClose={() => setFilter({ attempt: "" })}
          onChanged={() => {
            qc.invalidateQueries({ queryKey: ["grading-attempt", attemptId] });
            refresh();
          }}
          studentName={nameOf}
        />
      ) : null}

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <button
          type="button"
          className="btn secondary"
          aria-pressed={!reviewed}
          onClick={() => setFilter({ reviewed: false })}
        >
          {t("grading.show_waiting")}
        </button>
        <button
          type="button"
          className="btn secondary"
          aria-pressed={reviewed}
          onClick={() => setFilter({ reviewed: true })}
        >
          {t("grading.show_marked")}
        </button>
        <span className="spacer" />
        {examId ? (
          <button type="button" className="btn ghost" onClick={() => setFilter({ exam_id: "" })}>
            {t("grading.every_paper")}
          </button>
        ) : null}
        {studentId ? (
          <button type="button" className="btn ghost" onClick={() => setFilter({ student_id: "" })}>
            {t("grading.every_learner")}
          </button>
        ) : null}
      </div>

      {queue.isError ? (
        <div className="alert error">{t("common.could_not_load")} {(queue.error as ApiError).message}</div>
      ) : null}

      <div className="stack">
        {(queue.data?.items || []).map((row) => (
          <QueueRow
            key={row.id}
            row={row}
            open={open === row.id}
            onToggle={() => setOpen(open === row.id ? null : row.id)}
            onGraded={() => {
              setOpen(null);
              refresh();
              if (attemptId) qc.invalidateQueries({ queryKey: ["grading-attempt", attemptId] });
            }}
            onOpenAttempt={() => setFilter({ attempt: row.attempt_id })}
          />
        ))}
        {!queue.isLoading && !(queue.data?.items || []).length ? (
          <div className="card muted small">
            {reviewed ? t("grading.none_marked") : t("grading.nothing_waiting")}
          </div>
        ) : null}
      </div>

      <div className="row">
        <span className="small muted">{t("exams.showing", { total: queue.data?.total ?? 0 })}</span>
        <span className="spacer" />
        <button type="button" className="btn secondary" disabled={page <= 1} onClick={() => setFilter({ page: page - 1 })}>
          ‹
        </button>
        <span className="small">
          {page} / {pages}
        </span>
        <button type="button" className="btn secondary" disabled={page >= pages} onClick={() => setFilter({ page: page + 1 })}>
          ›
        </button>
      </div>
    </div>
  );
}

function QueueRow({
  row,
  open,
  onToggle,
  onGraded,
  onOpenAttempt,
}: {
  row: ReviewRow;
  open: boolean;
  onToggle: () => void;
  onGraded: () => void;
  onOpenAttempt: () => void;
}) {
  const { t } = useTranslation();

  return (
    <div className="card stack">
      <div className="row small muted" style={{ flexWrap: "wrap", gap: 8 }}>
        <span>{row.exam_title || t("grading.paper_unnamed")}</span>
        <span>· {row.student_name || row.student_id.slice(0, 8)}</span>
        {row.question_type ? <span>· {t(`questions.type_${row.question_type}`)}</span> : null}
        {row.submitted_at ? <span>· {when(row.submitted_at)}</span> : null}
        <span className="spacer" />
        <span>{t("grading.out_of_n", { n: row.max_score })}</span>
      </div>

      <div className="small" style={{ whiteSpace: "pre-wrap" }}>{row.prompt || t("exams.no_prompt")}</div>

      <div className="stack" style={{ gap: 4 }}>
        <strong className="small">{t("grading.learner_wrote")}</strong>
        {describe(row.response, t).map((line, index) => (
          <div className="small" key={index} style={{ whiteSpace: "pre-wrap" }}>{line}</div>
        ))}
      </div>

      {row.ai_suggestion ? <Suggestion value={row.ai_suggestion} /> : null}

      <div className="row small" style={{ gap: 8, flexWrap: "wrap" }}>
        {row.reviewed ? (
          <span className="chip">{t("grading.marked_as", { score: row.final_score ?? 0 })}</span>
        ) : (
          <span className="chip">{t("grading.waiting_for_mark")}</span>
        )}
        {row.reviewer_note ? <span className="muted">{t("grading.note_yours")}: {row.reviewer_note}</span> : null}
        <span className="spacer" />
        <button type="button" className="btn ghost" onClick={onOpenAttempt}>
          {t("grading.see_whole_paper")}
        </button>
        {row.question_id ? (
          // The bank screen shows the question's current wording, which may not be what this
          // sitting asked - so the sentence has to say that rather than let the teacher compare
          // the wrong text.
          <Link className="btn ghost" to={`/questions/${row.question_id}`}>
            {t("grading.open_in_bank")}
          </Link>
        ) : null}
        <button type="button" className="btn secondary" onClick={onToggle}>
          {row.reviewed ? t("grading.re_mark") : t("grading.mark")}
        </button>
      </div>

      {open ? <MarkPanel row={row} onDone={onGraded} /> : null}
    </div>
  );
}

function MarkPanel({ row, onDone }: { row: ReviewRow; onDone: () => void }) {
  const { t } = useTranslation();
  const [score, setScore] = useState<string>(row.final_score === null ? "" : String(row.final_score));
  const [correct, setCorrect] = useState<boolean | null>(null);
  const [note, setNote] = useState(row.reviewer_note || "");
  const [error, setError] = useState<string | null>(null);

  const grade = useMutation({
    mutationFn: () =>
      gradingApi.grade(row.id, {
        score: score === "" ? null : Number(score),
        correct,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: onDone,
    // "that answer is worth 4, so it cannot be given 7" is the sentence that explains the refusal,
    // so it is shown rather than replaced with a generic failure.
    onError: (e: ApiError) => setError(e.message),
  });

  return (
    <div className="stack" style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
      {error ? <div className="alert error">{error}</div> : null}
      <div className="two-col">
        <div className="field">
          <label>{t("grading.mark")}</label>
          <input
            className="input"
            type="number"
            min={0}
            max={row.max_score}
            step={0.5}
            value={score}
            onChange={(e) => setScore(e.target.value)}
          />
          <div className="small muted">{t("grading.mark_hint", { n: row.max_score })}</div>
        </div>
        <div className="field">
          <label>{t("grading.verdict")}</label>
          <div className="row" style={{ gap: 6 }}>
            <button
              type="button"
              className="btn secondary"
              aria-pressed={correct === null}
              onClick={() => setCorrect(null)}
            >
              {t("grading.verdict_from_mark")}
            </button>
            <button
              type="button"
              className="btn secondary"
              aria-pressed={correct === true}
              style={{ borderColor: correct === true ? "var(--accent)" : undefined }}
              onClick={() => setCorrect(true)}
            >
              {t("grading.right")}
            </button>
            <button
              type="button"
              className="btn secondary"
              aria-pressed={correct === false}
              style={{ borderColor: correct === false ? "var(--accent)" : undefined }}
              onClick={() => setCorrect(false)}
            >
              {t("grading.wrong")}
            </button>
          </div>
          <div className="small muted">{t("grading.verdict_hint")}</div>
        </div>
      </div>
      <div className="field">
        <label>{t("grading.note_to_this_answer")}</label>
        <textarea className="input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        <div className="small muted">{t("grading.note_hint")}</div>
      </div>
      <div className="row" style={{ gap: 8 }}>
        <button type="button" className="btn" disabled={grade.isPending} onClick={() => grade.mutate()}>
          {grade.isPending ? t("common.loading") : row.reviewed ? t("grading.save_new_mark") : t("grading.give_mark")}
        </button>
        {score === "" ? <span className="small muted">{t("grading.no_mark_note")}</span> : null}
      </div>
    </div>
  );
}

/** A suggestion only ever appears here if some adapter wrote it, and it is printed as what it is:
 * one possible mark, never the mark. Nothing in this panel applies it. */
function Suggestion({ value }: { value: Record<string, unknown> }) {
  const { t } = useTranslation();
  const score = typeof value.score === "number" ? value.score : null;
  const verdict = typeof value.verdict === "boolean" ? value.verdict : null;
  const reason = [value.reason, value.note, value.comment].find((entry) => typeof entry === "string") as
    | string
    | undefined;

  if (score === null && verdict === null && !reason) return null;

  return (
    <div className="card small">
      <strong>{t("grading.assistant_suggested")}</strong>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {score !== null ? <span>{t("grading.suggested_mark", { n: score })}</span> : null}
        {verdict !== null ? <span>{verdict ? t("grading.right") : t("grading.wrong")}</span> : null}
      </div>
      {reason ? <div className="muted" style={{ whiteSpace: "pre-wrap" }}>{reason}</div> : null}
      <div className="muted">{t("grading.suggestion_not_applied")}</div>
    </div>
  );
}

function AttemptPanel({
  attempt,
  error,
  loading,
  onClose,
  onChanged,
  studentName,
}: {
  attempt: AttemptDetail | undefined;
  error: ApiError | null;
  loading: boolean;
  onClose: () => void;
  onChanged: () => void;
  studentName: (id: string) => string;
}) {
  const { t } = useTranslation();
  if (error) {
    return (
      <div className="card stack">
        <div className="alert error">{t("common.could_not_load")} {error.message}</div>
        <button type="button" className="btn secondary" onClick={onClose}>
          {t("grading.close_paper")}
        </button>
      </div>
    );
  }
  if (!attempt) {
    return <div className="card muted">{loading ? t("common.loading") : null}</div>;
  }

  const waiting = attempt.answers.filter((line) => line.needs_manual_review).length;
  // This paper's own rule about leaving the tab, in words the teacher set when they wrote it:
  // "warn" is the same rule on the authoring screen and on the learner's rules list.
  const watchAction = t(`exams.action_${attempt.tab_switch_action || "warn"}`);

  return (
    <div className="card stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ margin: 0 }}>{attempt.exam_title}</h2>
        <span className="spacer" />
        <button type="button" className="btn secondary" onClick={onClose}>
          {t("grading.close_paper")}
        </button>
      </div>

      <div className="row small muted" style={{ flexWrap: "wrap", gap: 8 }}>
        <span>{attempt.student_name || studentName(attempt.student_id)}</span>
        <span>{t("exams.sitting_n_number", { n: attempt.attempt_number })}</span>
        <span>{t(`status.${attempt.status}`)}</span>
        {attempt.server_seconds_used !== null ? (
          <span>{t("exams.used_n", { time: span(attempt.server_seconds_used) })}</span>
        ) : null}
        {attempt.submitted_at ? <span>{when(attempt.submitted_at)}</span> : null}
      </div>

      <div className="row small" style={{ gap: 12, flexWrap: "wrap" }}>
        {attempt.max_score ? (
          <strong>{t("exams.score_of", { got: attempt.score ?? 0, total: attempt.max_score })}</strong>
        ) : (
          <span className="muted">{t("grading.not_added_up")}</span>
        )}
        {attempt.passed === true ? <span className="chip">{t("grading.passed")}</span> : null}
        {attempt.passed === false ? <span className="chip">{t("grading.not_passed")}</span> : null}
        {/* `passed` is empty two different ways: a paper with no pass mark never has a verdict, and
            a paper with one has a verdict that is not in yet. Only the second can be waited for. */}
        {attempt.passed === null ? (
          <span className="muted small">
            {attempt.passing_score === null ? t("grading.pass_unknown") : t("grading.pass_undecided")}
          </span>
        ) : null}
        {attempt.passing_score !== null ? (
          <span className="muted small">{t("grading.pass_mark", { pct: attempt.passing_score })}</span>
        ) : null}
        {waiting ? <span className="chip">{t("grading.pending_n", { n: waiting })}</span> : null}
      </div>

      {attempt.monitor_tab_switch ? (
        <div className="card small">
          <strong>{t("grading.switches", { n: attempt.tab_switches })}</strong>
          {/* The count, the rule and the limit arrive as separate fields, and this screen makes the
              sentence: a count of tabs is the browser reporting the paper left the screen, and
              nothing here says what was opened instead. */}
          <div className="muted">
            {attempt.tab_switch_limit === null
              ? t("grading.rule_no_limit", { action: watchAction })
              : t("grading.rule_limit", { action: watchAction, n: attempt.tab_switch_limit })}
          </div>
          <div className="muted">{t("grading.switch_source")}</div>
        </div>
      ) : null}

      <div className="stack">
        {attempt.answers.map((line) => (
          <div
            key={line.exam_item_id}
            className="stack"
            style={{ borderBottom: "1px solid var(--border)", paddingBottom: 6, gap: 2 }}
          >
            <div className="row small muted" style={{ gap: 8, flexWrap: "wrap" }}>
              <span>{line.position + 1}</span>
              <span style={{ flex: 1 }}>{line.prompt || t("exams.no_prompt")}</span>
              {line.version !== null ? <span>{t("exams.pinned_v", { v: line.version })}</span> : null}
              <span>{t("grading.out_of_n", { n: line.max_score })}</span>
            </div>
            <div className="small">
              {describe(line.response, t).map((entry, index) => (
                <div key={index} style={{ whiteSpace: "pre-wrap" }}>{entry}</div>
              ))}
            </div>
            <div className="row small" style={{ gap: 8, flexWrap: "wrap" }}>
              {line.graded ? (
                <span className="chip">{t("grading.scored_n", { n: line.score ?? 0 })}</span>
              ) : line.needs_manual_review ? (
                <span className="chip">{t("grading.waiting_for_mark")}</span>
              ) : (
                <span className="muted">{t("grading.unmarked")}</span>
              )}
              {line.answered ? null : <span className="muted">{t("grading.no_answer")}</span>}
              {line.correct === true ? <span className="muted">{t("grading.right")}</span> : null}
              {/* A mark below the full one is what "follow the mark" records as not right, and a
                  line that took part of its mark is not wrong. The screen says which of the two. */}
              {line.correct === false ? (
                <span className="muted">
                  {line.score && line.score > 0 ? t("grading.not_full") : t("grading.wrong")}
                </span>
              ) : null}
              {line.flagged ? <span className="muted">{t("grading.flagged")}</span> : null}
              {line.time_spent_seconds ? (
                <span className="muted">{t("grading.took", { time: span(line.time_spent_seconds) })}</span>
              ) : null}
              {line.changed_count ? (
                <span className="muted">{t("grading.changed_n", { n: line.changed_count })}</span>
              ) : null}
            </div>
          </div>
        ))}
      </div>

      <FeedbackPanel attempt={attempt} onChanged={onChanged} />
    </div>
  );
}

function FeedbackPanel({
  attempt,
  onChanged,
}: {
  attempt: AttemptDetail;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);

  const add = useMutation({
    mutationFn: () => gradingApi.addFeedback(attempt.id, { body }),
    onSuccess: () => {
      setBody("");
      setError(null);
      onChanged();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const remove = useMutation({
    mutationFn: (id: string) => gradingApi.removeFeedback(id),
    onSuccess: onChanged,
    onError: (e: ApiError) => setError(e.message),
  });

  return (
    <div className="stack" style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
      <strong className="small">{t("grading.feedback_title")}</strong>
      <p className="small muted" style={{ margin: 0 }}>
        {t("grading.feedback_hint")}
      </p>
      {error ? <div className="alert error">{error}</div> : null}
      {attempt.feedback.map((entry) => (
        <div className="row small" key={entry.id} style={{ gap: 8, alignItems: "flex-start" }}>
          <div style={{ flex: 1, whiteSpace: "pre-wrap" }}>{entry.body}</div>
          <span className="muted">{when(entry.created_at)}</span>
          <button type="button" className="btn ghost" onClick={() => remove.mutate(entry.id)}>
            {t("grading.take_back")}
          </button>
        </div>
      ))}
      {!attempt.feedback.length ? <div className="small muted">{t("grading.no_feedback")}</div> : null}
      <textarea
        className="input"
        rows={3}
        placeholder={t("grading.write_feedback")}
        value={body}
        onChange={(e) => setBody(e.target.value)}
      />
      <div className="row" style={{ gap: 8 }}>
        <button
          type="button"
          className="btn secondary"
          disabled={!body.trim() || add.isPending}
          onClick={() => add.mutate()}
        >
          {t("grading.send_feedback")}
        </button>
        <span className="small muted">{t("grading.feedback_when_visible")}</span>
      </div>
    </div>
  );
}

/** The learner's stored answer in the shape the grader keeps it, read back as words.
 *
 * `matching` and `ordering` are saved as references rather than text, so their lines name the
 * reference - the words behind it belong to the pinned version, which is why the queue keeps a
 * link to the question and never claims to show the pair in full.
 */
function describe(response: unknown, t: (key: string, options?: any) => string): string[] {
  if (response === null || response === undefined) return [t("grading.no_answer")];
  const value = response as Record<string, any>;

  if (typeof value.text === "string") return value.text.trim() ? [value.text] : [t("grading.no_answer")];
  if (typeof value.option_index === "number") return [t("grading.option_letter", { letter: letter(value.option_index) })];
  if (Array.isArray(value.option_indexes)) {
    return value.option_indexes.length
      ? [value.option_indexes.map((index: number) => letter(index)).join(", ")]
      : [t("grading.no_answer")];
  }
  if (typeof value.value === "boolean") return [value.value ? t("questions.true") : t("questions.false")];
  if (Array.isArray(value.blanks)) {
    return value.blanks.map((entry: unknown, index: number) =>
      `${index + 1}. ${String(entry ?? "").trim() || "—"}`,
    );
  }
  if (Array.isArray(value.pairs)) {
    return value.pairs.length
      ? value.pairs.map((pair: any) => `${pair?.left_ref ?? "?"} → ${pair?.right_ref ?? "—"}`)
      : [t("grading.no_answer")];
  }
  if (Array.isArray(value.order)) {
    return value.order.length ? [value.order.map((entry: unknown) => String(entry)).join(" › ")] : [t("grading.no_answer")];
  }
  // An answer this screen cannot word is still shown as filed, never as empty: the mark belongs to
  // the teacher, and a blank line would suggest the learner wrote nothing.
  return [t("grading.unreadable_answer")];
}

const letter = (index: number) => String.fromCharCode(65 + (index % 26));
