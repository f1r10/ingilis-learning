// The media library: what a teacher uploads, what the player measured, what is using it.
//
// The upload screen is built from `/media/meta`, so the formats and size ceilings it
// promises are the ones the backend actually enforces. Nothing here can name a file's
// type: the server reads the bytes, and a payload that could claim "this is a PNG" would
// be a way to store something else behind that label.

import { api } from "./client";

export interface MediaFormat {
  mime_type: string;
  kind: string;
  extension: string;
  label: string;
}

/** Which live content points at a file - the reason a trash can be refused. */
export interface MediaReferences {
  words: number;
  listenings: number;
  questions: number;
}

export interface MediaSummary {
  id: string;
  kind: string;
  mime_type: string | null;
  format_label: string | null;
  original_filename: string | null;
  label: string | null;
  size_bytes: number | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  source_origin: string | null;
  state: string;
  content_url: string;
  reference_count: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface MediaAsset extends MediaSummary {
  referenced_by: MediaReferences;
}

/** What a learner's player is given: what to play and nothing about the library. */
export interface MediaLearner {
  id: string;
  kind: string;
  mime_type: string | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  content_url: string;
}

export interface MediaMeta {
  formats: MediaFormat[];
  kinds: string[];
  max_upload_bytes: Record<string, number>;
  max_upload_mb: Record<string, number>;
  views: string[];
  sortable: string[];
  states: string[];
}

/** What the player reports after it has played the file - the only honest duration. */
export type MediaPatch = {
  duration_seconds?: number;
  width?: number;
  height?: number;
  label?: string;
};

export interface BulkResult {
  action: string;
  updated: string[];
  refused: { id: string; reason: string }[];
  not_found: string[];
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export const mediaKeys = (filters: Record<string, string | number | undefined>) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

export const mediaApi = {
  meta: () => api.get<MediaMeta>("/media/meta"),
  list: (filters: Record<string, string | number | undefined>) =>
    api.get<Page<MediaSummary>>(`/media?${mediaKeys(filters)}`),
  get: (id: string) => api.get<MediaAsset>(`/media/${id}`),
  /** 201 when the bytes were new, 200 when the library already held them. */
  upload: (file: File, label?: string) => {
    const form = new FormData();
    form.append("file", file);
    if (label) form.append("label", label);
    return api.postForm<MediaAsset & { deduplicated: boolean }>("/media", form);
  },
  patch: (id: string, body: MediaPatch) => api.patch<MediaAsset>(`/media/${id}`, body),
  trash: (id: string) => api.del<{ ok: boolean; trashed: string; state: string }>(`/media/${id}`),
  restore: (id: string) => api.post<MediaAsset>(`/media/${id}/restore`),
  bulk: (body: { asset_ids: string[]; action: string }) =>
    api.post<BulkResult>("/media/bulk", body),
};

export const MEDIA_SORTABLE = [
  "created_at",
  "updated_at",
  "original_filename",
  "size_bytes",
  "duration_seconds",
  "kind",
];
