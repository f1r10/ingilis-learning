// The teacher's word bank: filter it, work through it in bulk, and restore from the
// trash. Everything the backend refuses is shown per word, because "3 of 40 updated"
// is not an answer a teacher can act on.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { tagsApi } from "../api/questions";
import {
  SEARCH_KINDS,
  SORTABLE,
  vocabularyApi,
  type BulkResult,
  type VocabularySummary,
} from "../api/vocabulary";

export default function Vocabulary() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<string[]>([]);
  const [bulkLevel, setBulkLevel] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [bulkResult, setBulkResult] = useState<BulkResult | null>(null);

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      q_kind: params.get("q_kind") || "contains",
      learning_language: params.get("learning_language") || "",
      translation_language: params.get("translation_language") || "",
      level: params.get("level") || "",
      part_of_speech: params.get("part_of_speech") || "",
      status: params.get("status") || "",
      tag_id: params.get("tag_id") || "",
      has_audio: params.get("has_audio") || "",
      view: params.get("view") || "bank",
      sort: params.get("sort") || "word",
      order: params.get("order") || "asc",
      page: Number(params.get("page") || 1),
      page_size: 25,
    }),
    [params],
  );

  const setFilter = (patch: Record<string, string | number>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => {
      if (value === "" || value === undefined) next.delete(key);
      else next.set(key, String(value));
    });
    if (!("page" in patch)) next.delete("page");
    setParams(next);
  };

  const meta = useQuery({ queryKey: ["vocabulary-meta"], queryFn: vocabularyApi.meta });
  const tags = useQuery({ queryKey: ["tags"], queryFn: tagsApi.list });
  const list = useQuery({ queryKey: ["vocabulary", filters], queryFn: () => vocabularyApi.list(filters) });

  useEffect(() => {
    setSelected([]);
  }, [params]);

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / filters.page_size));
  const allSelected = rows.length > 0 && selected.length === rows.length;
  const nameOf = (id: string) => rows.find((row) => row.id === id)?.word || id.slice(0, 8);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["vocabulary"] });
    qc.invalidateQueries({ queryKey: ["tags"] });
  };

  const bulk = useMutation({
    mutationFn: (body: Record<string, string | undefined>) =>
      vocabularyApi.bulk({ entry_ids: selected, ...(body as object) } as Parameters<typeof vocabularyApi.bulk>[0]),
    onSuccess: (result) => {
      setBulkResult(result);
      setMessage(null);
      setSelected([]);
      refresh();
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const rowAction = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "trash" | "restore" | "status"; status?: string }) => {
      if (action === "trash") return vocabularyApi.trash(id);
      if (action === "restore") return vocabularyApi.restore(id);
      return vocabularyApi.setStatus(id, status || "ready");
    },
    onSuccess: refresh,
    onError: (e: ApiError) => setMessage(e.message),
  });

  const options = (values: string[] | undefined, allKey: string, labelKey?: string) => (
    <>
      <option value="">{t(allKey)}</option>
      {(values || []).map((value) => (
        <option key={value} value={value}>
          {labelKey ? t(`${labelKey}.${value}`) : value}
        </option>
      ))}
    </>
  );

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("vocabulary.title")}</h1>
        <span className="spacer" />
        <Link className="btn" to="/vocabulary/new">
          {t("vocabulary.new")}
        </Link>
      </div>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 200 }}
          placeholder={t("vocabulary.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 170 }} value={filters.q_kind} onChange={(e) => setFilter({ q_kind: e.target.value })}>
          {SEARCH_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {t(`vocabulary.search_${kind}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.status} onChange={(e) => setFilter({ status: e.target.value })}>
          {options(meta.data?.statuses, "vocabulary.all_statuses", "status")}
        </select>
        <select className="input" style={{ maxWidth: 110 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          {options(meta.data?.levels, "vocabulary.all_levels")}
        </select>
        <select
          className="input"
          style={{ maxWidth: 150 }}
          value={filters.part_of_speech}
          onChange={(e) => setFilter({ part_of_speech: e.target.value })}
        >
          {options(meta.data?.parts_of_speech, "vocabulary.all_parts_of_speech")}
        </select>
        <select
          className="input"
          style={{ maxWidth: 150 }}
          value={filters.learning_language}
          onChange={(e) => setFilter({ learning_language: e.target.value })}
        >
          {options(meta.data?.learning_languages, "vocabulary.all_learning_languages")}
        </select>
        <select
          className="input"
          style={{ maxWidth: 160 }}
          value={filters.translation_language}
          onChange={(e) => setFilter({ translation_language: e.target.value })}
        >
          {options(meta.data?.translation_languages, "vocabulary.all_translation_languages")}
        </select>
        <select className="input" style={{ maxWidth: 160 }} value={filters.tag_id} onChange={(e) => setFilter({ tag_id: e.target.value })}>
          <option value="">{t("vocabulary.all_tags")}</option>
          {(tags.data?.items || []).map((tag) => (
            <option key={tag.id} value={tag.id}>
              {tag.name}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.has_audio} onChange={(e) => setFilter({ has_audio: e.target.value })}>
          <option value="">{t("vocabulary.any_audio")}</option>
          <option value="true">{t("vocabulary.with_audio")}</option>
          <option value="false">{t("vocabulary.without_audio")}</option>
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          <option value="bank">{t("vocabulary.view_bank")}</option>
          <option value="trash">{t("vocabulary.view_trash")}</option>
          <option value="all">{t("vocabulary.view_all")}</option>
        </select>
        <select className="input" style={{ maxWidth: 160 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {SORTABLE.map((sort) => (
            <option key={sort} value={sort}>
              {t(`vocabulary.sort_${sort}`)}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn secondary"
          onClick={() => setFilter({ order: filters.order === "desc" ? "asc" : "desc" })}
        >
          {filters.order === "desc" ? "↓" : "↑"}
        </button>
      </div>

      {message ? <div className="alert error">{message}</div> : null}

      {list.isError ? (
        <div className="alert error">
          {t("common.could_not_load")} {(list.error as ApiError).message}
        </div>
      ) : null}

      {bulkResult ? (
        <div className="card stack">
          <div className="small">
            {t("vocabulary.bulk_result", {
              updated: bulkResult.updated.length,
              refused: bulkResult.refused.length,
              missing: bulkResult.not_found.length,
            })}
          </div>
          {bulkResult.refused.length ? (
            <ul className="small muted">
              {bulkResult.refused.map((item) => (
                <li key={item.id}>
                  {nameOf(item.id)} — {item.reason}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {selected.length ? (
        <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
          <strong className="small">{t("vocabulary.selected", { n: selected.length })}</strong>
          {(meta.data?.statuses || []).map((status) => (
            <button key={status} className="btn secondary" onClick={() => bulk.mutate({ action: "status", status })}>
              {t("vocabulary.mark_as", { status: t(`status.${status}`) })}
            </button>
          ))}
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "trash" })}>
            {t("vocabulary.trash")}
          </button>
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "restore" })}>
            {t("vocabulary.restore")}
          </button>
          <BulkTagPicker
            options={(tags.data?.items || []).map((tag) => ({ id: tag.id, name: tag.name }))}
            onPick={(id, remove) => bulk.mutate({ action: remove ? "remove_tag" : "add_tag", tag_id: id })}
          />
          {/* Its own picker, not the filter: the filter already restricts the list to one
              level, so moving the selection *to* a level could never be expressed with it. */}
          <div className="row" style={{ gap: 4 }}>
            <select
              className="input"
              style={{ maxWidth: 110 }}
              value={bulkLevel}
              onChange={(e) => setBulkLevel(e.target.value)}
              aria-label={t("vocabulary.level")}
            >
              <option value="">{t("vocabulary.choose_level")}</option>
              {(meta.data?.levels || []).map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
            <button
              className="btn secondary"
              disabled={!bulkLevel}
              onClick={() => bulk.mutate({ action: "set_level", level: bulkLevel })}
            >
              {t("vocabulary.set_level", { level: bulkLevel })}
            </button>
            {/* The same action with no level is the documented way to unset one. */}
            <button className="btn secondary" onClick={() => bulk.mutate({ action: "set_level", level: "" })}>
              {t("vocabulary.clear_level")}
            </button>
          </div>
        </div>
      ) : null}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th style={{ width: 28 }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={() => setSelected(allSelected ? [] : rows.map((row) => row.id))}
                />
              </th>
              <th>{t("vocabulary.word")}</th>
              <th>{t("vocabulary.level")}</th>
              <th>{t("vocabulary.part_of_speech")}</th>
              <th>{t("vocabulary.meanings_in")}</th>
              <th>{t("vocabulary.filed_under")}</th>
              <th>{t("vocabulary.status")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <VocabularyRow
                key={row.id}
                row={row}
                selected={selected.includes(row.id)}
                onToggle={(checked) =>
                  setSelected((prev) => (checked ? [...prev, row.id] : prev.filter((id) => id !== row.id)))
                }
                onAction={(action, status) => rowAction.mutate({ id: row.id, action, status })}
              />
            ))}
            {!list.isLoading && rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="muted">
                  {filters.view === "bank" ? t("vocabulary.empty") : t("vocabulary.empty_view")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
        <span className="small muted">{t("vocabulary.showing", { total })}</span>
        <span className="spacer" />
        <button className="btn secondary" disabled={filters.page <= 1} onClick={() => setFilter({ page: filters.page - 1 })}>
          ‹
        </button>
        <span className="small">
          {filters.page} / {pages}
        </span>
        <button className="btn secondary" disabled={filters.page >= pages} onClick={() => setFilter({ page: filters.page + 1 })}>
          ›
        </button>
      </div>
    </div>
  );
}

function VocabularyRow({
  row,
  selected,
  onToggle,
  onAction,
}: {
  row: VocabularySummary;
  selected: boolean;
  onToggle: (checked: boolean) => void;
  onAction: (action: "trash" | "restore" | "status", status?: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <tr>
      <td>
        <input type="checkbox" checked={selected} onChange={(e) => onToggle(e.target.checked)} />
      </td>
      <td>
        <Link to={`/vocabulary/${row.id}`}>{row.word}</Link>
        {row.ipa ? <span className="small muted"> {row.ipa}</span> : null}
        {row.has_audio ? <span className="chip">{t("vocabulary.has_audio")}</span> : null}
        {row.has_source ? <span className="chip">{t("vocabulary.has_source")}</span> : null}
        <div className="small muted">{row.definition || ""}</div>
      </td>
      <td className="small">{row.level || "—"}</td>
      <td className="small">{row.part_of_speech || "—"}</td>
      <td className="small">
        {row.translation_languages.join(", ") || "—"}
        {row.example_count ? <span className="small muted"> · {t("vocabulary.examples_n", { n: row.example_count })}</span> : null}
      </td>
      <td className="small muted">{row.tag_names.join(", ") || "—"}</td>
      <td className="small">{t(`status.${row.status}`)}</td>
      <td>
        <div className="row" style={{ gap: 2 }}>
          {row.deleted_at ? (
            <button className="btn ghost" onClick={() => onAction("restore")}>
              {t("vocabulary.restore")}
            </button>
          ) : (
            <>
              {row.status !== "ready" ? (
                <button className="btn ghost" onClick={() => onAction("status", "ready")}>
                  {t("vocabulary.publish")}
                </button>
              ) : (
                <button className="btn ghost" onClick={() => onAction("status", "archived")}>
                  {t("vocabulary.archive")}
                </button>
              )}
              <button className="btn ghost" onClick={() => onAction("trash")}>
                {t("vocabulary.trash")}
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

function BulkTagPicker({
  options,
  onPick,
}: {
  options: { id: string; name: string }[];
  onPick: (id: string, remove: boolean) => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  if (options.length === 0) return null;
  return (
    <span className="row" style={{ gap: 4 }}>
      <select className="input" style={{ maxWidth: 160 }} value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{t("vocabulary.all_tags")}</option>
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.name}
          </option>
        ))}
      </select>
      <button className="btn secondary" disabled={!value} onClick={() => onPick(value, false)}>
        +
      </button>
      <button className="btn secondary" disabled={!value} onClick={() => onPick(value, true)}>
        −
      </button>
    </span>
  );
}
