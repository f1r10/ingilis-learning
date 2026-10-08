// Exams: the teacher's assessed papers, and the sittings learners take of them.
//
// A catalog holds live references to the central banks; an exam pins a `QuestionVersion` and
// never follows the bank again. That single difference is why every route here is its own and
// why the shapes below carry marks the catalog screens have no use for: `version` and
// `current_version` on a line tell the teacher the paper is asking version 3 while the bank is
// on version 7, which is a fact about a result already given, not a suggestion to edit.
//
// Two rules out of the backend shape this whole file:
//
// * **The server owns the clock.** Nothing here computes a deadline. Every read returns
//   `remaining_seconds` produced from the stored `expires_at` and the server's own time, so a
//   learner who changes device or moves their clock forward is still measured by the same
//   running-out. The runner counts down from that number and re-reads it; it never decides.
// * **The token is the whole relationship.** A learner never names an attempt id: `/start`
//   hands back a token, and every later call presents it. So the learner's half of this file
//   takes tokens, and a token that is not one is answered the same way as a token belonging to
//   somebody else.
//
// The status codes are not uniform and are not this file's choice: creating a paper, a section,
// a set of items, an assignment, a clone, a sitting and a piece of feedback answers 201, while
// a status change, an edit, a hand-in, a mark and every read answer 200. `api` does not look at
// the code, so a wrong expectation here would fail silently - the tests that assert them live
// on the backend, and the screens simply read the body.
//
// Words the screens branch on (`availability_reasons`, `result_states`, `close_reasons`,
// `notices`, the two status sets) come from `/exams/meta` and `/grading/meta` rather than being
// typed out here, because the code that emits them is the only thing that knows the list.

import { api } from "./client";
import type { LearnerView } from "./questions";

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** The list's filters and the preview's options, in one place so an empty box and an absent
 * parameter mean the same thing to the endpoint. */
export const examKeys = (filters: Record<string, string | number | boolean | undefined | null>) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

/** Bulk answers name each id: a partially applied selection is shown, not hidden behind a
 * single "done". */
export interface BulkResult {
  action: string;
  updated: string[];
  refused: { id: string; reason: string }[];
  not_found: string[];
}

/** One line of a paper, resolved against the version it is pinned to.
 * `state` is `ready` or a refusal word; `serveable` is the shorter answer the publish check uses. */
export interface ExamItem {
  id: string;
  kind: string;
  ref_id: string;
  position: number;
  points: number | null;
  question_score: number | null;
  effective_points: number;
  version: number | null;
  current_version: number | null;
  section_id: string | null;
  title: string | null;
  detail: string | null;
  /** The question's own relationship to a text or recording, not a second line on the paper. */
  context_kind: string | null;
  context_id: string | null;
  context_title: string | null;
  state: string;
  available_to_learner?: boolean;
  serveable: boolean;
}

export interface ExamSection {
  id: string;
  title: string | null;
  position: number;
  shuffle_items: boolean;
  instructions: string | null;
  item_count: number;
  points: number;
}

/** Who the paper was handed to. A group is read live, so a learner who joins the class tomorrow
 * is sitting with the rest of the group without the teacher assigning twice. */
export interface Assignment {
  id: string;
  exam_id: string;
  student_id: string | null;
  group_id: string | null;
  name: string | null;
  kind: string;
  /** `false` when the student or group has since been binned: the row is shown as unreachable. */
  reachable: boolean;
  /** The learners this row reaches: one for a named student, the whole class for a named group. */
  member_ids: string[];
  attempts: number;
  submitted: number;
  created_at: string;
}

/** The rules a paper runs on. `ExamDraft` is the same object with every field optional, because
 * a PATCH applies only the fields the teacher actually sent. */
export interface ExamRules {
  title?: string | null;
  description?: string | null;
  learning_language?: string | null;
  level?: string | null;
  available_from?: string | null;
  available_to?: string | null;
  duration_minutes?: number | null;
  must_finish_before_close?: boolean;
  max_attempts?: number | null;
  passing_score?: number | null;
  shuffle_questions?: boolean;
  shuffle_options?: boolean;
  resume_after_disconnect?: boolean;
  restrict_copy_paste?: boolean;
  monitor_tab_switch?: boolean;
  tab_switch_limit?: number | null;
  tab_switch_action?: string | null;
  allow_previous?: boolean;
  feedback_timing?: string;
  show_correct_answers?: boolean;
  show_explanations?: boolean;
  result_visibility?: string;
  partial_scoring_enabled?: boolean;
  negative_marking_enabled?: boolean;
  grading_mode?: string;
  auto_submit_on_expiry?: boolean;
}

