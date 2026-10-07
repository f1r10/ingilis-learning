// The word bank: teacher payloads and the learner's study cards.
//
// The editor is built from `/vocabulary/meta` rather than a local list, so an admin who
// changes the configured languages in Settings changes the pickers here on the next load.
// A patch body only ever carries editable fields: `/vocabulary/{id}` refuses an unknown
// key instead of quietly dropping a `status` the teacher thought they had saved.

import { api } from "./client";

export interface TaxonomyRef {
  id: string;
  name: string;
}

export interface Translation {
  id?: string;
  language: string;
  value: string;
}

export interface Example {
  id?: string;
  sentence: string;
  language?: string | null;
  translation?: string | null;
}

export interface VocabularySummary {
  id: string;
  word: string;
  learning_language: string | null;
  definition: string | null;
  ipa: string | null;
  part_of_speech: string | null;
  level: string | null;
  status: string;
  synonyms: string[];
  translation_languages: string[];
  example_count: number;
  tag_names: string[];
  has_audio: boolean;
  has_source: boolean;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface Vocabulary {
  id: string;
  word: string;
  learning_language: string | null;
  definition: string | null;
  ipa: string | null;
  part_of_speech: string | null;
  level: string | null;
  status: string;
  synonyms: string[];
  antonyms: string[];
  notes: string | null;
  audio_asset_id: string | null;
  /** The address the pronunciation is served from. The server writes it, so a payload
   * cannot name its own media path; `mediaUrl` turns it into a request. */
  audio_url: string | null;
  source_file_id: string | null;
  translations: Translation[];
  examples: Example[];
  tags: TaxonomyRef[];
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

/** What a learner is served: no notes, no provenance, no lifecycle state. */
export interface StudyCard {
  id: string;
  word: string;
  learning_language: string | null;
  level: string | null;
  part_of_speech: string | null;
  ipa: string | null;
  definition: string | null;
  synonyms: string[];
  antonyms: string[];
  translations: Translation[];
  examples: Example[];
  has_audio: boolean;
  /** The learner's own address for the pronunciation - a student session authorises the
   * bytes it serves, so a study card never carries the library's path. */
  audio_url: string | null;
  tags: TaxonomyRef[];
}

export interface VocabularyMeta {
  learning_languages: string[];
  translation_languages: string[];
  example_languages: string[];
  levels: string[];
  parts_of_speech: string[];
  statuses: string[];
}

/** What a learner's own filter pickers are built from. */
export interface LearnerMeta {
  learning_languages: string[];
  translation_languages: string[];
  levels: string[];
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** Counts would hide a partial win: the backend answers per id, so the UI shows which. */
export interface BulkResult {
  action: string;
  updated: string[];
  refused: { id: string; reason: string }[];
  not_found: string[];
}

export type VocabularyDraft = {
  word: string;
  learning_language: string;
  definition: string | null;
  ipa: string | null;
  part_of_speech: string | null;
  level: string | null;
  synonyms: string[];
  antonyms: string[];
  notes: string | null;
  audio_asset_id: string | null;
  translations: Translation[];
  examples: Example[];
  tag_ids: string[];
};

export const vocabularyKeys = (filters: Record<string, string | number | boolean | undefined>) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== "" && value !== null)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

export const vocabularyApi = {
  meta: () => api.get<VocabularyMeta>("/vocabulary/meta"),
  list: (filters: Record<string, string | number | boolean | undefined>) =>
    api.get<Page<VocabularySummary>>(`/vocabulary?${vocabularyKeys(filters)}`),
  get: (id: string) => api.get<Vocabulary>(`/vocabulary/${id}`),
  create: (body: VocabularyDraft & { status: string }) => api.post<Vocabulary>("/vocabulary", body),
  update: (id: string, body: Partial<VocabularyDraft>) => api.patch<Vocabulary>(`/vocabulary/${id}`, body),
  setStatus: (id: string, status: string) => api.post<Vocabulary>(`/vocabulary/${id}/status`, { status }),
  trash: (id: string) => api.del<{ ok: boolean; trashed: string; status: string }>(`/vocabulary/${id}`),
  restore: (id: string) => api.post<Vocabulary>(`/vocabulary/${id}/restore`),
  assignTags: (id: string, tag_ids: string[]) =>
    api.post<Vocabulary>(`/vocabulary/${id}/taxonomy`, { tag_ids }),
  /** The learner's projection of a saved entry, served to the teacher's own session. */
  preview: (id: string, language?: string) =>
    api.get<StudyCard>(`/vocabulary/${id}/preview${language ? `?language=${encodeURIComponent(language)}` : ""}`),
  bulk: (body: {
    entry_ids: string[];
    action: string;
    status?: string;
    tag_id?: string;
    level?: string;
  }) => api.post<BulkResult>("/vocabulary/bulk", body),
};

export const learnerVocabularyApi = {
  /** The learner's own filter options. `/vocabulary/meta` is an admin endpoint, so a
   * student session must not reach for it. */
  meta: () => api.get<LearnerMeta>("/student/vocabulary/meta"),
  list: (filters: Record<string, string | number | undefined>) =>
    api.get<Page<VocabularySummary>>(`/student/vocabulary?${vocabularyKeys(filters)}`),
  card: (id: string, language?: string) =>
    api.get<StudyCard>(`/student/vocabulary/${id}${language ? `?language=${encodeURIComponent(language)}` : ""}`),
};

/** The list's search box offers the four things a word search can mean. */
export const SEARCH_KINDS = ["contains", "word_only", "exact", "starts_with"];
export const SORTABLE = ["word", "level", "part_of_speech", "status", "created_at", "updated_at"];
