// The teacher's reading library: find a text, move it through its lifecycle, and work
// through a whole selection at once.
//
// The list answers per row because "3 of 40 published" is not something a teacher can act
// on: the refusals are listed with the reason the server gave. Nothing here counts words -
// `word_count` comes back from the server, and an editor that typed its own number would
// disagree with the text after the next keystroke.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { readingApi, type BulkResult, type ReadingSummary } from "../api/reading";

const PAGE_SIZE = 25;

export default function Reading() {
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
      language: params.get("language") || "",
      level: params.get("level") || "",
      status: params.get("status") || "",
      layout: params.get("layout") || "",
      view: params.get("view") || "bank",
      sort: params.get("sort") || "updated_at",
      order: params.get("order") || "desc",
      page: Number(params.get("page") || 1),
      page_size: PAGE_SIZE,
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

  const meta = useQuery({ queryKey: ["reading-meta"], queryFn: readingApi.meta });
  const list = useQuery({ queryKey: ["reading", filters], queryFn: () => readingApi.list(filters) });

  useEffect(() => {
    setSelected([]);
    setBulkResult(null);
  }, [params]);

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const allSelected = rows.length > 0 && selected.length === rows.length;
  const nameOf = (id: string) => rows.find((row) => row.id === id)?.title || id.slice(0, 8);

  const refresh = () => qc.invalidateQueries({ queryKey: ["reading"] });

  const bulk = useMutation({
    mutationFn: (body: { action: string; status?: string; level?: string }) =>
      readingApi.bulk({ passage_ids: selected, ...body }),
    onSuccess: (result) => {
      setBulkResult(result);
      setMessage(null);
      setSelected([]);
      refresh();
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const rowAction = useMutation({
    mutationFn: async ({ id, action, status }: { id: string; action: "trash" | "restore" | "status"; status?: string }) => {
      if (action === "trash") return readingApi.trash(id);
      if (action === "restore") return readingApi.restore(id);
      return readingApi.setStatus(id, status || "ready");
    },
    onSuccess: () => {
      setMessage(null);
      refresh();
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("reading.title")}</h1>
        <span className="spacer" />
        <Link className="btn" to="/reading/new">
          {t("reading.new")}
        </Link>
      </div>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("reading.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 140 }} value={filters.status} onChange={(e) => setFilter({ status: e.target.value })}>
          <option value="">{t("reading.all_statuses")}</option>
          {(meta.data?.statuses || []).map((status) => (
            <option key={status} value={status}>
              {t(`status.${status}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 110 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          <option value="">{t("reading.all_levels")}</option>
          {(meta.data?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.language} onChange={(e) => setFilter({ language: e.target.value })}>
          <option value="">{t("reading.all_languages")}</option>
          {(meta.data?.learning_languages || []).map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.layout} onChange={(e) => setFilter({ layout: e.target.value })}>
          <option value="">{t("reading.all_layouts")}</option>
          {(meta.data?.layouts || []).map((layout) => (
            <option key={layout} value={layout}>
              {t(`reading.layout_${layout}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          <option value="bank">{t("reading.view_bank")}</option>
          <option value="trash">{t("reading.view_trash")}</option>
          <option value="all">{t("reading.view_all")}</option>
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {(meta.data?.sortable || []).map((sort) => (
            <option key={sort} value={sort}>
              {t(`reading.sort_${sort}`)}
            </option>
          ))}
        </select>
        <button type="button" className="btn secondary" onClick={() => setFilter({ order: filters.order === "desc" ? "asc" : "desc" })}>
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
            {t("reading.bulk_result", {
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
          <strong className="small">{t("reading.selected", { n: selected.length })}</strong>
          {(meta.data?.statuses || []).map((status) => (
            <button key={status} className="btn secondary" onClick={() => bulk.mutate({ action: "status", status })}>
              {t("reading.mark_as", { status: t(`status.${status}`) })}
            </button>
          ))}
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "trash" })}>
            {t("reading.trash")}
          </button>
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "restore" })}>
            {t("reading.restore")}
          </button>
          <div className="row" style={{ gap: 4 }}>
            <select
              className="input"
              style={{ maxWidth: 110 }}
              value={bulkLevel}
              onChange={(e) => setBulkLevel(e.target.value)}
              aria-label={t("reading.level")}
            >
              <option value="">{t("reading.choose_level")}</option>
              {(meta.data?.levels || []).map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
            <button className="btn secondary" disabled={!bulkLevel} onClick={() => bulk.mutate({ action: "set_level", level: bulkLevel })}>
              {t("reading.set_level", { level: bulkLevel })}
            </button>
            <button className="btn secondary" onClick={() => bulk.mutate({ action: "set_level", level: "" })}>
              {t("reading.clear_level")}
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
              <th>{t("reading.text")}</th>
              <th>{t("reading.level")}</th>
              <th>{t("reading.layout")}</th>
              <th>{t("reading.words")}</th>
              <th>{t("reading.blocks")}</th>
              <th>{t("reading.status")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <ReadingRow
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
                  {filters.view === "bank" ? t("reading.empty") : t("reading.empty_view")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
        <span className="small muted">{t("reading.showing", { total })}</span>
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

function ReadingRow({
  row,
  selected,
  onToggle,
  onAction,
}: {
  row: ReadingSummary;
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
        <Link to={`/reading/${row.id}`}>{row.title}</Link>
        <div className="small muted">{row.excerpt}</div>
        <div className="small muted">{row.language || "—"}</div>
      </td>
      <td className="small">{row.level || "—"}</td>
      <td className="small">{t(`reading.layout_${row.layout}`)}</td>
      <td className="small">{row.word_count ?? "—"}</td>
      <td className="small">{t("reading.blocks_n", { sets: row.set_count, questions: row.question_count })}</td>
      <td className="small">{t(`status.${row.status}`)}</td>
      <td>
        <div className="row" style={{ gap: 2 }}>
          {row.deleted_at ? (
            <button className="btn ghost" onClick={() => onAction("restore")}>
              {t("reading.restore")}
            </button>
          ) : (
            <>
              {row.status !== "ready" ? (
                <button className="btn ghost" onClick={() => onAction("status", "ready")}>
                  {t("reading.publish")}
                </button>
              ) : (
                <button className="btn ghost" onClick={() => onAction("status", "archived")}>
                  {t("reading.archive")}
                </button>
              )}
              <button className="btn ghost" onClick={() => onAction("trash")}>
                {t("reading.trash")}
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}