export interface Exam extends ExamRules {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  item_count: number;
  counts: Record<string, number>;
  points: number;
  sections: ExamSection[];
  items: ExamItem[];
  assignments: Assignment[];
  attempt_count: number;
  /** Set once somebody has sat the paper: the composition becomes history, the rules do not. */
  composition_locked: boolean;
  /** Why the paper cannot be handed out yet. The server names the rule; the screen says it. */
  publish_blockers: { code: string; params?: Record<string, unknown> }[];
}

export interface ExamRow {
  id: string;
  title: string;
  status: string;
  learning_language: string | null;
  level: string | null;
  available_from: string | null;
  available_to: string | null;
  duration_minutes: number | null;
  item_count: number;
  points: number;
  assigned_count: number;
  attempt_count: number;
  submitted_count: number;
  /** Answers waiting for a teacher - the number that makes the queue a queue. */
  review_count: number;
  created_at: string;
  deleted_at: string | null;
}

export interface ExamMeta {
  statuses: string[];
  /** Codes, not sentences: the editor names them in the teacher's own interface language. */
  kinds: { kind: string }[];
  feedback_timings: string[];
  result_visibilities: string[];
  /** `available: false` is a real option the teacher sees and cannot pick: `ai_assisted` needs
   * the writing assistant that belongs to a later phase. */
  grading_modes: { mode: string; available?: boolean }[];
  tab_switch_actions: string[];
  levels: string[];
  languages: string[];
  limits: Record<string, number>;
  views: string[];
  bulk_actions: string[];
}

export interface PreviewItem {
  position: number;
  kind: string;
  ref_id: string;
  exam_item_id: string;
  title: string | null;
  version: number | null;
  points: number;
  section_title: string | null;
  context_id: string | null;
  context_title: string | null;
  /** The authored option indexes in the order this sitting would show them. Numbers, because
   * the teacher's screen prints the words; what matters is that the server decided the order. */
  shuffled_options: number[] | null;
}

export interface ExamPreview {
  exam_id: string;
  title: string;
  seed: string;
  item_count: number;
  points: number;
  duration_minutes: number | null;
  opens_at: string | null;
  closes_at: string | null;
  items: PreviewItem[];
  /** Lines the preview had to leave out, with the reason. A preview that dropped them quietly
   * would be a paper that looks fine and then serves less. `reason` is the same state word the
   * item rows use, so both screens translate one vocabulary rather than two. */
  skipped: { exam_item_id: string; title: string | null; reason: string }[];
}

/** One sitting as a row on the teacher's list. `token` is present so a paper still running can
 * be opened; a learner's own screen always has it. */
export interface AttemptRow {
  id: string;
  token: string | null;
  exam_id: string;
  student_id: string;
  student_name: string | null;
  status: string;
  attempt_number: number;
  started_at: string;
  expires_at: string | null;
  submitted_at: string | null;
  remaining_seconds: number | null;
  server_seconds_used: number | null;
  answered_items: number;
  total_items: number;
  score: number | null;
  max_score: number | null;
  passed: boolean | null;
  tab_switches: number;
  needs_review: number;
}

export interface AttemptAnswerLine {
  exam_item_id: string;
  position: number;
  kind: string;
  prompt: string | null;
  question_type: string | null;
  version: number | null;
  response: unknown;
  answered: boolean;
  correct: boolean | null;
  score: number | null;
  max_score: number;
  graded: boolean;
  needs_manual_review: boolean;
  flagged: boolean;
  time_spent_seconds: number | null;
  changed_count: number;
}

export interface GradingFeedback {
  id: string;
  attempt_id: string | null;
  answer_id: string | null;
  student_id: string;
  body: string;
  created_at: string;
}

