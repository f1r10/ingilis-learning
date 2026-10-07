// The teacher's recordings: what has a file, what has words, and what is ready to play.
//
// `has_audio` and `audio_state` come from the server because they are the two things a
// title cannot tell you: a listening whose file was thrown out of the library looks
// exactly like one that is ready to play until somebody presses play.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { listeningApi, type BulkResult, type ListeningSummary } from "../api/listening";

const PAGE_SIZE = 25;

export default function Listenings() {
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
      has_audio: params.get("has_audio") || "",
      show_transcript: params.get("show_transcript") || "",
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

  const meta = useQuery({ queryKey: ["listening-meta"], queryFn: listeningApi.meta });
  const list = useQuery({ queryKey: ["listening", filters], queryFn: () => listeningApi.list(filters) });

  useEffect(() => {
    setSelected([]);
    setBulkResult(null);
  }, [params]);

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const allSelected = rows.length > 0 && selected.length === rows.length;
  const nameOf = (id: string) => rows.find((row) => row.id === id)?.title || id.slice(0, 8);

  const refresh = () => qc.invalidateQueries({ queryKey: ["listening"] });

  const bulk = useMutation({
    mutationFn: (body: { action: string; status?: string; level?: string }) =>
      listeningApi.bulk({ passage_ids: selected, ...body }),
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
      if (action === "trash") return listeningApi.trash(id);
      if (action === "restore") return listeningApi.restore(id);
      return listeningApi.setStatus(id, status || "ready");
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
        <h1 style={{ margin: 0 }}>{t("listening.title")}</h1>
        <span className="spacer" />
        <Link className="btn" to="/listening/new">
          {t("listening.new")}
        </Link>
      </div>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("listening.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 140 }} value={filters.status} onChange={(e) => setFilter({ status: e.target.value })}>
          <option value="">{t("listening.all_statuses")}</option>
          {(meta.data?.statuses || []).map((status) => (
            <option key={status} value={status}>
              {t(`status.${status}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 110 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          <option value="">{t("listening.all_levels")}</option>
          {(meta.data?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.language} onChange={(e) => setFilter({ language: e.target.value })}>
          <option value="">{t("listening.all_languages")}</option>
          {(meta.data?.learning_languages || []).map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.has_audio} onChange={(e) => setFilter({ has_audio: e.target.value })}>
          <option value="">{t("listening.any_audio")}</option>
          <option value="true">{t("listening.with_audio")}</option>
          <option value="false">{t("listening.without_audio")}</option>
        </select>
        <select
          className="input"
          style={{ maxWidth: 170 }}
          value={filters.show_transcript}
          onChange={(e) => setFilter({ show_transcript: e.target.value })}
        >
          <option value="">{t("listening.any_transcript")}</option>
          <option value="true">{t("listening.transcript_shown")}</option>
          <option value="false">{t("listening.transcript_hidden")}</option>
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          <option value="bank">{t("listening.view_bank")}</option>
          <option value="trash">{t("listening.view_trash")}</option>
          <option value="all">{t("listening.view_all")}</option>
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {(meta.data?.sortable || []).map((sort) => (
            <option key={sort} value={sort}>
              {t(`listening.sort_${sort}`)}
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
            {t("listening.bulk_result", {
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
          <strong className="small">{t("listening.selected", { n: selected.length })}</strong>
          {(meta.data?.statuses || []).map((status) => (
            <button key={status} className="btn secondary" onClick={() => bulk.mutate({ action: "status", status })}>
              {t("listening.mark_as", { status: t(`status.${status}`) })}
            </button>
          ))}
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "trash" })}>
            {t("listening.trash")}
          </button>
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "restore" })}>
            {t("listening.restore")}
          </button>
          <div className="row" style={{ gap: 4 }}>
            <select
              className="input"
              style={{ maxWidth: 110 }}
              value={bulkLevel}
              onChange={(e) => setBulkLevel(e.target.value)}
              aria-label={t("listening.level")}
            >
              <option value="">{t("listening.choose_level")}</option>
              {(meta.data?.levels || []).map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
            <button className="btn secondary" disabled={!bulkLevel} onClick={() => bulk.mutate({ action: "set_level", level: bulkLevel })}>
              {t("listening.set_level", { level: bulkLevel })}
            </button>
            <button className="btn secondary" onClick={() => bulk.mutate({ action: "set_level", level: "" })}>
              {t("listening.clear_level")}
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
              <th>{t("listening.recording")}</th>
              <th>{t("listening.level")}</th>
              <th>{t("listening.audio")}</th>
              <th>{t("listening.transcript")}</th>
              <th>{t("listening.blocks")}</th>
              <th>{t("listening.status")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <ListeningRow
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
                  {filters.view === "bank" ? t("listening.empty") : t("listening.empty_view")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
        <span className="small muted">{t("listening.showing", { total })}</span>
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

function ListeningRow({
  row,
  selected,
  onToggle,
  onAction,
}: {
  row: ListeningSummary;
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
        <Link to={`/listening/${row.id}`}>{row.title}</Link>
        <div className="small muted">{row.language || "—"}</div>
      </td>
      <td className="small">{row.level || "—"}</td>
      <td className="small">
        {row.has_audio ? (
          row.duration_seconds ? (
            <span>{t("listening.seconds", { n: Math.round(row.duration_seconds) })}</span>
          ) : (
            <span className="chip">{t("listening.has_audio")}</span>
          )
        ) : (
          <span className="chip">{t("listening.no_audio")}</span>
        )}
      </td>
      <td className="small">
        {row.has_transcript ? t(`listening.source_${row.transcript_source}`) : "—"}
        {row.show_transcript ? <span className="muted small"> · {t("listening.transcript_shown")}</span> : null}
      </td>
      <td className="small">{t("listening.blocks_n", { sets: row.set_count, questions: row.question_count })}</td>
      <td className="small">{t(`status.${row.status}`)}</td>
      <td>
        <div className="row" style={{ gap: 2 }}>
          {row.deleted_at ? (
            <button className="btn ghost" onClick={() => onAction("restore")}>
              {t("listening.restore")}
            </button>
          ) : (
            <>
              {row.status !== "ready" ? (
                <button className="btn ghost" onClick={() => onAction("status", "ready")}>
                  {t("listening.publish")}
                </button>
              ) : (
                <button className="btn ghost" onClick={() => onAction("status", "archived")}>
                  {t("listening.archive")}
                </button>
              )}
              <button className="btn ghost" onClick={() => onAction("trash")}>
                {t("listening.trash")}
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}
