// Document import: a teacher's paper, the candidates it produced, and the content they become.
//
// The three stages the importer works in are visible in this file, because they are visible in
// the API: bytes decide the format, shapes decide the candidates, and a person decides the
// content. So the upload carries no content type and no parser choice - a file that could
// describe itself would be a way to store something behind a label the bytes do not support.
//
// Two rules from the backend shape everything here:
//
// * **A candidate is the document's words.** `extracted` is what the paper said and is never
//   overwritten by what a teacher typed; a correction lands in `corrected`, and the row says
//   afterwards whether it still reads as the file. Editing sends the whole candidate back,
//   because the server holds the list of fields that kind may have.
// * **Nothing is filed by accident.** A row with a gap cannot be approved at all, and the
//   refusal names the gaps - so a screen that greyed the button out would only hide which
//   field is missing. The approve control is offered and the server's sentence is shown.
//
// `filing` (level, lifecycle, language, topics, tags) stays a separate object for the same
// reason: it is a teacher's decision about the bank, never something extracted from a paper.
// `status` is a filing field and never a candidate field, and a body sent with the other name
// would be refused as an unknown key.
//
// The words the screens branch on - formats, kinds, question types, editable fields per kind,
// decisions, views, the missing-field codes, both ceilings - come from `/imports/meta` rather
// than being typed out here, because the code that enforces them is the only thing that knows
// the list.

import { api, mediaUrl } from "./client";

/** The candidate's own text, whichever kind it is. Every key here is one a parser already
 * fills, so a screen rendering `editable_fields[kind]` can name them all without casting. */
export interface CandidateOption {
  text: string;
  correct?: boolean;
}

export interface CandidateExample {
  sentence: string;
}

export interface CandidateText {
  prompt?: string;
  options?: CandidateOption[];
  accepted?: string[];
  answer_text?: string;
  explanation?: string;
  word?: string;
  definition?: string;
  examples?: CandidateExample[];
  title?: string;
  body?: string;
  text?: string;
  /** A level written in the paper. The teacher's own choice about a level is `filing.level`. */
  level?: string;
}

/** Where approved content goes in the bank. `language` is the learning language. */
export interface ImportFiling {
  status: string;
  level: string | null;
  language: string | null;
  topic_ids: string[];
  tag_ids: string[];
}

export interface ImportFormat {
  name: string;
  mime_type: string;
  extension: string;
  label: string;
}

export interface ImportMeta {
  formats: ImportFormat[];
  kinds: string[];
  approvable_kinds: string[];
  question_types: string[];
  editable_fields: Record<string, string[]>;
  filing_statuses: string[];
  job_statuses: string[];
  decisions: string[];
  views: string[];
  max_document_bytes: number;
  max_document_mb: number;
  auto_mode_default: boolean;
  low_confidence_threshold: number;
  sortable_jobs: string[];
  sortable_items: string[];
  max_bulk_items: number;
  /** The gap codes `missing` can hold, in the server's words. The screen says them in the
   * teacher's language as `imports.missing.<code>`. */
  missing_fields: Record<string, string>;
}

export interface ImportSource {
  id: string;
  title: string;
  original_filename: string | null;
  mime_type: string | null;
  format: string | null;
  format_label: string | null;
  bytes: number | null;
  page_count: number | null;
  language: string | null;
  trashed: boolean;
}

/** How far a queue has been decided. `incomplete` counts pending rows that still have a gap. */
export interface ImportCounts {
  pending: number;
  approved: number;
  rejected: number;
  edited: number;
  total: number;
  incomplete: number;
}

export interface ImportJob {
  id: string;
  status: string;
  auto_mode: boolean;
  profile: string | null;
  error: string | null;
  /** The worker's own progress: stage, counts, and `outcome` once the paper has been read. */
  progress: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  source: ImportSource | null;
  counts: ImportCounts;
}

export interface ImportItem {
  id: string;
  job_id: string;
  position: number;
  kind: string | null;
  type: string | null;
  page: number | null;
  sheet: string | null;
  confidence: number | null;
  decision: string;
  /** The document's words, exactly as they were read. */
  extracted: CandidateText;
  /** The teacher's version, or null while the candidate still reads as the file. */
  corrected: CandidateText | null;
  has_correction: boolean;
  missing: string[];
  filing: ImportFiling;
  note: string | null;
  /** The content this row filed, once it has: a kind and an id to link to. */
  result: { kind: string; id: string } | null;
  /** `false` once the row is filed - the bank is where that content is edited now. */
  editable: boolean;
  approvable: boolean;
  /** Low confidence, a gap, or text a teacher changed: the queue shows these first. */
  attention: boolean;
}

export interface ImportPage<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

/** A bulk answer names every row it touched: a mixed queue is shown row by row, never as one
 * "done" that hides the candidates still waiting. */
export interface ImportBulkResult {
  action: string;
  done: string[];
  refused: { id: string; code: string; params: Record<string, unknown>; reason: string }[];
  not_found: string[];
}

export const importKeys = (
  filters: Record<string, string | number | boolean | undefined | null>,
) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== null && value !== "" && value !== false)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

export const importsApi = {
  meta: () => api.get<ImportMeta>("/imports/meta"),

  list: (filters: Record<string, string | number | boolean | undefined | null> = {}) =>
    api.get<ImportPage<ImportJob>>(`/imports?${importKeys(filters)}`),

  /** 201 when these bytes are new, 200 with `duplicate` true when the same paper is already
   * here - and the payload is then the queue that exists rather than a second one. */
  upload: (file: File, options: { title?: string; autoMode?: boolean } = {}) => {
    const form = new FormData();
    form.append("file", file);
    if (options.title) form.append("title", options.title);
    // `auto_mode` is read as an optional flag: sending `false` would be asking for the
    // platform's default anyway, and sending nothing says the same thing more plainly.
    if (options.autoMode === true) form.append("auto_mode", "true");
    return api.postForm<ImportJob & { duplicate: boolean }>("/imports", form);
  },

  job: (jobId: string) => api.get<ImportJob>(`/imports/${jobId}`),
  items: (jobId: string, filters: Record<string, string | number | boolean | undefined | null> = {}) =>
    api.get<ImportPage<ImportItem>>(`/imports/${jobId}/items?${importKeys(filters)}`),

  /** The paper this queue was made from, as an attachment the application serves - no storage
   * key or self-authorising link ever reaches the browser. */
  documentUrl: (jobId: string) => mediaUrl(`/imports/${jobId}/document`),

  edit: (itemId: string, body: { kind?: string; type?: string; extracted?: CandidateText; filing?: Partial<ImportFiling> }) =>
    api.patch<ImportItem>(`/imports/items/${itemId}`, body),
  /** Approve or refuse one candidate. Filing may travel with an approval so the text and where
   * it lands are one decision. */
  decide: (itemId: string, body: { approve: boolean; filing?: Partial<ImportFiling> }) =>
    api.post<ImportItem>(`/imports/items/${itemId}/decision`, body),
  bulk: (body: { item_ids: string[]; action: string; filing?: Partial<ImportFiling> }) =>
    api.post<ImportBulkResult>("/imports/bulk", body),

  remove: (jobId: string) => api.del<{ ok: boolean; deleted: string }>(`/imports/${jobId}`),
  retry: (jobId: string) => api.post<ImportJob>(`/imports/${jobId}/retry`),
};