/** Everything about one sitting, including the lines the learner may not see yet.
 * The tab-switch rule arrives as data - whether this paper watched the tab, what its rule was,
 * how many times the browser reported the page became invisible - and this screen says it.
 * Nothing here calls a count of tabs evidence about what the learner opened. */
export interface AttemptDetail {
  id: string;
  token: string | null;
  exam_id: string;
  exam_title: string;
  student_id: string;
  student_name: string | null;
  status: string;
  attempt_number: number;
  started_at: string;
  expires_at: string | null;
  submitted_at: string | null;
  server_seconds_used: number | null;
  score: number | null;
  max_score: number | null;
  passed: boolean | null;
  passing_score: number | null;
  tab_switches: number;
  monitor_tab_switch: boolean;
  tab_switch_action: string | null;
  tab_switch_limit: number | null;
  answers: AttemptAnswerLine[];
  feedback: GradingFeedback[];
}

/** One answer waiting for a teacher, with enough on the row to mark it without opening the
 * whole sitting. */
export interface ReviewRow {
  id: string;
  attempt_id: string;
  answer_id: string;
  exam_id: string;
  exam_title: string;
  student_id: string;
  student_name: string | null;
  question_id: string | null;
  question_type: string | null;
  prompt: string | null;
  response: unknown;
  max_score: number;
  /** The engine's provisional mark, and only while the answer is still waiting. */
  auto_score: number | null;
  reviewed: boolean;
  final_score: number | null;
  reviewer_note: string | null;
  submitted_at: string | null;
  /** Only ever a suggestion some assistant actually produced; the queue never invents a mark. */
  ai_suggestion: Record<string, unknown> | null;
}

export interface GradingSummary {
  pending: number;
  reviewed_today: number;
  exams: { exam_id: string; title: string | null; pending: number }[];
  students: { student_id: string; name: string | null; pending: number }[];
}

/** The grading vocabulary, from the code that produces it. `statuses` are sitting states
 * (`in_progress`, `submitted`, …), separate from the paper's lifecycle states. */
export interface GradingMeta {
  statuses: string[];
  exam_statuses: string[];
  availability_reasons: string[];
  result_states: string[];
  close_reasons: string[];
  notices: string[];
}

export interface ExamDraft {
  name: string;
  member_count: number;
  id: string;
}

// --------------------------------------------------------------------------- //
// The learner's half
// --------------------------------------------------------------------------- //

/** What the runner has to obey, stated once by the server rather than decided by the screen. */
export interface RunnerRules {
  allow_previous: boolean;
  restrict_copy_paste: boolean;
  monitor_tab_switch: boolean;
  tab_switch_limit: number | null;
  tab_switch_action: string | null;
  resume_after_disconnect: boolean;
  feedback_timing: string;
  auto_submit_on_expiry: boolean;
}

/** One thing to answer, in the order this sitting was frozen in.
 * `view` is the bank's own learner projection of the *pinned version* with the answer key
 * removed, so a resumed paper shows exactly what the first one did. `saved` is the learner's own
 * last answer coming back - never a verdict. */
export interface AttemptStep {
  exam_item_id: string;
  kind: string;
  ref_id: string;
  position: number;
  section_id: string | null;
  section_title: string | null;
  points: number;
  /** The text or recording this question travels with. The runner prints it once per consecutive
   * run that shares it, which is why the key is handed over instead of dug out of the view. */
  context_id: string | null;
  /** The bank's own learner projection of the pinned version, answer key removed - the same shape
   * `/questions/{id}/learner` serves for practice, which is why one widget renders both. */
  view: LearnerView;
  saved: unknown;
  answered: boolean;
}

export interface AttemptState {
  token: string;
  attempt_id: string;
  exam_id: string;
  title: string;
  status: string;
  attempt_number: number;
  /** The allowance the sitting was dealt under. A paper whose rule is empty reads as `1` here,
   * not as `null`, because `null` would promise unlimited attempts to a second start that refuses. */
  max_attempts: number | null;
  started_at: string;
  expires_at: string | null;
  /** `null` when the paper has no time limit; `0` when it is over. */
  remaining_seconds: number | null;
  server_seconds_used: number | null;
  duration_minutes: number | null;
  total_items: number;
  answered_items: number;
  points: number;
  tab_switches: number;
  rules: RunnerRules;
  /** A word for what just happened, so a screen that resumed onto an expired sitting can say so
   * instead of showing a form that will not accept answers. */
  notice: string | null;
}

