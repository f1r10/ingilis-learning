// Catalogs: the teacher's reusable practice collections.
//
// A catalog holds *references* into the four central banks, never a copy of the content,
// so an exercise corrected in its own bank is corrected here too. That is also why there
// is nothing in this file about versions or scores: a catalog is always current, and the
// run a learner opens is built from the rows as they stand.
//
// `/meta` supplies every picker on the screen - languages, levels, item kinds, feedback
// timings, caps - so a change in Settings or in the backend's own limits is reflected
// without editing this file.

import { api } from "./client";

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

/** One reference, with the content it names described from that row rather than copied.
 * `state` is the teacher's honest answer about a hole in the collection: `missing` means
 * the row is gone, `broken_block` means the block this reference named was deleted. */
export interface CatalogItem {
  id: string;
  kind: string;
  ref_id: string;
  position: number;
  config: Record<string, unknown>;
  title: string | null;
  detail: string | null;
  state: string;
  available_to_learner: boolean;
}

export interface CatalogSummary {
  id: string;
  name: string;
  description: string | null;
  parent_id: string | null;
  parent_name: string | null;
  learning_language: string | null;
  level: string | null;
  shuffle_default: boolean;
  known_states_enabled: boolean;
  feedback_timing: string;
  status: string;
  item_count: number;
  counts: Record<string, number>;
  /** `null` means the list did not resolve references for this row; only the detail does. */
  unavailable_count: number | null;
  child_count: number;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface Crumb {
  id: string;
  name: string;
}

export interface Catalog extends CatalogSummary {
  path: Crumb[];
  items: CatalogItem[];
  /** This catalog ready and un-trashed *and* every folder above it the same. */
  available_to_learner: boolean;
}

export interface ItemKind {
  kind: string;
  label: string;
}

export interface CatalogMeta {
  learning_languages: string[];
  levels: string[];
  statuses: string[];
  views: string[];
  sortable: string[];
  item_kinds: ItemKind[];
  bulk_actions: string[];
  feedback_timings: string[];
  max_items: number;
  max_depth: number;
  item_states: string[];
}

export type CatalogDraft = {
  name: string;
  description?: string | null;
  parent_id?: string | null;
  learning_language?: string | null;
  level?: string | null;
  shuffle_default?: boolean;
  known_states_enabled?: boolean;
  feedback_timing?: string;
};

/** What one reference may say about itself: the single block of a passage it names. */
export type ItemConfig = { set_id?: string | null };

export type ItemAdd = { kind: string; ref_id: string; config?: ItemConfig };

/** Query strings for the two routes that take one: the list's filters and the preview's
 * options. Empty and absent mean the same thing to both endpoints, so neither is sent. */
export const catalogKeys = (filters: Record<string, string | number | boolean | undefined>) =>
  Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

/** What the teacher's preview serves: the same steps a learner would get, minus the run
 * token, because nothing was written and no answer can be filed against it. */
export interface PreviewRun {
  catalog_id: string;
  catalog_name: string;
  feedback_timing: string;
  shuffle: boolean;
  known_states_enabled: boolean;
  skipped_count: number;
  steps: { item_id: string; kind: string; ref_id: string; position: number; view: Record<string, any> }[];
}

export const catalogsApi = {
  meta: () => api.get<CatalogMeta>("/catalogs/meta"),
  list: (filters: Record<string, string | number | boolean | undefined>) =>
    api.get<Page<CatalogSummary>>(`/catalogs?${catalogKeys(filters)}`),
  get: (id: string, includeTrash = true) =>
    api.get<Catalog>(`/catalogs/${id}?include_trash=${includeTrash}`),
  create: (body: CatalogDraft & { status?: string }) => api.post<Catalog>("/catalogs", body),
  update: (id: string, body: CatalogDraft) => api.patch<Catalog>(`/catalogs/${id}`, body),
  setStatus: (id: string, status: string) => api.post<Catalog>(`/catalogs/${id}/status`, { status }),
  trash: (id: string) => api.del<{ ok: boolean; trashed: string; status: string }>(`/catalogs/${id}`),
  restore: (id: string) => api.post<Catalog>(`/catalogs/${id}/restore`),
  preview: (id: string, options?: { shuffle?: boolean; language?: string }) => {
    const query = catalogKeys({ shuffle: options?.shuffle, language: options?.language });
    return api.get<PreviewRun>(`/catalogs/${id}/preview${query ? `?${query}` : ""}`);
  },
  items: (id: string) => api.get<{ items: CatalogItem[] }>(`/catalogs/${id}/items`),
  /** Append after the references already held. All checked, or nothing written. */
  addItems: (id: string, items: ItemAdd[]) =>
    api.post<{ added: CatalogItem[]; catalog: Catalog }>(`/catalogs/${id}/items`, { items }),
  updateItem: (itemId: string, config: ItemConfig) =>
    api.patch<CatalogItem>(`/catalogs/items/${itemId}`, { config }),
  removeItem: (itemId: string) =>
    api.del<{ ok: boolean; removed: string; content_untouched: boolean; items_left: number }>(
      `/catalogs/items/${itemId}`,
    ),
  /** The request must name every item of this catalog, in the order wanted. */
  reorderItems: (id: string, itemIds: string[]) =>
    api.post<{ items: CatalogItem[] }>(`/catalogs/${id}/items/reorder`, { item_ids: itemIds }),
  bulk: (body: { catalog_ids: string[]; action: string; status?: string; level?: string }) =>
    api.post<BulkResult>("/catalogs/bulk", body),
};
