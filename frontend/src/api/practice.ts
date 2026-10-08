// Practice: the learner's own list, runs, marks and saved items.
//
// A run has no table. The server issues a `session_id` when the learner opens a catalog,
// every answer is written to the activity log against that token, and the screens below
// read the run back from those rows. Two consequences shape this file: the token is the
// only thing that authorises an answer, so it travels on every call; and the verdicts a
// response carries depend on the catalog's `feedback_timing`, so `withheld` is a real
// field and not an empty payload.

import { api } from "./client";
import { catalogKeys, type Page } from "./catalogs";
import type { StudyCard } from "./vocabulary";
import type { LearnerReading } from "./reading";
import type { ListeningLearner } from "./listening";
import type { LearnerView } from "./questions";

/** One thing to do in a run. `view` is the bank's own learner projection, so the shape
 * depends on `kind` and the answer-key rules live in exactly one place: the backend. */
export interface Step {
  item_id: string;
  kind: string;
  ref_id: string;
  position: number;
  view: Record<string, any>;
}

export interface Run {
  session_id: string;
  catalog_id: string;
  catalog_name: string;
  feedback_timing: string;
  shuffle: boolean;
  known_states_enabled: boolean;
  /** References the catalog holds that could not be served right now. The learner is told
   * the number, not whose draft caused it. */
  skipped_count: number;
  steps: Step[];
  started_at: string;
}

export interface AnswerResult {
  recorded: boolean;
  session_id: string;
  question_id: string;
  withheld: boolean;
  correct: boolean | null;
  score: number | null;
  max_score: number | null;
  requires_manual: boolean;
  explanation: string | null;
}

export interface ResultLine {
  question_id: string;
  prompt: string | null;
  correct: boolean | null;
  score: number;
  max_score: number;
  requires_manual: boolean;
  explanation: string | null;
  answered_at: string | null;
}

export interface RunSummary {
  session_id: string;
  catalog_id: string;
  catalog_name: string;
  answered: number;
  correct_count: number;
  incorrect_count: number;
  /** Essays and anything a teacher marks by hand: counted, never scored as wrong. */
  manual_count: number;
  score: number;
  max_score: number;
  finished: boolean;
  started_at: string | null;
  finished_at: string | null;
  results: ResultLine[];
}

/** One catalog in a learner's practice list: no lifecycle state and no folder name, since
 * the folder structure is rendered from `parent_id` by the learner's own screen. */
export interface PracticeCatalog {
  id: string;
  name: string;
  description: string | null;
  parent_id: string | null;
  learning_language: string | null;
  level: string | null;
  item_count: number;
  counts: Record<string, number>;
  child_count: number;
  shuffle_default: boolean;
  known_states_enabled: boolean;
  feedback_timing: string;
  /** How many runs this learner has already opened here. No score on a list row. */
  runs: number;
}

export interface PracticeDetail extends Omit<PracticeCatalog, "runs" | "child_count"> {
  path: { id: string; name: string }[];
  runs: RunSummary[];
}

export interface KnownState {
  ref_id: string;
  state: string;
}

export interface KnownStates {
  enabled: boolean;
  word_count: number;
  counts: Record<string, number>;
  items: KnownState[];
}

export interface Favorite {
  id: string;
  kind: string;
  ref_id: string;
  /** Named from its own row: a review list of ids is a list the learner cannot use. */
  title: string | null;
  detail: string | null;
  /** `false` when the content has since been drafted, binned or deleted. */
  available: boolean;
  created_at: string;
}

export interface PracticeMeta {
  learning_languages: string[];
  levels: string[];
  sortable: string[];
  known_states: string[];
  favorite_kinds: string[];
  feedback_timings: string[];
}

/** The shape the grader accepts, per question type. These are the engine's own keys; a
 * widget that invented a different one would be graded as an empty answer. */
export type AnswerPayload =
  | { option_index: number }
  | { option_indexes: number[] }
  | { value: boolean }
  | { text: string }
  | { blanks: string[] }
  | { pairs: { left_ref: string; right_ref: string }[] }
  | { order: string[] };

export type StepView = LearnerView | StudyCard | LearnerReading | ListeningLearner;

export const practiceApi = {
  meta: () => api.get<PracticeMeta>("/student/practice/meta"),
  list: (filters: Record<string, string | number | undefined>) =>
    api.get<Page<PracticeCatalog>>(`/student/practice/catalogs?${catalogKeys(filters)}`),
  catalog: (id: string) => api.get<PracticeDetail>(`/student/practice/catalogs/${id}`),
  /** `shuffle` overrides the catalog's default for this run only, and the order is decided
   * server-side because the log records answers against the steps that were served. */
  start: (id: string, options?: { shuffle?: boolean | null; language?: string }) =>
    api.post<Run>(
      `/student/practice/catalogs/${id}/run${options?.language ? `?language=${encodeURIComponent(options.language)}` : ""}`,
      { shuffle: options?.shuffle ?? null },
    ),
  known: (id: string) => api.get<KnownStates>(`/student/practice/catalogs/${id}/known`),
  mark: (id: string, refId: string, state: string) =>
    api.post<{ ref_id: string; state: string }>(`/student/practice/catalogs/${id}/known`, {
      ref_id: refId,
      state,
    }),
  answer: (body: {
    session_id: string;
    question_id: string;
    response: AnswerPayload | unknown;
    time_spent_seconds?: number | null;
  }) => api.post<AnswerResult>("/student/practice/answer", body),
  run: (sessionId: string) => api.get<RunSummary>(`/student/practice/runs/${sessionId}`),
  /** The steps a run was opened with, in the order the stored shuffle seed gives back.
   * Reading it does not open a second run. */
  steps: (sessionId: string, language?: string) =>
    api.get<Run>(
      `/student/practice/runs/${sessionId}/steps${language ? `?language=${encodeURIComponent(language)}` : ""}`,
    ),
  finish: (sessionId: string) => api.post<RunSummary>(`/student/practice/runs/${sessionId}/finish`),
};

export const favoritesApi = {
  list: () => api.get<{ items: Favorite[] }>("/student/favorites"),
  /** Saving the same thing twice is not an error: `added` says whether the list grew. */
  add: (kind: string, refId: string) =>
    api.post<{ ok: boolean; added: boolean; id?: string; kind: string; ref_id: string }>(
      "/student/favorites",
      { kind, ref_id: refId },
    ),
  remove: (kind: string, refId: string) =>
    api.post<{ ok: boolean; removed: boolean; kind: string; ref_id: string }>(
      "/student/favorites/remove",
      { kind, ref_id: refId },
    ),
};