export interface AttemptSteps {
  token: string;
  status: string;
  remaining_seconds: number | null;
  steps: AttemptStep[];
}

/** Opening a sitting, or being handed back the one already open.
 * Deliberately not `AttemptState` plus a flag: the start read answers "what did the paper just
 * deal me", so it carries the frozen `total_items`, `points` and `rules` and none of the running
 * facts a state read adds later (`answered_items`, `server_seconds_used`, `notice`). The runner
 * takes the clock from a state read and the composition from `steps`. */
export interface StartRead {
  token: string;
  attempt_id: string;
  exam_id: string;
  title: string;
  attempt_number: number;
  max_attempts: number | null;
  started_at: string;
  expires_at: string | null;
  remaining_seconds: number | null;
  total_items: number;
  points: number;
  rules: RunnerRules;
  /** `false` when this opened a new sitting, `true` when an existing one was handed back - which
   * is what a second tab, a reconnect or a device switch looks like. */
  resumed: boolean;
  steps: AttemptStep[];
}

/** What saving one answer returns. The verdict is only present when the paper's own feedback
 * timing allows it, and `withheld` says so out loud rather than leaving a blank. */
export interface AnswerReceipt {
  recorded: boolean;
  token: string;
  exam_item_id: string;
  withheld: boolean;
  correct: boolean | null;
  score: number | null;
  max_score: number | null;
  requires_manual: boolean;
  remaining_seconds: number | null;
  /** A switch limit reached, or the deadline passing while the learner was typing. */
  notice: string | null;
}

export interface TabSwitchReceipt {
  token: string;
  tab_switches: number;
  tab_switch_limit: number | null;
  tab_switch_action: string | null;
  status: string;
  notice: string;
  /** `true` only when this report is what closed the paper. */
  auto_submitted: boolean;
  remaining_seconds: number | null;
}

export interface ResultLine {
  exam_item_id: string;
  prompt: string | null;
  answered: boolean;
  correct: boolean | null;
  score: number;
  max_score: number;
  requires_manual: boolean;
  /** Only ever the question's own explanation, and only when the paper allows explanations. */
  explanation: string | null;
  /** What the teacher wrote when they marked this line by hand. */
  reviewer_note: string | null;
}

/** The learner's result screen, as far as the paper's visibility rules reach.
 * `visible` is the whole answer to "can they see this yet"; when it is false the totals are
 * absent and `state` names the rule holding them. */
export interface AttemptResult {
  token: string;
  exam_id: string;
  title: string;
  status: string;
  submitted_at: string | null;
  server_seconds_used: number | null;
  result_visibility: string;
  visible: boolean;
  /** `closed` | `awaiting_teacher` | `hidden` | `shown`. */
  state: string;
  /** `closed`, or `already_submitted` for a second press that must not look like a new result. */
  notice: string | null;
  score: number | null;
  max_score: number | null;
  passed: boolean | null;
  passing_score: number | null;
  answered_items: number;
  total_items: number;
  correct_count: number;
  /** Lines that took part of their mark, counted apart from right and wrong. */
  partial_count: number;
  incorrect_count: number;
  manual_count: number;
  show_correct_answers: boolean;
  lines: ResultLine[];
  feedback: GradingFeedback[];
}

/** One assigned paper on the learner's list, with whether it can be opened and what it cost. */
export interface LearnerExam {
  id: string;
  title: string;
  description: string | null;
  level: string | null;
  learning_language: string | null;
  status: string;
  available: boolean;
  /** `open` | `not_yet` | `closed` | `no_attempts_left` | `already_submitted`. */
  availability_reason: string;
  opens_at: string | null;
  closes_at: string | null;
  duration_minutes: number | null;
  item_count: number;
  points: number;
  max_attempts: number | null;
  attempts_used: number;
  attempts_left: number | null;
  best_score: number | null;
  best_max_score: number | null;
  latest_status: string | null;
  /** A sitting is still open on this paper, so the list offers "continue" and never asks the
   * learner to begin a second one. */
  resume_token: string | null;
  result_visible: boolean;
  feedback_waiting: boolean;
}

