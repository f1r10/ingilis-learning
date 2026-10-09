// The review queue: what one document said, and what a teacher decides it becomes.
//
// A candidate is the paper's own text until a person writes on it. `extracted` never changes,
// a correction is stored beside it, and the row says afterwards whether it still reads as the
// file - so the queue can answer "did the importer misread this, or did somebody change it?"
// The screens therefore send the whole candidate back rather than a patch: the server holds the
// list of fields that kind may have, refuses anything outside it, and can keep that claim.
//
// Two rules the interface is not allowed to work around:
//
// * **A gap stops an approval.** A row with a missing answer key, word or language cannot be
//   filed, and the refusal names the gaps. The button is still offered rather than hidden,
//   because the server's sentence is the part that tells a teacher which box to fill.
// * **A decision is final.** Once a row has produced content, this screen stops editing it and
//   links to the bank instead - the question lives there now, and re-deciding the candidate
//   would say something about the paper that is no longer true of it.
//
// Fields, kinds, question types, the lifecycle choices and the gap words all come from
// `/imports/meta`. A picker that carried its own list would offer something the importer
// refuses, and a new field in `editable_fields` appears here without a change to this file.

import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { importsApi, type CandidateText, type ImportBulkResult, type ImportFiling, type ImportItem } from "../api/imports";
import { readingApi } from "../api/reading";
import { tagsApi, topicsApi, type Tag, type TopicNode } from "../api/questions";
import { when, wordsFor } from "../i18n/format";

const PAGE_SIZE = 50;

/** The bank page each filed kind now lives on. */
const CONTENT_ROUTE: Record<string, (id: string) => string> = {
  question: (id) => `/questions/${id}`,
  vocabulary: (id) => `/vocabulary/${id}`,
  reading: (id) => `/reading/${id}`,
};

const IN_FLIGHT = new Set(["queued", "processing"]);

/** The two findings that leave a queue empty for a reason the teacher can act on. A failed read
 * already carries its own sentence in `job.error`, so nothing here guesses at a code it has no
 * words for. */
const EMPTY_FINDINGS = new Set(["no_text", "no_text_layer"]);

/** The text a candidate holds now: the teacher's version if there is one, the file's if not. */
const textOf = (item: ImportItem): CandidateText => item.corrected || item.extracted || {};

/** A word cannot be filed until a person says which language it is a word in, and the paper never
 * says: `language` is a filing choice, not a column. The row that is missing exactly that offers
 * the picker where the decision is made, so one approval is enough. */
function needsLanguage(item: ImportItem, languages: string[]): boolean {
  return item.editable && languages.length > 0 && (item.kind === "vocabulary" || item.missing.includes("language"));
}

