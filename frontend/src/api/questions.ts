// Question bank, topics and tags: the payloads the backend actually answers with.
// Nothing here hardcodes the question type list - the editor is built from
// `/questions/types`, so a new type appears without a frontend change.

import { api } from "./client";
import type { MediaLearner } from "./media";

export interface TaxonomyRef {
  id: string;
  name: string;
}

export interface QuestionSummary {
  id: string;
  type: string;
  prompt: string | null;
  status: string;
  context_kind: string;
  level: string | null;
  difficulty: number | null;
  learning_language: string | null;
  score: number;
  current_version: number;
  topic_names: string[];
  tag_names: string[];
  has_media: boolean;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface Question extends QuestionSummary {
  partial_scoring: Record<string, unknown>;
  negative_scoring: Record<string, unknown>;
  explanation: string | null;
  teacher_notes: string | null;
  media_asset_id: string | null;
  reading_id: string | null;
  listening_id: string | null;
  source_file_id: string | null;
  source_page: number | null;
  extraction_method: string | null;
  config: Record<string, unknown>;
  topics: TaxonomyRef[];
  tags: TaxonomyRef[];
}

export interface QuestionVersion {
  id: string;
  question_id: string;
  version: number;
  change_note: string | null;
  created_at: string;
  snapshot: Record<string, unknown>;
}

export interface TypeSpec {
  type: string;
  group: string;
  answer_widget: string;
  gradable_automatically: boolean;
  supports_partial: boolean;
  config_schema: Record<string, unknown>;
}

export interface TopicNode {
  id: string;
  name: string;
  parent_id: string | null;
  level_path: string;
  language: string | null;
  question_count: number;
  children: TopicNode[];
}

export interface Tag {
  id: string;
  name: string;
  color: string | null;
  question_count: number;
  vocabulary_count: number;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface GradeOutcome {
  score: number;
  max_score: number;
  correct: boolean | null;
  requires_manual: boolean;
  detail: Record<string, unknown>;
  version?: number;
}

export type QuestionDraft = Record<string, unknown> & { type: string; config: Record<string, unknown> };

export const questionKeys = (filters: Record<string, string | number | undefined>) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

export const questionsApi = {
  types: () => api.get<{ items: TypeSpec[]; total: number }>("/questions/types"),
  list: (filters: Record<string, string | number | undefined>) =>
    api.get<Page<QuestionSummary>>(`/questions?${questionKeys(filters)}`),
  get: (id: string) => api.get<Question>(`/questions/${id}`),
  create: (body: QuestionDraft) => api.post<Question>("/questions", body),
  update: (id: string, body: Record<string, unknown>) => api.patch<Question>(`/questions/${id}`, body),
  setStatus: (id: string, status: string) => api.post<Question>(`/questions/${id}/status`, { status }),
  trash: (id: string) => api.del<{ ok: boolean; trashed: string; status: string }>(`/questions/${id}`),
  restore: (id: string) => api.post<Question>(`/questions/${id}/restore`),
  clone: (id: string) => api.post<Question>(`/questions/${id}/clone?status=draft`),
  assignTaxonomy: (id: string, body: { topic_ids?: string[]; tag_ids?: string[] }) =>
    api.post<Question>(`/questions/${id}/taxonomy`, body),
  preview: (id: string) => api.get<LearnerView>(`/questions/${id}/preview`),
  versions: (id: string) => api.get<{ items: QuestionVersion[]; total: number }>(`/questions/${id}/versions`),
  version: (id: string, version: number) =>
    api.get<QuestionVersion>(`/questions/${id}/versions/${version}`),
  grade: (id: string, response: unknown, atVersion?: number) =>
    api.post<GradeOutcome>(
      `/questions/${id}/grade${atVersion ? `?at_version=${atVersion}` : ""}`,
      { response },
    ),
  bulk: (body: {
    question_ids: string[];
    action: string;
    status?: string;
    topic_id?: string;
    tag_id?: string;
    level?: string;
    learning_language?: string;
  }) => api.post<BulkResult>("/questions/bulk", body),
};

/** Counts would hide a partial win: the backend answers per id, so the UI shows which. */
export interface BulkResult {
  action: string;
  updated: string[];
  refused: { id: string; reason: string }[];
  not_found: string[];
}

export interface LearnerView {
  id: string;
  type: string;
  prompt: string | null;
  score: number;
  answer_widget: string;
  requires_manual_grading: boolean;
  config: Record<string, unknown>;
  /** Only the single-question route sends it. A reading's own screen already showed the
   * text above the questions, so each question is served without a copy of it. */
  context?: Record<string, unknown>;
  /** Present when the question carries a file of its own; the app serves the bytes. */
  media?: MediaLearner;
  explanation_available: boolean;
}

export const topicsApi = {
  list: () => api.get<{ items: TopicNode[]; total: number }>("/topics"),
  create: (body: { name: string; parent_id?: string | null; language?: string | null }) =>
    api.post<TopicNode>("/topics", body),
  update: (id: string, body: Record<string, unknown>) => api.patch<TopicNode>(`/topics/${id}`, body),
  remove: (id: string) => api.del<{ ok: boolean; deleted: string }>(`/topics/${id}`),
};

export const tagsApi = {
  list: () => api.get<{ items: Tag[]; total: number }>("/tags"),
  create: (body: { name: string; color?: string | null }) => api.post<Tag>("/tags", body),
  update: (id: string, body: Record<string, unknown>) => api.patch<Tag>(`/tags/${id}`, body),
  remove: (id: string) => api.del<{ ok: boolean; deleted: string }>(`/tags/${id}`),
};

export const QUESTION_STATUSES = ["draft", "ready", "archived"];

export const LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"];