/** The page before the paper: the rules stated plainly, then the start button. No question, no
 * option and no answer key is here. */
export interface LearnerExamBrief {
  id: string;
  title: string;
  description: string | null;
  level: string | null;
  item_count: number;
  counts: Record<string, number>;
  points: number;
  duration_minutes: number | null;
  opens_at: string | null;
  closes_at: string | null;
  must_finish_before_close: boolean;
  max_attempts: number | null;
  attempts_used: number;
  attempts_left: number | null;
  passing_score: number | null;
  rules: RunnerRules;
  available: boolean;
  availability_reason: string;
  resume_token: string | null;
}

export interface LearnerAttempts {
  exam_id: string;
  title: string;
  attempts: AttemptRow[];
}

export const examsApi = {
  meta: () => api.get<ExamMeta>("/exams/meta"),
  list: (filters: Record<string, string | number | boolean | undefined | null> = {}) =>
    api.get<Page<ExamRow>>(`/exams?${examKeys(filters)}`),
  get: (id: string) => api.get<Exam>(`/exams/${id}`),
  create: (body: ExamRules & { title: string; status?: string }) => api.post<Exam>("/exams", body),
  update: (id: string, body: ExamRules) => api.patch<Exam>(`/exams/${id}`, body),
  setStatus: (id: string, status: string) => api.post<Exam>(`/exams/${id}/status`, { status }),
  clone: (id: string) => api.post<Exam>(`/exams/${id}/clone`),
  trash: (id: string) => api.del<{ id: string; deleted_at: string | null }>(`/exams/${id}`),
  restore: (id: string) => api.post<{ id: string; restored: boolean }>(`/exams/${id}/restore`),
  /** Nothing is written: no attempt, no event, no timer. Pass a `seed` back to see the order a
   * sitting already got. */
  preview: (id: string, seed?: string) =>
    api.get<ExamPreview>(`/exams/${id}/preview${seed ? `?seed=${encodeURIComponent(seed)}` : ""}`),
  items: (id: string) => api.get<{ items: ExamItem[]; count: number }>(`/exams/${id}/items`),

  createSection: (id: string, body: { title?: string | null; instructions?: string | null; position?: number | null; shuffle_items?: boolean }) =>
    api.post<ExamSection>(`/exams/${id}/sections`, body),
  updateSection: (sectionId: string, body: { title?: string | null; instructions?: string | null; shuffle_items?: boolean }) =>
    api.patch<ExamSection>(`/exams/sections/${sectionId}`, body),
  /** The part's items go back to the paper's unsectioned run rather than being deleted. */
  removeSection: (sectionId: string) => api.del<Exam>(`/exams/sections/${sectionId}`),
  /** Must name every section of this paper, in the order wanted. */
  reorderSections: (id: string, sectionIds: string[]) =>
    api.post<Exam>(`/exams/${id}/sections/reorder`, { section_ids: sectionIds }),

  /** `reading` and `listening` are authoring shortcuts: they add the questions filed under that
   * text or recording, each pinned to its own version, so `version` and `points` do not apply. */
  addItems: (
    id: string,
    items: { kind: string; ref_id: string; section_id?: string | null; version?: number | null; set_id?: string | null; points?: number | null }[],
  ) =>
    api.post<{ exam_id: string; added: number; item_ids: string[]; skipped: { ref_id: string; reason: string }[]; exam: Exam }>(
      `/exams/${id}/items`,
      { items },
    ),
  /** A mark, or which part the line sits in. Never the question or the version behind it. */
  updateItem: (itemId: string, body: { points?: number | null; section_id?: string | null }) =>
    api.patch<ExamItem>(`/exams/items/${itemId}`, body),
  removeItem: (itemId: string) => api.del<Exam>(`/exams/items/${itemId}`),
  reorderItems: (id: string, itemIds: string[], sectionId?: string | null) =>
    api.post<Exam>(`/exams/${id}/items/reorder`, { item_ids: itemIds, section_id: sectionId ?? null }),

  assignments: (id: string) => api.get<{ exam_id: string; items: Assignment[] }>(`/exams/${id}/assignments`),
  assign: (id: string, body: { student_ids: string[]; group_ids: string[] }) =>
    api.post<{ exam_id: string; created: number; already_assigned: number; assignments: Assignment[] }>(
      `/exams/${id}/assignments`,
      body,
    ),
  /** Sittings already started are untouched: this says nobody else should start, not that what
   * happened is undone. */
  unassign: (assignmentId: string) =>
    api.del<{ removed: boolean; exam_id: string }>(`/exams/assignments/${assignmentId}`),

  attempts: (id: string, filters: Record<string, string | number | undefined | null> = {}) =>
    api.get<Page<AttemptRow>>(`/exams/${id}/attempts?${examKeys(filters)}`),
  attempt: (attemptId: string) => api.get<AttemptDetail>(`/exams/attempts/${attemptId}`),
  bulk: (body: { exam_ids: string[]; action: string; status?: string }) =>
    api.post<BulkResult>("/exams/bulk", body),

  groups: () => api.get<{ items: ExamDraft[] }>("/groups"),
  students: (q = "") =>
    api.get<{ items: { id: string; name: string; surname: string; username: string; status: string }[] }>(
      `/students?${q ? `q=${encodeURIComponent(q)}` : ""}`,
    ),
};

