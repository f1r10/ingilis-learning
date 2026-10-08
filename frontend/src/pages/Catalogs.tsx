// The teacher's practice collections: find one, move it through its lifecycle, and work
// through a whole selection at once.
//
// A catalog is a folder as well as a list, so the screen shows the folder it sits in and
// how many folders sit inside it - a teacher who cannot see that a unit holds four lessons
// cannot tell an empty collection from an unopened one. The bulk toolbar answers per row
// because "3 of 40 published" is not something anyone can act on: the refusals are listed
// with the reason the server gave, and trashing a unit together with the lessons inside it
// is one click rather than a sequence that fails halfway.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { catalogsApi, type BulkResult, type CatalogSummary } from "../api/catalogs";

const PAGE_SIZE = 25;

export default function Catalogs() {
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
      kind: params.get("kind") || "",
      language: params.get("language") || "",
      level: params.get("level") || "",
      status: params.get("status") || "",
      view: params.get("view") || "bank",
      root_only: params.get("root_only") === "1",
      parent_id: params.get("parent_id") || "",
      sort: params.get("sort") || "updated_at",
      order: params.get("order") || "desc",
      page: Number(params.get("page") || 1),
      page_size: PAGE_SIZE,
    }),
    [params],
  );

  const setFilter = (patch: Record<string, string | number | boolean>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => {
      if (value === "" || value === undefined || value === false) next.delete(key);
      else next.set(key, value === true ? "1" : String(value));
    });
    if (!("page" in patch)) next.delete("page");
    setParams(next);
  };

  const meta = useQuery({ queryKey: ["catalogs-meta"], queryFn: catalogsApi.meta });
  const list = useQuery({ queryKey: ["catalogs", filters], queryFn: () => catalogsApi.list(filters) });

  useEffect(() => {
    setSelected([]);
    setBulkResult(null);
  }, [params]);

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const allSelected = rows.length > 0 && selected.length === rows.length;
  const nameOf = (id: string) => rows.find((row) => row.id === id)?.name || id.slice(0, 8);
  const kindLabel = (kind: string) => t(`catalogs.kind_${kind}`);

  const refresh = () => qc.invalidateQueries({ queryKey: ["catalogs"] });

  const bulk = useMutation({
    mutationFn: (body: { action: string; status?: string; level?: string }) =>
      catalogsApi.bulk({ catalog_ids: selected, ...body }),
    onSuccess: (result) => {
      setBulkResult(result);
      setMessage(null);
      setSelected([]);
      refresh();
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const rowAction = useMutation({
    mutationFn: async ({
      id,
      action,
      status,
    }: {
      id: string;
      action: "trash" | "restore" | "status";
      status?: string;
    }) => {
      if (action === "trash") return catalogsApi.trash(id);
      if (action === "restore") return catalogsApi.restore(id);
      return catalogsApi.setStatus(id, status || "ready");
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
        <h1 style={{ margin: 0 }}>{t("catalogs.title")}</h1>
        <span className="spacer" />
        <Link className="btn secondary" to="/catalogs/new?folder=1">
          {t("catalogs.new_folder")}
        </Link>
        <Link className="btn" to="/catalogs/new">
          {t("catalogs.new")}
        </Link>
      </div>
      <p className="muted small">{t("catalogs.hint")}</p>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("catalogs.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 140 }} value={filters.status} onChange={(e) => setFilter({ status: e.target.value })}>
          <option value="">{t("catalogs.all_statuses")}</option>
          {(meta.data?.statuses || []).map((status) => (
            <option key={status} value={status}>
              {t(`status.${status}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.kind} onChange={(e) => setFilter({ kind: e.target.value })}>
          <option value="">{t("catalogs.all_kinds")}</option>
          {(meta.data?.item_kinds || []).map((entry) => (
            <option key={entry.kind} value={entry.kind}>
              {t(`catalogs.kind_${entry.kind}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 110 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          <option value="">{t("catalogs.all_levels")}</option>
          {(meta.data?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.language} onChange={(e) => setFilter({ language: e.target.value })}>
          <option value="">{t("catalogs.all_languages")}</option>
          {(meta.data?.learning_languages || []).map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          <option value="bank">{t("catalogs.view_bank")}</option>
          <option value="trash">{t("catalogs.view_trash")}</option>
          <option value="all">{t("catalogs.view_all")}</option>
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {(meta.data?.sortable || []).map((sort) => (
            <option key={sort} value={sort}>
              {t(`catalogs.sort_${sort}`)}
            </option>
          ))}
        </select>
        <button type="button" className="btn secondary" onClick={() => setFilter({ order: filters.order === "desc" ? "asc" : "desc" })}>
          {filters.order === "desc" ? "↓" : "↑"}
        </button>
        <button
          type="button"
          className="btn secondary"
          aria-pressed={filters.root_only}
          onClick={() => setFilter({ root_only: !filters.root_only })}
        >
          {t("catalogs.folders_only")}
        </button>
      </div>

      {filters.parent_id ? (
        <div className="row small">
          <button type="button" className="btn ghost" onClick={() => setFilter({ parent_id: "" })}>
            ‹ {t("catalogs.all_collections")}
          </button>
          <span className="muted">
            {t("catalogs.inside")}: {rows[0]?.parent_name || filters.parent_id.slice(0, 8)}
          </span>
        </div>
      ) : null}

      {message ? <div className="alert error">{message}</div> : null}
      {list.isError ? (
        <div className="alert error">
          {t("common.could_not_load")} {(list.error as ApiError).message}
        </div>
      ) : null}

      {bulkResult ? (
        <div className="card stack">
          <div className="small">
            {t("catalogs.bulk_result", {
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
          <strong className="small">{t("catalogs.selected", { n: selected.length })}</strong>
          {(meta.data?.statuses || []).map((status) => (
            <button key={status} className="btn secondary" onClick={() => bulk.mutate({ action: "status", status })}>
              {t("catalogs.mark_as", { status: t(`status.${status}`) })}
            </button>
          ))}
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "trash" })}>
            {t("catalogs.trash")}
          </button>
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "restore" })}>
            {t("catalogs.restore")}
          </button>
          <div className="row" style={{ gap: 4 }}>
            <select
              className="input"
              style={{ maxWidth: 110 }}
              value={bulkLevel}
              onChange={(e) => setBulkLevel(e.target.value)}
              aria-label={t("catalogs.level")}
            >
              <option value="">{t("catalogs.choose_level")}</option>
              {(meta.data?.levels || []).map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
            <button className="btn secondary" disabled={!bulkLevel} onClick={() => bulk.mutate({ action: "set_level", level: bulkLevel })}>
              {t("catalogs.set_level", { level: bulkLevel })}
            </button>
            <button className="btn secondary" onClick={() => bulk.mutate({ action: "set_level", level: "" })}>
              {t("catalogs.clear_level")}
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
              <th>{t("catalogs.name")}</th>
              <th>{t("catalogs.level")}</th>
              <th>{t("catalogs.contents")}</th>
              <th>{t("catalogs.folders")}</th>
              <th>{t("catalogs.status")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <CatalogRow
                key={row.id}
                row={row}
                kindLabel={kindLabel}
                selected={selected.includes(row.id)}
                onToggle={(checked) =>
                  setSelected((prev) => (checked ? [...prev, row.id] : prev.filter((id) => id !== row.id)))
                }
                onAction={(action, status) => rowAction.mutate({ id: row.id, action, status })}
                onOpenFolder={() => setFilter({ parent_id: row.id, root_only: false })}
              />
            ))}
            {!list.isLoading && rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="muted">
                  {filters.view === "bank" ? t("catalogs.empty") : t("catalogs.empty_view")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
        <span className="small muted">{t("catalogs.showing", { total })}</span>
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

function CatalogRow({
  row,
  kindLabel,
  selected,
  onToggle,
  onAction,
  onOpenFolder,
}: {
  row: CatalogSummary;
  kindLabel: (kind: string) => string;
  selected: boolean;
  onToggle: (checked: boolean) => void;
  onAction: (action: "trash" | "restore" | "status", status?: string) => void;
  onOpenFolder: () => void;
}) {
  const { t } = useTranslation();
  const counts = Object.entries(row.counts || {})
    .filter(([, n]) => n > 0)
    .map(([kind, n]) => `${n} ${kindLabel(kind)}`)
    .join(" · ");

  return (
    <tr>
      <td>
        <input type="checkbox" checked={selected} onChange={(e) => onToggle(e.target.checked)} />
      </td>
      <td>
        <Link to={`/catalogs/${row.id}`}>{row.name}</Link>
        {row.description ? <div className="small muted">{row.description}</div> : null}
        <div className="small muted">
          {row.parent_name ? `${t("catalogs.inside")}: ${row.parent_name}` : t("catalogs.at_top_level")}
          {row.learning_language ? ` · ${row.learning_language}` : ""}
        </div>
      </td>
      <td className="small">{row.level || "—"}</td>
      <td className="small">
        {row.item_count ? counts || t("catalogs.items_n", { n: row.item_count }) : t("catalogs.no_items")}
        {row.unavailable_count ? (
          <div className="muted">{t("catalogs.unavailable_n", { n: row.unavailable_count })}</div>
        ) : null}
      </td>
      <td className="small">
        {row.child_count ? (
          <button type="button" className="btn ghost" onClick={onOpenFolder}>
            {t("catalogs.open_n", { n: row.child_count })}
          </button>
        ) : (
          "—"
        )}
      </td>
      <td className="small">{t(`status.${row.status}`)}</td>
      <td>
        <div className="row" style={{ gap: 2 }}>
          {row.deleted_at ? (
            <button className="btn ghost" onClick={() => onAction("restore")}>
              {t("catalogs.restore")}
            </button>
          ) : (
            <>
              {row.status !== "ready" ? (
                <button className="btn ghost" onClick={() => onAction("status", "ready")}>
                  {t("catalogs.publish")}
                </button>
              ) : (
                <button className="btn ghost" onClick={() => onAction("status", "archived")}>
                  {t("catalogs.archive")}
                </button>
              )}
              <button className="btn ghost" onClick={() => onAction("trash")}>
                {t("catalogs.trash")}
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}
