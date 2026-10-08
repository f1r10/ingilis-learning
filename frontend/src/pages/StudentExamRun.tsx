// The sitting itself: one paper, one clock, and the result at the end of it.
//
// Three things about this screen are decided somewhere else, and the file is written around that:
//
// * **The clock is the server's.** The countdown starts from `remaining_seconds` on a state read
//   and counts down the seconds that pass after it arrived, but it never decides anything: when it
//   reaches zero the screen re-reads the sitting and shows what the server says. A learner who
//   moves their device clock forward still runs out at the same moment, and a learner whose tab
//   was closed comes back to the deadline that passed.
// * **The token is the whole relationship.** Nothing here names an attempt id. `/start` handed back
//   a token, it lives in the URL, and every call presents it - which is why a second tab, a
//   reconnect and a phone all land on the same sitting with the same answers in it.
// * **Progress is not stored in this file.** The paper comes from `/steps`, which carries each
//   line's own saved answer, and the counts come from the state read. Reloading costs nothing.
//
// A tab that cannot reach the server keeps the answer it was given locally and sends it with the
// hand-in, because an answer that never arrived is worse than one that arrived late. It is not a
// way to keep working past the deadline: the timer runs regardless, and the server grades what it
// holds when the paper closes.
import { useEffect, useMemo, useRef, useState, type SyntheticEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { studentExamsApi, type AnswerReceipt, type AttemptResult, type AttemptStep, type RunnerRules } from "../api/exams";
import { span, when } from "../i18n/format";
import AnswerWidget from "../components/AnswerWidget";

/** The words `/student/attempts` can put in a notice, and what each one says to the learner. A
 * word that is not here prints nothing rather than a code - the backend's English sentence is for
 * a teacher's screen, and a learner who cannot read the reason should still get their paper. */
const NOTICE_KEYS: Record<string, string> = {
  closed: "student_exams.notice_closed",
  expired: "student_exams.notice_expired",
  tab_limit_reached: "student_exams.notice_tab_limit",
  tab_switched: "student_exams.notice_tab_switched",
  already_submitted: "student_exams.notice_already_submitted",
  monitoring_off: "student_exams.notice_monitoring_off",
  withheld: "student_exams.notice_withheld",
};

const OPEN = "in_progress";

function clockText(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return null;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export default function StudentExamRun() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const token = params.get("token") || "";

  const [index, setIndex] = useState(0);
  const [receipts, setReceipts] = useState<Record<string, AnswerReceipt>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [buffered, setBuffered] = useState<string[]>([]);
  const [reachable, setReachable] = useState(() => navigator.onLine);
  const [confirming, setConfirming] = useState(false);
  const started = useRef<Record<string, number>>({});
  const outbox = useRef<Map<string, { response: unknown; seconds: number | null }>>(new Map());

  const state = useQuery({
    queryKey: ["attempt-state", token],
    queryFn: () => studentExamsApi.state(token),
    enabled: Boolean(token),
    // The clock is only honest while it is re-read. Twenty seconds is short enough that a paper
    // closed by the worker's sweep shows up on this screen almost at once.
    refetchInterval: (query) => (query.state.data?.status === OPEN ? 20_000 : false),
  });

  const steps = useQuery({
    queryKey: ["attempt-steps", token],
    queryFn: () => studentExamsApi.steps(token),
    enabled: Boolean(token),
  });

  const rows: AttemptStep[] = useMemo(() => steps.data?.steps || [], [steps.data]);
  const step = rows[Math.min(index, Math.max(0, rows.length - 1))];
  const status = state.data?.status || steps.data?.status || "";
  const closed = Boolean(status && status !== OPEN);
  const rules = state.data?.rules;

  const result = useQuery({
    queryKey: ["attempt-result", token],
    queryFn: () => studentExamsApi.result(token),
    enabled: Boolean(token) && closed,
  });

  // The local countdown. It is seeded from the server's number each time a state read arrives and
  // measures only the seconds since that read; at zero it asks again rather than deciding.
  const base = useRef<{ seconds: number | null; at: number }>({ seconds: null, at: 0 });
  const [left, setLeft] = useState<number | null>(null);
  useEffect(() => {
    if (!state.data) return;
    base.current = { seconds: state.data.remaining_seconds, at: Date.now() };
    setLeft(state.data.remaining_seconds);
    if (state.data.remaining_seconds === null) return;
    const id = window.setInterval(() => {
      const rest = Math.max(0, (base.current.seconds ?? 0) - Math.floor((Date.now() - base.current.at) / 1000));
      setLeft(rest);
      if (rest === 0) {
        window.clearInterval(id);
        qc.invalidateQueries({ queryKey: ["attempt-state", token] });
      }
    }, 1000);
    return () => window.clearInterval(id);
  }, [state.data, token, qc]);

  // Each line times itself from the moment it appears, and the duration is reported rather than
  // inferred: nothing outside the learner's tab can know how long a question was on their screen.
  useEffect(() => {
    if (step) started.current[step.exam_item_id] = Date.now();
  }, [step]);

  useEffect(() => {
    if (!notice) return;
    const id = window.setTimeout(() => setNotice(null), 8000);
    return () => window.clearTimeout(id);
  }, [notice]);

  const answer = useMutation({
    mutationFn: (vars: { exam_item_id: string; response: unknown }) => {
      const began = started.current[vars.exam_item_id];
      return studentExamsApi.answer({
        token,
        exam_item_id: vars.exam_item_id,
        response: vars.response,
        time_spent_seconds: began ? Math.max(0, Math.round((Date.now() - began) / 1000)) : null,
        client_updated_at: new Date().toISOString(),
      });
    },
    onSuccess: (receipt) => {
      setMessage(null);
      setReceipts((prev) => ({ ...prev, [receipt.exam_item_id]: receipt }));
      outbox.current.delete(receipt.exam_item_id);
      setBuffered(Array.from(outbox.current.keys()));
      setNotice(receipt.notice);
      qc.invalidateQueries({ queryKey: ["attempt-state", token] });
    },
    // A refusal that names a closed paper is the deadline arriving while the learner typed. The
    // server has already graded what it holds, so the screen re-reads and shows that.
    onError: (error: unknown, vars) => {
      if (error instanceof ApiError) {
        if (error.status === 409) {
          qc.invalidateQueries({ queryKey: ["attempt-state", token] });
          setMessage(error.message);
          return;
        }
        setMessage(error.message);
        return;
      }
      outbox.current.set(vars.exam_item_id, {
        response: vars.response,
        seconds: (() => {
          const began = started.current[vars.exam_item_id];
          return began ? Math.max(0, Math.round((Date.now() - began) / 1000)) : null;
        })(),
      });
      setBuffered(Array.from(outbox.current.keys()));
      setMessage(t("student_exams.saved_offline"));
    },
  });

  const switchReport = useMutation({
    mutationFn: () => studentExamsApi.tabSwitch(token),
    onSuccess: (receipt) => {
      setNotice(receipt.notice);
      // A rule that hands the paper in costs the learner their sitting, so the screen has to find
      // out at once rather than let them keep typing into a closed paper.
      if (receipt.auto_submitted) qc.invalidateQueries({ queryKey: ["attempt-state", token] });
    },
    // The report is the browser's own account of a page going invisible. If it does not arrive the
    // learner loses nothing they can see, so nothing here claims a number the server did not count.
    onError: () => undefined,
  });

  // Tab switches are watched only while the paper's own rule watches them. `mutate` keeps a stable
  // identity across renders, so the listener is attached once per rule change rather than once per
  // state poll.
  const watched = Boolean(rules?.monitor_tab_switch) && !closed;
  const reportSwitch = switchReport.mutate;
  useEffect(() => {
    if (!token || !watched) return;
    const onVisibility = () => {
      if (document.hidden) reportSwitch();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [token, watched, reportSwitch]);

  useEffect(() => {
    const sync = () => setReachable(navigator.onLine);
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    return () => {
      window.removeEventListener("online", sync);
      window.removeEventListener("offline", sync);
    };
  }, []);

  const submit = useMutation({
    mutationFn: () => {
      const batch = Array.from(outbox.current.entries()).map(([exam_item_id, entry]) => ({
        exam_item_id,
        response: entry.response,
        time_spent_seconds: entry.seconds,
      }));
      return studentExamsApi.submit(token, batch.length ? batch : undefined);
    },
    onSuccess: () => {
      outbox.current.clear();
      setBuffered([]);
      setMessage(null);
      setConfirming(false);
      qc.invalidateQueries({ queryKey: ["attempt-state", token] });
    },
    onError: (error: unknown) =>
      setMessage(error instanceof ApiError ? error.message : t("student_exams.hand_in_failed")),
  });

  if (!token) {
    return (
      <div className="stack">
        <div className="alert error">{t("student_exams.no_paper_open")}</div>
        <Link className="btn secondary" to="/student/exams">
          ‹ {t("student_exams.back")}
        </Link>
      </div>
    );
  }

  if (state.isError || steps.isError) {
    return (
      <div className="stack">
        <Link className="btn secondary" to="/student/exams">
          ‹ {t("student_exams.back")}
        </Link>
        <div className="alert error">
          {t("common.could_not_load")} {((state.error || steps.error) as ApiError).message}
        </div>
      </div>
    );
  }

  if (closed) {
    if (result.isError) {
      return (
        <div className="alert error">
          {t("common.could_not_load")} {(result.error as ApiError).message}
        </div>
      );
    }
    if (!result.data) return <div className="card muted">{t("common.loading")}</div>;
    return (
      <ResultScreen
        result={result.data}
        noticeText={noticeLine(state.data?.notice, t) || noticeLine(result.data.notice, t)}
      />
    );
  }

  if (!state.data || !steps.data) return <div className="card muted">{t("common.loading")}</div>;

  const answeredSet = new Set(rows.filter((row) => row.answered || receipts[row.exam_item_id]).map((row) => row.exam_item_id));
  const unanswered = Math.max(0, (state.data.total_items || rows.length) - answeredSet.size);

  return (
    <div
      className="stack"
      {...(rules?.restrict_copy_paste ? guardCopyPaste() : {})}
    >
      <div className="row small muted" style={{ flexWrap: "wrap", gap: 8 }}>
        <Link className="btn secondary" to="/student/exams">
          ‹ {t("student_exams.back")}
        </Link>
        <span className="spacer" />
        <span>{t("student_exams.sitting_n", { n: state.data.attempt_number })}</span>
        {clockText(left) !== null ? (
          <strong style={{ color: left !== null && left < 300 ? "var(--danger, inherit)" : undefined }}>
            {t("student_exams.time_left")} {clockText(left)}
          </strong>
        ) : (
          <span>{t("student_exams.open_time")}</span>
        )}
        <span>{t("student_exams.answered_n", { done: state.data.answered_items, total: state.data.total_items })}</span>
      </div>

      <p className="small muted" style={{ margin: 0 }}>{state.data.title}</p>

      {notice ? <div className="alert">{noticeLine(notice, t)}</div> : null}
      {message ? <div className="alert error">{message}</div> : null}
      {!reachable ? <div className="alert">{t("student_exams.offline")}</div> : null}
      {buffered.length ? (
        <div className="alert">{t("student_exams.unsent_n", { n: buffered.length })}</div>
      ) : null}
      {rules && !rules.allow_previous ? (
        <p className="small muted">{t("student_exams.rule_no_back")}</p>
      ) : null}
      {rules?.monitor_tab_switch ? <p className="small muted">{ruleWatches(rules, t)}</p> : null}
      {rules?.restrict_copy_paste ? <p className="small muted">{t("student_exams.rule_no_copy")}</p> : null}

      {rows.length === 0 ? (
        <div className="card muted">{t("student_exams.nothing_to_answer")}</div>
      ) : (
        <>
          <div className="row small muted">
            <button
              type="button"
              className="btn ghost"
              disabled={index === 0 || !rules?.allow_previous}
              onClick={() => setIndex(index - 1)}
            >
              ‹ {t("practice.previous")}
            </button>
            <span className="spacer" />
            <span>{t("student_exams.question_n", { n: index + 1, total: rows.length })}</span>
            <span className="spacer" />
            <button
              type="button"
              className="btn ghost"
              disabled={index >= rows.length - 1}
              onClick={() => setIndex(index + 1)}
            >
              {t("practice.next")} ›
            </button>
          </div>

          {step ? (
            <div className="stack">
              {step.section_title ? <p className="small muted">{step.section_title}</p> : null}
              <AnswerWidget
                view={step.view}
                stepKey={step.exam_item_id}
                initial={step.saved}
                heading={t("student_exams.question_n", { n: step.position + 1, total: rows.length })}
                busy={answer.isPending && answer.variables?.exam_item_id === step.exam_item_id}
                result={receipts[step.exam_item_id] || null}
                onSubmit={(payload) =>
                  answer.mutate({ exam_item_id: step.exam_item_id, response: payload })
                }
              />
              {step.saved !== null && step.saved !== undefined && !receipts[step.exam_item_id] ? (
                <p className="small muted">{t("student_exams.saved_answer")}</p>
              ) : null}
            </div>
          ) : null}

          <div className="card row small" style={{ flexWrap: "wrap", gap: 6 }}>
            {rows.map((row, position) => (
              <button
                type="button"
                className="btn ghost"
                key={row.exam_item_id}
                aria-pressed={position === index}
                style={{
                  minWidth: 40,
                  fontWeight: position === index ? 700 : 400,
                  borderColor: position === index ? "var(--accent)" : undefined,
                  opacity: !rules?.allow_previous && position < index ? 0.4 : 1,
                }}
                onClick={() => {
                  // A forward-only paper still lets the learner see where they have been; it just
                  // does not hand them the answer boxes again.
                  if (!rules?.allow_previous && position < index) return;
                  setIndex(position);
                }}
              >
                {position + 1}
                {answeredSet.has(row.exam_item_id) ? " ·" : ""}
              </button>
            ))}
          </div>

          <div className="card stack">
            <div className="row small">
              <span>
                {unanswered === 0
                  ? t("student_exams.all_answered")
                  : t("student_exams.unanswered_n", { n: unanswered })}
              </span>
              <span className="spacer" />
              <span className="muted">
                {rules?.feedback_timing === "immediate"
                  ? t("student_exams.feedback_now")
                  : t("student_exams.feedback_later")}
              </span>
            </div>
            {buffered.length ? (
              <p className="small muted">{t("student_exams.unsent_send_note")}</p>
            ) : null}
            <button
              type="button"
              className="btn"
              style={{ minHeight: 44 }}
              disabled={submit.isPending}
              onClick={() => (confirming ? submit.mutate() : setConfirming(true))}
            >
              {submit.isPending
                ? t("common.loading")
                : confirming
                  ? t("student_exams.confirm_hand_in")
                  : t("student_exams.hand_in")}
            </button>
            {confirming ? (
              <button type="button" className="btn ghost" onClick={() => setConfirming(false)}>
                {t("common.cancel")}
              </button>
            ) : null}
            <p className="small muted">{t("student_exams.hand_in_final")}</p>
          </div>
        </>
      )}
    </div>
  );
}

/** Copy and paste switched off by the teacher's rule. A browser cannot make this unbreakable, and
 * the platform does not claim it does: the line is the rule made visible on the paper. */
function guardCopyPaste() {
  const stop = (event: SyntheticEvent) => event.preventDefault();
  return {
    onCopy: stop,
    onCut: stop,
    onPaste: stop,
    onDrop: stop,
  };
}

function noticeLine(word: string | null | undefined, t: (key: string) => string) {
  if (!word) return null;
  const key = NOTICE_KEYS[word];
  return key ? t(key) : null;
}

function ruleWatches(rules: RunnerRules, t: (key: string, options?: any) => string) {
  const action = t(`exams.action_${rules.tab_switch_action || "warn"}`);
  return rules.tab_switch_limit
    ? t("student_exams.rule_watches_limit", { n: rules.tab_switch_limit, action })
    : t("student_exams.rule_watches", { action });
}

/** The end of the paper, as far as its own visibility rules reach.
 * `visible` is the whole answer: when it is false the marks exist somewhere but not for this
 * screen, and `state` says which of the three rules is holding them. */
function ResultScreen({
  result,
  noticeText,
}: {
  result: AttemptResult;
  noticeText: string | null;
}) {
  const { t } = useTranslation();

  return (
    <div className="stack">
      <div className="row">
        <Link className="btn secondary" to="/student/exams">
          ‹ {t("student_exams.back")}
        </Link>
        <span className="spacer" />
        <span className="chip">{t(`status.${result.status}`)}</span>
      </div>

      <h1 style={{ margin: 0 }}>{result.title}</h1>
      {noticeText ? <div className="alert">{noticeText}</div> : null}
      <p className="small muted" style={{ margin: 0 }}>
        {result.submitted_at
          ? t("student_exams.handed_in_at", { when: when(result.submitted_at) })
          : t("student_exams.not_handed_in")}
        {result.server_seconds_used !== null
          ? ` · ${t("student_exams.took", { time: span(result.server_seconds_used) })}`
          : ""}
      </p>

      {!result.visible ? (
        <div className="card stack">
          <strong>{t("student_exams.marks_not_ready")}</strong>
          <p className="small" style={{ margin: 0 }}>
            {t(`student_exams.held_${result.state}`)}
          </p>
          {result.manual_count ? (
            <p className="small muted">{t("student_exams.waiting_n", { n: result.manual_count })}</p>
          ) : null}
          <p className="small muted">{t("student_exams.we_will_tell_you")}</p>
        </div>
      ) : (
        <div className="card stack">
          <div className="row" style={{ flexWrap: "wrap", gap: 16 }}>
            <div>
              <div className="muted small">{t("practice.your_score")}</div>
              {result.score !== null && result.max_score !== null ? (
                <strong style={{ fontSize: "1.4rem" }}>
                  {t("practice.score_of", { got: result.score, total: result.max_score })}
                </strong>
              ) : (
                <strong>{t("student_exams.no_mark_yet")}</strong>
              )}
            </div>
            <div className="small">
              <div>{t("student_exams.answered_n", { done: result.answered_items, total: result.total_items })}</div>
              {result.show_correct_answers ? <div>{t("practice.right_n", { n: result.correct_count })}</div> : null}
              {/* Said only when there is one, because "Partly right: 0" is noise next to a total
                  that already adds up. */}
              {result.show_correct_answers && result.partial_count ? (
                <div>{t("practice.partly_right_n", { n: result.partial_count })}</div>
              ) : null}
              {result.show_correct_answers ? <div>{t("practice.wrong_n", { n: result.incorrect_count })}</div> : null}
              {result.manual_count ? <div>{t("student_exams.waiting_n", { n: result.manual_count })}</div> : null}
              {result.passed === true ? <div>{t("student_exams.passed")}</div> : null}
              {result.passed === false ? <div>{t("student_exams.failed")}</div> : null}
              {result.passed === null && result.passing_score !== null ? (
                <div className="muted">{t("student_exams.pass_undecided")}</div>
              ) : null}
              {result.passing_score !== null ? (
                <div className="muted">{t("student_exams.passing_at", { pct: result.passing_score })}</div>
              ) : null}
            </div>
          </div>
        </div>
      )}

      {result.visible && result.lines.length ? (
        <div className="card stack">
          <h2 style={{ margin: 0 }}>{t("practice.results_title")}</h2>
          {/* Said once for the whole card. Repeating it on every line would turn one rule into five
              sentences the learner has to skip through to reach their marks. */}
          {result.visible && !result.show_correct_answers ? (
            <p className="small muted" style={{ margin: 0 }}>
              {t("practice.verdict_withheld")}
            </p>
          ) : null}
          {result.lines.map((line, position) => (
            <div
              className="stack"
              key={line.exam_item_id}
              style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}
            >
              <div className="small">
                <span className="muted">{position + 1}. </span>
                {line.prompt || t("practice.content_gone")}
              </div>
              <div className="row small" style={{ gap: 8 }}>
                {!line.answered ? <span className="chip">{t("student_exams.left_blank")}</span> : null}
                {line.requires_manual ? (
                  <span className="chip">{t("practice.not_auto_marked")}</span>
                ) : line.correct === true ? (
                  <span className="chip">{t("practice.right")}</span>
                ) : line.correct === false ? (
                  // Not the full mark is not the same as wrong, and a line that says both beside
                  // "2 of 3" reads like a broken screen.
                  <span className="chip">
                    {line.score > 0 ? t("practice.not_full") : t("practice.wrong")}
                  </span>
                ) : !result.show_correct_answers ? (
                  // The verdict is absent because the paper keeps its answers back, not because
                  // nobody graded the line, so saying "not marked" here would accuse a teacher who
                  // finished the job. The card above already told the learner which of the two it is.
                  null
                ) : (
                  <span className="muted">{t("practice.not_marked")}</span>
                )}
                <span className="muted">
                  {t("practice.score_of", { got: line.score, total: line.max_score })}
                </span>
              </div>
              {line.reviewer_note ? (
                <p className="small" style={{ margin: 0 }}>
                  <span className="muted">{t("student_exams.teacher_note_label")}</span>{" "}
                  {line.reviewer_note}
                </p>
              ) : null}
              {line.explanation ? <p className="small" style={{ margin: 0 }}>{line.explanation}</p> : null}
            </div>
          ))}
        </div>
      ) : null}

      {result.visible && result.feedback.length ? (
        <div className="card stack">
          <h2 style={{ margin: 0 }}>{t("student_exams.teacher_notes")}</h2>
          {result.feedback.map((note) => (
            <div className="small" key={note.id}>
              <div>{note.body}</div>
              <div className="muted">{when(note.created_at)}</div>
            </div>
          ))}
        </div>
      ) : null}

      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <Link className="btn secondary" to={`/student/exams/${result.exam_id}`}>
          {t("student_exams.see_paper")}
        </Link>
        <Link className="btn ghost" to="/student/exams">
          {t("student_exams.back")}
        </Link>
      </div>
      <p className="small muted">{t("student_exams.result_final")}</p>
    </div>
  );
}