export const gradingApi = {
  meta: () => api.get<GradingMeta>("/grading/meta"),
  summary: () => api.get<GradingSummary>("/grading/summary"),
  queue: (filters: Record<string, string | number | boolean | undefined | null> = {}) =>
    api.get<Page<ReviewRow>>(`/grading/queue?${examKeys(filters)}`),
  review: (reviewId: string) => api.get<ReviewRow>(`/grading/answers/${reviewId}`),
  /** Marks one waiting answer and lets the paper's total follow it. Re-marking is allowed.
   * Both halves come back because the mark moves the sitting's total: the row is decided and the
   * paper underneath it is re-added, so the screen updates the queue line and the header from the
   * same response. */
  grade: (reviewId: string, body: { score?: number | null; correct?: boolean | null; note?: string | null }) =>
    api.post<{ review: ReviewRow; attempt: AttemptDetail }>(`/grading/answers/${reviewId}/grade`, body),
  feedbackFor: (studentId: string) =>
    api.get<{ items: GradingFeedback[] }>(`/grading/students/${studentId}/feedback`),
  addFeedback: (attemptId: string, body: { body: string; answer_id?: string | null }) =>
    api.post<GradingFeedback>(`/grading/attempts/${attemptId}/feedback`, body),
  removeFeedback: (feedbackId: string) =>
    api.del<{ removed: boolean; feedback_id: string }>(`/grading/feedback/${feedbackId}`),
};

export const studentExamsApi = {
  meta: () => api.get<GradingMeta>("/student/exams/meta"),
  list: () => api.get<{ items: LearnerExam[]; total: number }>("/student/exams"),
  brief: (examId: string) => api.get<LearnerExamBrief>(`/student/exams/${examId}`),
  history: (examId: string) => api.get<LearnerAttempts>(`/student/exams/${examId}/attempts`),
  /** Open the paper, or be handed back the sitting already open on it. */
  start: (examId: string) => api.post<StartRead>(`/student/exams/${examId}/start`),
  state: (token: string) => api.get<AttemptState>(`/student/attempts/${token}`),
  /** The whole paper in the order this sitting was dealt, with the learner's own answers. A
   * resume reads from here rather than from anything the browser kept. */
  steps: (token: string) => api.get<AttemptSteps>(`/student/attempts/${token}/steps`),
  answer: (body: {
    token: string;
    exam_item_id: string;
    response: unknown;
    time_spent_seconds?: number | null;
    client_updated_at?: string | null;
  }) => api.post<AnswerReceipt>("/student/attempts/answer", body),
  /** `answers` carries what an offline tab buffered; the paper is then graded once. */
  submit: (token: string, answers?: { exam_item_id: string; response: unknown; time_spent_seconds?: number | null }[]) =>
    api.post<AttemptResult>("/student/attempts/submit", { token, answers: answers ?? null }),
  /** The browser saying the page went invisible, once. Counted only while the paper's rule watches. */
  tabSwitch: (token: string) => api.post<TabSwitchReceipt>("/student/attempts/tab-switch", { token }),
  result: (token: string) => api.get<AttemptResult>(`/student/attempts/${token}/result`),
};
