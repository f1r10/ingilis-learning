// Reading texts: the passage, the blocks filed under it, and the learner's view of both.
//
// Both screens are built from `/reading/meta`, so the languages, CEFR bands, layouts and
// sort names are the ones the endpoints enforce. `word_count` is never sent by the
// editor: the server counts the text it was handed, and a number typed by a browser
// would disagree with the text after the next edit.

import { api } from "./client";
import type { LearnerView } from "./questions";

/** A question as a passage serves it: the learner payload, without the copy of the text
 * the passage has already put on the screen above it. */
export type LearnerQuestion = Omit<LearnerView, "context">;

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** The same bulk answer shape the other banks use: per id, never a bare count. */
export interface BulkResult {
  action: string;
  updated: string[];
  refused: { id: string; reason: string }[];
  not_found: string[];
}

/** Enough of a question to recognise it in a block list - never the answer key. */
export interface QuestionStub {
  id: string;
  type: string;
  prompt: string | null;
  status: string;
  score: number;
}

export interface FiledQuestion extends QuestionStub {
  position: number;
}

export interface QuestionSet {
  id: string;
  title: string | null;
  instructions: string | null;
  position: number;
  config: Record<string, unknown>;
  question_count: number;
  questions: FiledQuestion[];
}

export interface ReadingSummary {
  id: string;
  title: string;
  excerpt: string;
  language: string | null;
  level: string | null;
  layout: string;
  status: string;
  word_count: number | null;
  set_count: number;
  question_count: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface Reading extends ReadingSummary {
  body: string;
  source_file_id: string | null;
  sets: QuestionSet[];
  unfiled: QuestionStub[];
}

/** A block as a learner sees it: where it sits and what it asks, without the teacher's
 * own authoring `config`. */
export interface LearnerSet {
  id: string;
  title: string | null;
  instructions: string | null;
  position: number;
  question_count: number;
  questions: LearnerQuestion[];
}

export interface LearnerReadingSummary {
  id: string;
  title: string;
  excerpt: string;
  language: string | null;
  level: string | null;
  layout: string;
  word_count: number | null;
  question_count: number;
}

export interface LearnerReading {
  id: string;
  title: string;
  body: string;
  language: string | null;
  level: string | null;
  layout: string;
  sets: LearnerSet[];
  unfiled: LearnerQuestion[];
}

export interface ReadingMeta {
  learning_languages: string[];
  levels: string[];
  layouts: string[];
  statuses: string[];
  views: string[];
  sortable: string[];
  max_body_characters: number;
  max_sets: number;
  excerpt_characters: number;
}

export interface LearnerMeta {
  learning_languages: string[];
  levels: string[];
  sortable: string[];
}

export type ReadingDraft = {
  title: string;
  body: string;
  language: string | null;
  level: string | null;
  layout: string | null;
};

export type SetDraft = {
  title: string;
  instructions: string | null;
  config: Record<string, unknown>;
};

/** What the assignment answered, beyond the block itself: what left and what moved in. */
export interface AssignmentResult extends QuestionSet {
  unfiled: string[];
  moved: { question_id: string; from_title: string | null }[];
}

export const readingKeys = (filters: Record<string, string | number | undefined>) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

export const readingApi = {
  meta: () => api.get<ReadingMeta>("/reading/meta"),
  list: (filters: Record<string, string | number | undefined>) =>
    api.get<Page<ReadingSummary>>(`/reading?${readingKeys(filters)}`),
  get: (id: string) => api.get<Reading>(`/reading/${id}`),
  create: (body: ReadingDraft & { status: string }) => api.post<Reading>("/reading", body),
  update: (id: string, body: Partial<ReadingDraft>) => api.patch<Reading>(`/reading/${id}`, body),
  setStatus: (id: string, status: string) => api.post<Reading>(`/reading/${id}/status`, { status }),
  trash: (id: string) => api.del<{ ok: boolean; trashed: string; status: string }>(`/reading/${id}`),
  restore: (id: string) => api.post<Reading>(`/reading/${id}/restore`),
  sets: (id: string) => api.get<{ items: QuestionSet[] }>(`/reading/${id}/sets`),
  createSet: (id: string, body: SetDraft) => api.post<QuestionSet>(`/reading/${id}/sets`, body),
  updateSet: (setId: string, body: Partial<SetDraft>) =>
    api.patch<QuestionSet>(`/reading/sets/${setId}`, body),
  deleteSet: (setId: string) =>
    api.del<{ ok: boolean; deleted: string; returned_to_pool: number }>(`/reading/sets/${setId}`),
  /** The block holds exactly these questions in this order; anything omitted is unfiled. */
  assign: (setId: string, question_ids: string[]) =>
    api.post<AssignmentResult>(`/reading/sets/${setId}/questions`, { question_ids }),
  reorder: (id: string, set_ids: string[]) =>
    api.post<{ items: QuestionSet[] }>(`/reading/${id}/sets/reorder`, { set_ids }),
  preview: (id: string) => api.get<LearnerReading>(`/reading/${id}/preview`),
  bulk: (body: {
    passage_ids: string[];
    action: string;
    status?: string;
    level?: string;
  }) => api.post<BulkResult>("/reading/bulk", body),
};

export const learnerReadingApi = {
  meta: () => api.get<LearnerMeta>("/student/reading/meta"),
  list: (filters: Record<string, string | number | undefined>) =>
    api.get<Page<LearnerReadingSummary>>(`/student/reading?${readingKeys(filters)}`),
  get: (id: string) => api.get<LearnerReading>(`/student/reading/${id}`),
};