export default function ImportReview() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { jobId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [bulkResult, setBulkResult] = useState<ImportBulkResult | null>(null);

  const filters = useMemo(
    () => ({
      view: params.get("view") || "queue",
      kind: params.get("kind") || "",
      decision: params.get("decision") || "",
      only_incomplete: params.get("incomplete") === "1",
      sort: params.get("sort") || "position",
      order: params.get("order") || "asc",
      page: Number(params.get("page") || 1),
      page_size: PAGE_SIZE,
    }),
    [params],
  );

  const setFilter = (patch: Record<string, string | number | boolean>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => {
      if (value === "" || value === undefined || value === false) next.delete(key);
      else next.set(key, String(value));
    });
    if (!("page" in patch)) next.delete("page");
    setParams(next);
  };

  const meta = useQuery({ queryKey: ["imports-meta"], queryFn: importsApi.meta });
  const bank = useQuery({ queryKey: ["reading-meta"], queryFn: readingApi.meta });
  const job = useQuery({
    queryKey: ["imports", "job", jobId],
    queryFn: () => importsApi.job(jobId),
    refetchInterval: (query) => (IN_FLIGHT.has(query.state.data?.status || "") ? 2_000 : false),
  });
  const list = useQuery({
    queryKey: ["imports", "items", jobId, filters],
    queryFn: () => importsApi.items(jobId, filters),
    refetchInterval: IN_FLIGHT.has(job.data?.status || "") ? 2_000 : false,
  });

  useEffect(() => {
    setSelected([]);
    setBulkResult(null);
  }, [params]);

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, list.data?.pages ?? Math.ceil(total / PAGE_SIZE));
  // Only a row still waiting can be picked up again, so "this page" means the rows on it that can
  // still be decided - a filed line checked beside them would only be refused by the bulk call.
  const selectable = rows.filter((item) => item.editable);
  const allSelected = selectable.length > 0 && selected.length === selectable.length;

  const refresh = () => qc.invalidateQueries({ queryKey: ["imports"] });

  /** A row that has been decided leaves the selection it was part of: a checkbox beside a
   * candidate that is now a question in the bank would be an invitation to file it twice. */
  const decided = (item: ImportItem) => {
    const filed = item.decision === "approved" || item.decision === "edited";
    setSelected((prev) => prev.filter((id) => id !== item.id));
    setMessage(
      t(filed ? "imports.approved_message" : "imports.rejected_message", { position: item.position + 1 }),
    );
    refresh();
  };

  const bulk = useMutation({
    mutationFn: (body: { action: string; filing?: Partial<ImportFiling> }) =>
      importsApi.bulk({ item_ids: selected.slice(0, meta.data?.max_bulk_items ?? 300), ...body }),
    onSuccess: (result) => {
      setBulkResult(result);
      setMessage(null);
      setSelected([]);
      refresh();
    },
    // A bulk body that cannot be carried at all - `file` with no filing to set - is refused as one
    // request; anything refused row by row comes back in `refused` and is shown from there.
    onError: (e: ApiError) => setMessage(e.message),
  });

  const saved = (item: ImportItem) => {
    setEditing(null);
    setMessage(t("imports.saved", { position: item.position + 1 }));
    refresh();
  };

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <Link className="btn ghost" to="/imports">
          ‹ {t("imports.title")}
        </Link>
        <span className="spacer" />
        {job.data ? (
          <a className="btn secondary" href={importsApi.documentUrl(jobId) || "#"}>
            {t("imports.original")}
          </a>
        ) : null}
      </div>

      <h1 style={{ margin: 0 }}>{job.data?.source?.title || t("common.loading")}</h1>
      {job.data ? (
        <div className="card row" style={{ flexWrap: "wrap", gap: 12 }}>
          <span className="chip">{t(`imports.job_status_${job.data.status}`)}</span>
          <span className="small muted">
            {t("imports.added")} {job.data.created_at ? when(job.data.created_at) : "—"}
          </span>
          <span className="small muted">
            {t("imports.counts", {
              total: job.data.counts.total,
              pending: job.data.counts.pending,
              filed: job.data.counts.approved + job.data.counts.edited,
              rejected: job.data.counts.rejected,
              incomplete: job.data.counts.incomplete,
            })}
          </span>
          {job.data.auto_mode ? <span className="small muted">{t("imports.auto_mode_on")}</span> : null}
          {job.data.error ? (
            <span className="small">{wordsFor(String(job.data.progress.outcome || ""), job.data.error)}</span>
          ) : null}
          {/* The two findings a paper can give with no queue at all are about the document, and a
              teacher who can send a scan for OCR has to be told that is what this one needs. */}
          {job.data.counts.total === 0 && EMPTY_FINDINGS.has(String(job.data.progress.outcome)) ? (
            <span className="small muted">{t(`imports.outcome_${String(job.data.progress.outcome)}`)}</span>
          ) : null}
        </div>
      ) : null}
      <p className="muted small">{t("imports.review_hint")}</p>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <select className="input" style={{ maxWidth: 150 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          {(meta.data?.views || ["queue", "all"]).map((view) => (
            <option key={view} value={view}>
              {t(`imports.view_${view}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.kind} onChange={(e) => setFilter({ kind: e.target.value })}>
          <option value="">{t("imports.all_kinds")}</option>
          {(meta.data?.kinds || []).map((kind) => (
            <option key={kind} value={kind}>
              {t(`imports.kind_${kind}`)}
            </option>
          ))}
        </select>
        <select
          className="input"
          style={{ maxWidth: 150 }}
          value={filters.decision}
          onChange={(e) => setFilter({ decision: e.target.value })}
        >
          <option value="">{t("imports.all_decisions")}</option>
          {(meta.data?.decisions || []).map((decision) => (
            <option key={decision} value={decision}>
              {t(`imports.decision_${decision}`)}
            </option>
          ))}
        </select>
        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={filters.only_incomplete} onChange={(e) => setFilter({ incomplete: e.target.checked })} />
          {t("imports.only_incomplete")}
        </label>
        <select className="input" style={{ maxWidth: 160 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {(meta.data?.sortable_items || ["position"]).map((sort) => (
            <option key={sort} value={sort}>
              {t(`imports.sort_item_${sort}`)}
            </option>
          ))}
        </select>
        <button type="button" className="btn secondary" onClick={() => setFilter({ order: filters.order === "desc" ? "asc" : "desc" })}>
          {filters.order === "desc" ? "↓" : "↑"}
        </button>
      </div>

      {message ? <div className="alert">{message}</div> : null}
      {list.isError ? (
        <div className="alert error">
          {t("common.could_not_load")} {(list.error as ApiError).message}
        </div>
      ) : null}

      {bulkResult ? (
        <div className="card stack">
          <div className="small">
            {t("imports.bulk_result", {
              done: bulkResult.done.length,
              refused: bulkResult.refused.length,
              missing: bulkResult.not_found.length,
            })}
          </div>
          {bulkResult.refused.length ? (
            <ul className="small muted">
              {bulkResult.refused.map((refusal) => (
                <li key={refusal.id}>
                  {/* The reason the server sent is the rule's own sentence; `code` says which rule,
                      and a row that has not been filed yet can be named by its position. */}
                  #{rows.find((row) => row.id === refusal.id)?.position ?? "?"} — {refusal.reason}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {selected.length ? (
        <BulkBar
          selected={selected}
          statuses={meta.data?.filing_statuses || []}
          levels={bank.data?.levels || []}
          languages={bank.data?.learning_languages || []}
          max={meta.data?.max_bulk_items ?? 300}
          busy={bulk.isPending}
          onRun={(action, filing) => bulk.mutate({ action, filing })}
        />
      ) : null}

      {IN_FLIGHT.has(job.data?.status || "") ? (
        <div className="alert">{t("imports.reading")}</div>
      ) : null}

      <div className="row" style={{ gap: 8 }}>
        <label className="row small" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() => setSelected(allSelected ? [] : rows.filter((item) => item.editable).map((item) => item.id))}
          />
          {t("imports.select_page")}
        </label>
        <span className="spacer" />
        <span className="small muted">{t("imports.page_counts", { shown: rows.length, total })}</span>
      </div>

      {rows.map((item) => (
        <ItemCard
          key={item.id}
          item={item}
          meta={meta.data}
          levels={bank.data?.levels || []}
          languages={bank.data?.learning_languages || []}
          selected={selected.includes(item.id)}
          editing={editing === item.id}
          onToggle={(checked) =>
            setSelected((prev) => (checked ? [...prev, item.id] : prev.filter((id) => id !== item.id)))
          }
          onEdit={() => setEditing(item.id)}
          onCloseEdit={() => setEditing(null)}
          onDecided={decided}
          onRefused={(e) => setMessage(e.message)}
          onSaved={saved}
        />
      ))}

      {!list.isLoading && rows.length === 0 ? (
        <div className="card muted">
          {filters.view === "queue" ? t("imports.queue_empty") : t("imports.empty")}
        </div>
      ) : null}

      <div className="row">
        <span className="small muted">{t("imports.showing", { total })}</span>
        <span className="spacer" />
        <button type="button" className="btn secondary" disabled={filters.page <= 1} onClick={() => setFilter({ page: filters.page - 1 })}>
          ‹
        </button>
        <span className="small">
          {filters.page} / {pages}
        </span>
        <button
          type="button"
          className="btn secondary"
          disabled={filters.page >= pages}
          onClick={() => setFilter({ page: filters.page + 1 })}
        >
          ›
        </button>
      </div>
    </div>
  );
}

function BulkBar({
  selected,
  statuses,
  levels,
  languages,
  max,
  busy,
  onRun,
}: {
  selected: string[];
  statuses: string[];
  levels: string[];
  languages: string[];
  max: number;
  busy: boolean;
  onRun: (action: string, filing?: Partial<ImportFiling>) => void;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState("draft");
  const [level, setLevel] = useState("");
  const [language, setLanguage] = useState("");

  return (
    <div className="card stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <strong className="small">{t("imports.selected", { n: selected.length, max })}</strong>
        <span className="spacer" />
        <button type="button" className="btn" disabled={busy} onClick={() => onRun("approve")}>
          {t("imports.approve_selected")}
        </button>
        <button type="button" className="btn secondary" disabled={busy} onClick={() => onRun("reject")}>
          {t("imports.reject_selected")}
        </button>
      </div>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        {/* Filing a page is a decision about the bank, not about the paper, so it is its own action
            and it carries its own lifecycle choice: `status` has a value whether or not the teacher
            touches it, and saying so here is what keeps a row from being quietly moved back to
            draft. */}
        <select className="input" style={{ maxWidth: 130 }} value={status} onChange={(e) => setStatus(e.target.value)}>
          {statuses.map((value) => (
            <option key={value} value={value}>
              {t(`status.${value}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 130 }} value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">{t("imports.level_keep")}</option>
          {levels.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 130 }} value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="">{t("imports.language_keep")}</option>
          {languages.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn secondary"
          disabled={busy}
          onClick={() =>
            onRun("file", {
              status,
              level: level || null,
              language: language || null,
              topic_ids: [],
              tag_ids: [],
            })
          }
        >
          {t("imports.file_selected")}
        </button>
      </div>
      <div className="small muted">{t("imports.bulk_hint")}</div>
    </div>
  );
}

function ItemCard({
  item,
  meta,
  levels,
  languages,
  selected,
  editing,
  onToggle,
  onEdit,
  onCloseEdit,
  onDecided,
  onRefused,
  onSaved,
}: {
  item: ImportItem;
  meta?: {
    kinds: string[];
    question_types: string[];
    editable_fields: Record<string, string[]>;
    filing_statuses: string[];
    low_confidence_threshold: number;
  };
  levels: string[];
  languages: string[];
  selected: boolean;
  editing: boolean;
  onToggle: (checked: boolean) => void;
  onEdit: () => void;
  onCloseEdit: () => void;
  onDecided: (item: ImportItem) => void;
  onRefused: (e: ApiError) => void;
  onSaved: (item: ImportItem) => void;
}) {
  const { t } = useTranslation();
  // The language the word is filed in, chosen on the row that needs it. It is not a field of the
  // candidate - a paper does not say which language its own words are - so it travels with the
  // approval, and a row that already has one starts from it.
  const [language, setLanguage] = useState(item.filing?.language || "");
  const topics = useQuery({ queryKey: ["topics"], queryFn: topicsApi.list, enabled: editing });
  const tags = useQuery({ queryKey: ["tags"], queryFn: tagsApi.list, enabled: editing });
  const edit = useMutation({
    mutationFn: (body: { kind?: string; type?: string; extracted?: CandidateText; filing?: Partial<ImportFiling> }) =>
      importsApi.edit(item.id, body),
    onSuccess: onSaved,
    onError: (e: ApiError) => onRefused(e),
  });

  // A word is filed in a language or not at all, and an approval may carry that choice with it, so
  // the row that needs one offers it here. A level or language left unset is sent as null, which
  // the server reads as "keep what the row already has".
  const decide = useMutation({
    mutationFn: (approve: boolean) =>
      importsApi.decide(item.id, {
        approve,
        filing: {
          status: item.filing?.status || "draft",
          level: item.filing?.level || null,
          language: language || null,
          topic_ids: item.filing?.topic_ids || [],
          tag_ids: item.filing?.tag_ids || [],
        },
      }),
    onSuccess: onDecided,
    // An incomplete candidate refuses with the fields it still needs, and a word the bank already
    // holds answers the same way as anywhere else: the row did not move, so the sentence the
    // server sent is the whole answer.
    onError: (e: ApiError) => onRefused(e),
  });

  const low =
    item.confidence !== null &&
    meta !== undefined &&
    item.confidence < meta.low_confidence_threshold;

  return (
    <div className="card stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        {item.editable ? (
          <input type="checkbox" checked={selected} onChange={(e) => onToggle(e.target.checked)} />
        ) : null}
        <strong className="small">#{item.position + 1}</strong>
        <span className="chip">{t(`imports.kind_${item.kind || "note"}`)}</span>
        {item.type ? <span className="small mono">{item.type}</span> : null}
        {item.page ? <span className="small muted">{t("imports.page_n", { n: item.page })}</span> : null}
        {item.sheet ? <span className="small muted mono">{item.sheet}</span> : null}
        {item.confidence !== null ? (
          <span className="small muted">{t("imports.confidence", { n: Math.round(item.confidence * 100) })}</span>
        ) : null}
        <span className="small">{t(`imports.decision_${item.decision}`)}</span>
        {item.has_correction ? <span className="chip">{t("imports.corrected")}</span> : null}
        {low ? <span className="chip">{t("imports.low_confidence")}</span> : null}
        <span className="spacer" />
        {item.editable ? (
          <>
            {needsLanguage(item, languages) ? (
              <select className="input" style={{ maxWidth: 110 }} value={language} onChange={(e) => setLanguage(e.target.value)}>
                <option value="">{t("imports.language_choose")}</option>
                {languages.map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </select>
            ) : null}
            <button type="button" className="btn secondary" onClick={onEdit} disabled={editing}>
              {t("imports.edit")}
            </button>
            <button type="button" className="btn" onClick={() => decide.mutate(true)} disabled={decide.isPending}>
              {t("imports.approve")}
            </button>
            <button type="button" className="btn ghost" onClick={() => decide.mutate(false)} disabled={decide.isPending}>
              {t("imports.reject")}
            </button>
          </>
        ) : item.result ? (
          <Link className="btn ghost" to={CONTENT_ROUTE[item.result.kind]?.(item.result.id) || "/imports"}>
            {t("imports.in_the_bank")}
          </Link>
        ) : null}
      </div>

      {item.missing.length ? (
        <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
          <span className="small">{t("imports.still_needs")}:</span>
          {item.missing.map((code) => (
            <span key={code} className="chip">
              {t(`imports.missing.${code}`)}
            </span>
          ))}
        </div>
      ) : null}
      {item.note ? <div className="small muted">{wordsFor(item.note, item.note)}</div> : null}

      {editing ? (
        <ItemEditor
          item={item}
          kinds={meta?.kinds || []}
          questionTypes={meta?.question_types || []}
          fields={meta?.editable_fields || {}}
          statuses={meta?.filing_statuses || []}
          levels={levels}
          languages={languages}
          topics={flattenTopics(topics.data?.items || [])}
          tags={tags.data?.items || []}
          busy={edit.isPending}
          onCancel={onCloseEdit}
          onSubmit={(body) => edit.mutate(body)}
        />
      ) : (
        <TextRead kind={item.kind || "note"} fields={meta?.editable_fields || {}} text={textOf(item)} />
      )}
    </div>
  );
}

/** The candidate's text as the editor holds it: the same keys the server's field list names. */
type TextDraft = Record<string, unknown>;

function asOptions(value: unknown): { text: string; correct: boolean }[] {
  if (!Array.isArray(value)) return [];
  return value.map((option) =>
    typeof option === "string"
      ? { text: option, correct: false }
      : { text: String((option as { text?: unknown })?.text ?? ""), correct: Boolean((option as { correct?: unknown })?.correct) },
  );
}

function asExamples(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((example) =>
    typeof example === "string" ? example : String((example as { sentence?: unknown })?.sentence ?? ""),
  );
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** The editor's starting text for one kind: that kind's fields, filled from whatever the
 * candidate already holds under the same name. A field the new kind has no room for is left
 * out of the payload rather than dropped from the row - `extracted` still carries the paper's
 * own words, which is what the provenance claim is for. */
function skeleton(kind: string, fields: Record<string, string[]>, current: CandidateText): TextDraft {
  const draft: TextDraft = {};
  (fields[kind] || []).forEach((field) => {
    const value = (current as Record<string, unknown>)[field];
    if (field === "options") draft[field] = asOptions(value);
    else if (field === "examples") draft[field] = asExamples(value);
    else if (field === "accepted") draft[field] = asStrings(value);
    else draft[field] = typeof value === "string" ? value : "";
  });
  return draft;
}

const LONG_FIELDS = new Set(["prompt", "body", "definition", "explanation", "text"]);

function ItemEditor({
  item,
  kinds,
  questionTypes,
  fields,
  statuses,
  levels,
  languages,
  topics,
  tags,
  busy,
  onCancel,
  onSubmit,
}: {
  item: ImportItem;
  kinds: string[];
  questionTypes: string[];
  fields: Record<string, string[]>;
  statuses: string[];
  levels: string[];
  languages: string[];
  topics: { id: string; name: string }[];
  tags: Tag[];
  busy: boolean;
  onCancel: () => void;
  onSubmit: (body: {
    kind?: string;
    type?: string;
    extracted?: CandidateText;
    filing?: Partial<ImportFiling>;
  }) => void;
}) {
  const { t } = useTranslation();
  const [kind, setKind] = useState(item.kind || "note");
  const [type, setType] = useState(item.type || "");
  const [text, setText] = useState<TextDraft>(() => skeleton(item.kind || "note", fields, textOf(item)));
  const [filing, setFiling] = useState({
    status: item.filing?.status || "draft",
    level: item.filing?.level || "",
    language: item.filing?.language || "",
    topic_ids: item.filing?.topic_ids || [],
    tag_ids: item.filing?.tag_ids || [],
  });

  const names = fields[kind] || [];
  const knownTypes = questionTypes.includes(type) || !type ? questionTypes : [type, ...questionTypes];

  const changeKind = (next: string) => {
    setKind(next);
    setText(skeleton(next, fields, text as CandidateText));
    if (next !== "question") setType("");
  };

  const setField = (field: string, value: unknown) => setText((prev) => ({ ...prev, [field]: value }));

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("imports.as_kind")}</label>
          <select className="input" value={kind} onChange={(e) => changeKind(e.target.value)}>
            {(kinds.length ? kinds : [kind]).map((value) => (
              <option key={value} value={value}>
                {t(`imports.kind_${value}`)}
              </option>
            ))}
          </select>
        </div>
        {kind === "question" ? (
          <div className="field" style={{ margin: 0 }}>
            <label>{t("imports.question_type")}</label>
            <select className="input" value={type} onChange={(e) => setType(e.target.value)}>
              <option value="">{t("imports.type_keep")}</option>
              {knownTypes.map((value) => (
                <option key={value} value={value}>
                  {t(`questions.type_${value}`, { defaultValue: value })}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>

      {kind !== (item.kind || "note") ? <div className="small muted">{t("imports.refiled")}</div> : null}

      {names.map((field) => {
        const value = text[field];
        if (field === "options") {
          return (
            <OptionList
              key={field}
              label={t(`imports.field_${field}`)}
              options={asOptions(value)}
              multi={type === "multi_select"}
              onChange={(options) => setField(field, options)}
            />
          );
        }
        if (field === "examples") {
          return (
            <StringList
              key={field}
              label={t(`imports.field_${field}`)}
              placeholder={t("imports.example_placeholder")}
              values={asExamples(value)}
              onChange={(values) => setField(field, values)}
            />
          );
        }
        if (field === "accepted") {
          return (
            <StringList
              key={field}
              label={t(`imports.field_${field}`)}
              placeholder={t("imports.accepted_placeholder")}
              values={asStrings(value)}
              onChange={(values) => setField(field, values)}
            />
          );
        }
        return (
          <div className="field" key={field}>
            <label>{t(`imports.field_${field}`)}</label>
            {LONG_FIELDS.has(field) ? (
              <textarea
                className="input"
                rows={field === "body" ? 8 : 3}
                value={String(value ?? "")}
                onChange={(e) => setField(field, e.target.value)}
              />
            ) : (
              <input
                className="input"
                value={String(value ?? "")}
                onChange={(e) => setField(field, e.target.value)}
              />
            )}
          </div>
        );
      })}

      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("imports.filing_status")}</label>
          <select className="input" value={filing.status} onChange={(e) => setFiling({ ...filing, status: e.target.value })}>
            {statuses.map((value) => (
              <option key={value} value={value}>
                {t(`status.${value}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("imports.filing_level")}</label>
          <select className="input" value={filing.level} onChange={(e) => setFiling({ ...filing, level: e.target.value })}>
            <option value="">{t("imports.level_keep")}</option>
            {levels.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("imports.filing_language")}</label>
          <select
            className="input"
            value={filing.language}
            onChange={(e) => setFiling({ ...filing, language: e.target.value })}
          >
            <option value="">{t("imports.language_keep")}</option>
            {languages.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <div className="field" style={{ margin: 0, flex: "1 1 220px" }}>
          <label>{t("imports.topics")}</label>
          <select
            className="input"
            multiple
            size={Math.min(8, Math.max(3, topics.length))}
            value={filing.topic_ids}
            onChange={(e) =>
              setFiling({ ...filing, topic_ids: Array.from(e.target.selectedOptions).map((o) => o.value) })
            }
          >
            {topics.map((node) => (
              <option key={node.id} value={node.id}>
                {node.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ margin: 0, flex: "1 1 220px" }}>
          <label>{t("imports.tags")}</label>
          <select
            className="input"
            multiple
            size={Math.min(8, Math.max(3, tags.length))}
            value={filing.tag_ids}
            onChange={(e) =>
              setFiling({ ...filing, tag_ids: Array.from(e.target.selectedOptions).map((o) => o.value) })
            }
          >
            {tags.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="row" style={{ gap: 8 }}>
        <button
          type="button"
          className="btn"
          // The field list comes from the server, and without it this screen would be sending a
          // text it has no names for.
          disabled={busy || names.length === 0}
          onClick={() =>
            onSubmit({
              kind,
              type: kind === "question" && type ? type : undefined,
              extracted: text as CandidateText,
              filing: {
                status: filing.status,
                level: filing.level || null,
                language: filing.language || null,
                topic_ids: filing.topic_ids,
                tag_ids: filing.tag_ids,
              },
            })
          }
        >
          {busy ? t("imports.saving") : t("imports.save")}
        </button>
        <button type="button" className="btn ghost" onClick={onCancel} disabled={busy}>
          {t("common.cancel")}
        </button>
        <span className="small muted">{t("imports.edit_hint")}</span>
      </div>
    </div>
  );
}

/** The choice list. A tick here is the answer key, so it is the one part of a candidate a
 * teacher cannot leave for the platform to decide - a row with no tick stays unapprovable. */
function OptionList({
  label,
  options,
  multi,
  onChange,
}: {
  label: string;
  options: { text: string; correct: boolean }[];
  multi: boolean;
  onChange: (options: { text: string; correct: boolean }[]) => void;
}) {
  const { t } = useTranslation();
  const set = (index: number, patch: Partial<{ text: string; correct: boolean }>) =>
    onChange(options.map((option, i) => (i === index ? { ...option, ...patch } : option)));

  const tick = (index: number, checked: boolean) => {
    if (!checked) {
      onChange(options.map((option, i) => (i === index ? { ...option, correct: false } : option)));
      return;
    }
    // One answer per line for a single-choice question, as many as the teacher ticks for a
    // `multi_select`: which shape the key has is the question's own type, not this screen's guess.
    onChange(options.map((option, i) => ({ ...option, correct: multi ? (i === index ? true : option.correct) : i === index })));
  };

  return (
    <div className="field">
      <label>{label}</label>
      <div className="stack">
        {options.map((option, index) => (
          <div className="row" key={index} style={{ gap: 8 }}>
            <input
              className="input"
              value={option.text}
              onChange={(e) => set(index, { text: e.target.value })}
            />
            <label className="row small" style={{ gap: 6 }}>
              <input type="checkbox" checked={option.correct} onChange={(e) => tick(index, e.target.checked)} />
              {t("imports.is_answer")}
            </label>
            <button type="button" className="btn ghost" onClick={() => onChange(options.filter((_, i) => i !== index))}>
              {t("imports.remove_option")}
            </button>
          </div>
        ))}
        <div>
          <button type="button" className="btn secondary" onClick={() => onChange([...options, { text: "", correct: false }])}>
            {t("common.add")}
          </button>
        </div>
      </div>
    </div>
  );
}

function StringList({
  label,
  placeholder,
  values,
  onChange,
}: {
  label: string;
  placeholder: string;
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="field">
      <label>{label}</label>
      <div className="stack">
        {values.map((value, index) => (
          <div className="row" key={index} style={{ gap: 8 }}>
            <input
              className="input"
              placeholder={placeholder}
              value={value}
              onChange={(e) => onChange(values.map((item, i) => (i === index ? e.target.value : item)))}
            />
            <button type="button" className="btn ghost" onClick={() => onChange(values.filter((_, i) => i !== index))}>
              {t("imports.remove_option")}
            </button>
          </div>
        ))}
        <div>
          <button type="button" className="btn secondary" onClick={() => onChange([...values, ""])}>
            {t("common.add")}
          </button>
        </div>
      </div>
    </div>
  );
}

function TextRead({ kind, fields, text }: { kind: string; fields: Record<string, string[]>; text: CandidateText }) {
  const { t } = useTranslation();
  const names = fields[kind] || [];
  if (!names.length) return null;
  return (
    <div className="stack">
      {names.map((field) => (
        <div key={field}>
          <div className="small muted">{t(`imports.field_${field}`)}</div>
          <FieldText field={field} value={(text as Record<string, unknown>)[field]} />
        </div>
      ))}
    </div>
  );
}

function FieldText({ field, value }: { field: string; value: unknown }) {
  const { t } = useTranslation();
  if (field === "options" && Array.isArray(value)) {
    return (
      <ul className="small">
        {value.map((option, index) => (
          <li key={index}>
            {(option as { text?: string; correct?: boolean })?.text ?? String(option)}
            {(option as { correct?: boolean })?.correct ? ` — ${t("imports.is_answer")}` : ""}
          </li>
        ))}
      </ul>
    );
  }
  if (field === "examples" && Array.isArray(value)) {
    return (
      <ul className="small">
        {value.map((example, index) => (
          <li key={index}>{(example as { sentence?: string })?.sentence ?? String(example)}</li>
        ))}
      </ul>
    );
  }
  if (Array.isArray(value)) {
    return <div className="small mono">{value.map(String).join(" · ")}</div>;
  }
  if (typeof value === "string") {
    return <div style={{ whiteSpace: "pre-wrap" }}>{value}</div>;
  }
  return <span className="muted small">—</span>;
}

function flattenTopics(nodes: TopicNode[]): { id: string; name: string }[] {
  return nodes.flatMap((node) => [{ id: node.id, name: node.name }, ...flattenTopics(node.children || [])]);
}
