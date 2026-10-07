// Listening items: a recording from the library, its transcript, and the blocks under it.
//
// A listening never holds a copy of the file and never holds a URL: it names a media
// asset id, and the server answers with a `content_url` that is served by the
// application. That is what lets the library still answer "is anything using this file?"
// and what keeps an object-store address out of the browser.
//
// `transcript_source` is the server's word for where the text came from, so this editor
// does not send it: typed here, read in by the importer, dictated by the speech adapter,
// or absent.

import { api } from "./client";
import type { MediaLearner } from "./media";
import type {
  BulkResult,
  FiledQuestion,
  LearnerMeta,
  LearnerQuestion,
  Page,
  QuestionSet,
  QuestionStub,
} from "./reading";

export type { BulkResult, FiledQuestion, LearnerMeta, Page, QuestionSet, QuestionStub };

/** The block's own playback window into the recording; the file is never cut. */
export interface ListeningSet extends QuestionSet {
  start_seconds: number | null;
  end_seconds: number | null;
}

export interface LearnerListeningSet {
  id: string;
  title: string | null;
  instructions: string | null;
  position: number;
  question_count: number;
  questions: LearnerQuestion[];
  start_seconds: number | null;
  end_seconds: number | null;
}

/** A transcript cue: `{start, end?, text}`. Extra keys survive, so an adapter that adds
 * a speaker or a confidence is not erased by the editor. */
export type Cue = { start?: number; end?: number; text?: string } & Record<string, unknown>;

export interface Audio {
  id: string;
  kind: string;
  mime_type: string | null;
  label: string | null;
  duration_seconds: number | null;
  state: string;
  content_url: string;
}

export interface ListeningSummary {
  id: string;
  title: string;
  language: string | null;
  level: string | null;
  status: string;
  has_audio: boolean;
  audio_state: string | null;
  duration_seconds: number | null;
  has_transcript: boolean;
  transcript_source: string;
  show_transcript: boolean;
  set_count: number;
  question_count: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface Listening {
  id: string;
  title: string;
  language: string | null;
  level: string | null;
  status: string;
  audio: Audio | null;
  transcript: string | null;
  transcript_source: string;
  transcript_timestamps: Cue[];
  replay_limit: number | null;
  allow_pause: boolean;
  allow_seek: boolean;
  show_transcript: boolean;
  source_file_id: string | null;
  set_count: number;
  question_count: number;
  sets: ListeningSet[];
  unfiled: QuestionStub[];
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface ListeningLearnerSummary {
  id: string;
  title: string;
  language: string | null;
  level: string | null;
  duration_seconds: number | null;
  has_audio: boolean;
  replay_limit: number | null;
  show_transcript: boolean;
  question_count: number;
}

export interface ListeningLearner {
  id: string;
  title: string;
  language: string | null;
  level: string | null;
  audio: MediaLearner | null;
  duration_seconds: number | null;
  replay_limit: number | null;
  allow_pause: boolean;
  allow_seek: boolean;
  show_transcript: boolean;
  transcript: string | null;
  transcript_timestamps: Cue[];
  sets: LearnerListeningSet[];
  unfiled: LearnerQuestion[];
}

export interface ListeningMeta {
  learning_languages: string[];
  levels: string[];
  statuses: string[];
  views: string[];
  sortable: string[];
  transcript_sources: string[];
  max_replay_limit: number;
  max_body_characters: number;
  max_sets: number;
  max_questions_per_set: number;
}

export type ListeningDraft = {
  title: string;
  media_asset_id: string | null;
  language: string | null;
  level: string | null;
  transcript: string | null;
  transcript_timestamps: Cue[];
  replay_limit: number | null;
  allow_pause: boolean;
  allow_seek: boolean;
  show_transcript: boolean;
};

export type ListeningSetDraft = {
  title: string;
  instructions: string | null;
  config: Record<string, unknown>;
  start_seconds: number | null;
  end_seconds: number | null;
};

export const listeningKeys = (filters: Record<string, string | number | boolean | undefined>) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== "" && value !== null)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

export const listeningApi = {
  meta: () => api.get<ListeningMeta>("/listening/meta"),
  list: (filters: Record<string, string | number | boolean | undefined>) =>
    api.get<Page<ListeningSummary>>(`/listening?${listeningKeys(filters)}`),
  get: (id: string) => api.get<Listening>(`/listening/${id}`),
  create: (body: Partial<ListeningDraft> & { title: string; status?: string }) =>
    api.post<Listening>("/listening", body),
  update: (id: string, body: Partial<ListeningDraft>) =>
    api.patch<Listening>(`/listening/${id}`, body),
  setStatus: (id: string, status: string) => api.post<Listening>(`/listening/${id}/status`, { status }),
  trash: (id: string) => api.del<{ ok: boolean; trashed: string; status: string }>(`/listening/${id}`),
  restore: (id: string) => api.post<Listening>(`/listening/${id}/restore`),
  sets: (id: string) => api.get<{ items: ListeningSet[] }>(`/listening/${id}/sets`),
  createSet: (id: string, body: Partial<ListeningSetDraft> & { title: string }) =>
    api.post<ListeningSet>(`/listening/${id}/sets`, body),
  updateSet: (setId: string, body: Partial<ListeningSetDraft>) =>
    api.patch<ListeningSet>(`/listening/sets/${setId}`, body),
  deleteSet: (setId: string) =>
    api.del<{ ok: boolean; deleted: string; returned_to_pool: number }>(`/listening/sets/${setId}`),
  assign: (setId: string, question_ids: string[]) =>
    api.post<ListeningSet & { unfiled: string[]; moved: { question_id: string; from_title: string | null }[] }>(
      `/listening/sets/${setId}/questions`,
      { question_ids },
    ),
  reorder: (id: string, set_ids: string[]) =>
    api.post<{ items: ListeningSet[] }>(`/listening/${id}/sets/reorder`, { set_ids }),
  preview: (id: string) => api.get<ListeningLearner>(`/listening/${id}/preview`),
  bulk: (body: { passage_ids: string[]; action: string; status?: string; level?: string }) =>
    api.post<BulkResult>("/listening/bulk", body),
};

export const learnerListeningApi = {
  meta: () => api.get<LearnerMeta>("/student/listening/meta"),
  list: (filters: Record<string, string | number | undefined>) =>
    api.get<Page<ListeningLearnerSummary>>(`/student/listening?${listeningKeys(filters)}`),
  get: (id: string) => api.get<ListeningLearner>(`/student/listening/${id}`),
};
