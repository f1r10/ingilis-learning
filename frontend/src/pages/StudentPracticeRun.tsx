// Working through a practice run, and the result at the end of it.
//
// The screen is driven by two reads: `/runs/{id}/steps`, which serves the exercises in the
// order the run was opened in, and `/runs/{id}`, which is rebuilt from the activity log and
// says what has been answered so far. That split is why a run survives a closed tab and a
// different device: nothing about progress is kept in this file.
//
// The verdict a learner sees depends on the catalog's feedback timing, not on this screen.
// Under `after_session` each answer comes back `withheld`, and the marks appear when the
// run is finished - so the finish button is the moment the learner has been working towards,
// and a run that is already finished opens straight onto its results.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import {
  favoritesApi,
  practiceApi,
  type AnswerPayload,
  type AnswerResult,
  type RunSummary,
} from "../api/practice";
import PracticeStep from "../components/PracticeStep";

export default function StudentPracticeRun() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const session = params.get("session") || "";
  const [index, setIndex] = useState(0);
  const [results, setResults] = useState<Record<string, AnswerResult>>({});
  const [message, setMessage] = useState<string | null>(null);
  const started = useRef<Record<string, number>>({});

  const run = useQuery({
    queryKey: ["practice-run", session],
    queryFn: () => practiceApi.steps(session),
    enabled: Boolean(session),
  });
  const summary = useQuery({
    queryKey: ["practice-summary", session],
    queryFn: () => practiceApi.run(session),
    enabled: Boolean(session),
  });

  const steps = run.data?.steps || [];
  const step = steps[Math.min(index, Math.max(0, steps.length - 1))];
  const questionIds = useMemo(
    () => steps.filter((each) => each.kind === "question").map((each) => String(each.view?.id ?? "")),
    [steps],
  );

  const known = useQuery({
    queryKey: ["practice-known", run.data?.catalog_id],
    queryFn: () => practiceApi.known(run.data!.catalog_id),
    enabled: Boolean(run.data?.known_states_enabled && run.data?.catalog_id),
  });
  const favorites = useQuery({ queryKey: ["favorites"], queryFn: favoritesApi.list });

  // Each step starts its own clock when it appears. The duration is reported, not inferred:
  // the backend stores what the learner's screen measured and nothing at all if it did not.
  useEffect(() => {
    if (step) started.current[`${step.kind}:${step.ref_id}`] = Date.now();
  }, [step]);

  const answer = useMutation({
    mutationFn: ({ questionId, payload }: { questionId: string; payload: AnswerPayload }) => {
      const key = `question:${questionId}`;
      const began = started.current[key];
      return practiceApi.answer({
        session_id: session,
        question_id: questionId,
        response: payload,
        time_spent_seconds: began ? Math.max(0, Math.round((Date.now() - began) / 1000)) : null,
      });
    },
    onSuccess: (result) => {
      setMessage(null);
      setResults((prev) => ({ ...prev, [result.question_id]: result }));
      qc.invalidateQueries({ queryKey: ["practice-summary", session] });
    },
    // A rejected answer leaves no event, so the learner is told plainly and can send it again.
    onError: (e: ApiError) => setMessage(e.message),
  });

  const mark = useMutation({
    mutationFn: ({ refId, state }: { refId: string; state: string }) =>
      practiceApi.mark(run.data!.catalog_id, refId, state),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["practice-known", run.data?.catalog_id] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const favorite = useMutation({
    mutationFn: async ({ kind, refId, saved }: { kind: string; refId: string; saved: boolean }) => {
      if (saved) await favoritesApi.remove(kind, refId);
      else await favoritesApi.add(kind, refId);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["favorites"] }),
    onError: (e: ApiError) => setMessage(e.message),
  });

  const finish = useMutation({
    mutationFn: () => practiceApi.finish(session),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["practice-summary", session] });
      qc.invalidateQueries({ queryKey: ["practice"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  // "Do it again" opens a second run rather than rewriting the first: the answers already
  // given are the learner's own history, and a fresh session is what the server logs.
  const retry = useMutation({
    mutationFn: () =>
      practiceApi.start(run.data!.catalog_id, { shuffle: run.data?.shuffle ? true : undefined }),
    onSuccess: (fresh) => {
      setMessage(null);
      setResults({});
      setIndex(0);
      started.current = {};
      qc.invalidateQueries({ queryKey: ["practice"] });
      const next = new URLSearchParams(params);
      next.set("session", fresh.session_id);
      setParams(next);
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  if (!session) {
    return (
      <div className="stack">
        <div className="alert error">{t("practice.no_run_opened")}</div>
        <Link className="btn secondary" to="/student/practice">
          ‹ {t("practice.back_to_shelf")}
        </Link>
      </div>
    );
  }

  if (run.isError) {
    return (
      <div className="stack">
        <Link className="btn secondary" to="/student/practice">
          ‹ {t("practice.back_to_shelf")}
        </Link>
        <div className="alert error">{t("common.could_not_load")} {(run.error as ApiError).message}</div>
      </div>
    );
  }

  if (!run.data) return <div className="card muted">{t("common.loading")}</div>;

  const savedSet: Record<string, boolean> = {};
  for (const row of favorites.data?.items || []) savedSet[`${row.kind}:${row.ref_id}`] = true;
  const marks: Record<string, string> = {};
  for (const row of known.data?.items || []) marks[row.ref_id] = row.state;

  const done = Boolean(summary.data?.finished);
  const answered = summary.data?.answered ?? Object.keys(results).length;
  const totalSteps = steps.filter((each) => each.kind === "question").length;

  if (done && summary.data) {
    return (
      <div className="stack">
        {message ? <div className="alert error">{message}</div> : null}
        <ResultScreen
          catalogId={run.data.catalog_id}
          catalogName={run.data.catalog_name}
          skipped={run.data.skipped_count}
          summary={summary.data}
          onRetry={() => retry.mutate()}
          retrying={retry.isPending}
        />
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="row">
        <Link className="btn secondary" to={`/student/practice?open=${run.data.catalog_id}`}>
          ‹ {run.data.catalog_name}
        </Link>
        <span className="spacer" />
        <span className="small muted">
          {t("practice.progress", { done: answered, total: totalSteps })}
        </span>
      </div>

      {run.data.skipped_count ? (
        <p className="small muted">{t("practice.skipped_note", { n: run.data.skipped_count })}</p>
      ) : null}
      {message ? <div className="alert error">{message}</div> : null}

      {steps.length === 0 ? (
        <div className="card muted">{t("practice.nothing_to_do")}</div>
      ) : (
        <>
          <div className="row small muted">
            <button type="button" className="btn ghost" disabled={index === 0} onClick={() => setIndex(index - 1)}>
              ‹ {t("practice.previous")}
            </button>
            <span className="spacer" />
            <span>
              {t("practice.step_of", { n: index + 1, total: steps.length })}
            </span>
            <span className="spacer" />
            <button type="button" className="btn ghost" disabled={index >= steps.length - 1} onClick={() => setIndex(index + 1)}>
              {t("practice.next")} ›
            </button>
          </div>

          {step ? (
            <PracticeStep
              step={step}
              sessionId={session}
              results={results}
              busyId={answer.isPending ? (answer.variables?.questionId ?? null) : null}
              onAnswer={(questionId, payload) => answer.mutate({ questionId, payload })}
              knownEnabled={run.data.known_states_enabled}
              known={marks}
              onMark={(refId, state) => mark.mutate({ refId, state })}
              favorites={savedSet}
              onFavorite={(kind, refId, wasSaved) => favorite.mutate({ kind, refId, saved: wasSaved })}
            />
          ) : null}

          <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
            <span className="small">
              {run.data.feedback_timing === "after_session"
                ? t("practice.timing_note")
                : t("practice.marks_shown_note")}
            </span>
            <span className="spacer" />
            <button
              type="button"
              className="btn"
              disabled={finish.isPending || answered === 0}
              onClick={() => finish.mutate()}
              style={{ minHeight: 44 }}
            >
              {finish.isPending ? t("common.loading") : t("practice.finish")}
            </button>
          </div>

          {questionIds.length ? (
            <div className="card row small" style={{ flexWrap: "wrap", gap: 6 }}>
              {questionIds.map((id, position) => (
                <button
                  key={id || position}
                  type="button"
                  className="btn ghost"
                  onClick={() => setIndex(steps.findIndex((each) => each.kind === "question" && String(each.view?.id) === id))}
                  style={{ fontWeight: results[id] || summary.data?.results.some((line) => line.question_id === id) ? 700 : 400 }}
                >
                  {position + 1}
                </button>
              ))}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

function ResultScreen({
  catalogId,
  catalogName,
  skipped,
  summary,
  onRetry,
  retrying,
}: {
  catalogId: string;
  catalogName: string;
  skipped: number;
  summary: RunSummary;
  onRetry: () => void;
  retrying: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="stack">
      <div className="row">
        <Link className="btn secondary" to={`/student/practice?open=${catalogId}`}>
          ‹ {catalogName}
        </Link>
      </div>
      <h1 style={{ margin: 0 }}>{t("practice.finished")}</h1>
      <div className="card stack">
        <div className="row" style={{ flexWrap: "wrap", gap: 16 }}>
          <div>
            <div className="muted small">{t("practice.your_score")}</div>
            <strong style={{ fontSize: "1.4rem" }}>{t("practice.score_of", { got: summary.score, total: summary.max_score })}</strong>
          </div>
          <div className="small">
            <div>{t("practice.answered_n", { n: summary.answered })}</div>
            <div>{t("practice.right_n", { n: summary.correct_count })}</div>
            {/* Said only when there is one, because "Partly right: 0" is noise next to a total
                that already adds up. */}
            {summary.partial_count ? (
              <div>{t("practice.partly_right_n", { n: summary.partial_count })}</div>
            ) : null}
            <div>{t("practice.wrong_n", { n: summary.incorrect_count })}</div>
            {summary.manual_count ? <div>{t("practice.not_auto_marked_n", { n: summary.manual_count })}</div> : null}
            {skipped ? <div className="muted">{t("practice.skipped_note", { n: skipped })}</div> : null}
          </div>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <button type="button" className="btn secondary" disabled={retrying} onClick={onRetry} style={{ minHeight: 44 }}>
            {t("practice.do_it_again")}
          </button>
          <Link className="btn ghost" to={`/student/practice?open=${catalogId}`}>
            {t("practice.pick_another")}
          </Link>
        </div>
      </div>

      <div className="card stack">
        <h3 style={{ margin: 0 }}>{t("practice.results_title")}</h3>
        {summary.results.length === 0 ? <div className="muted small">{t("practice.no_answers_yet")}</div> : null}
        {summary.results.map((line) => (
          <div className="stack" key={line.question_id} style={{ borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
            <div className="small">{line.prompt || t("practice.content_gone")}</div>
            <div className="row small" style={{ gap: 8 }}>
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
              ) : (
                <span className="muted">{t("practice.not_marked")}</span>
              )}
              <span className="muted">{t("practice.score_of", { got: line.score, total: line.max_score })}</span>
            </div>
            {line.explanation ? <p className="small" style={{ margin: 0 }}>{line.explanation}</p> : null}
          </div>
        ))}
      </div>
      <p className="small muted">{t("practice.history_note")}</p>
    </div>
  );
}
