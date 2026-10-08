// The teacher's papers: find one, move it through its lifecycle, and work through a selection.
//
// A paper is not a collection, and the list says so: it reports who was handed the exam, how
// many have sat it, and how many answers are still waiting for a mark - numbers a practice
// shelf has no use for. Nothing here edits the questions themselves; the editor pins them to a
// version and this screen only decides whether the paper is open.
//
// The counts are read from the server rather than derived. `review_count` in particular is the
// grading queue expressed as a number, so a teacher can see which paper still owes learners
// marks without opening four editors to find out.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { examsApi, type BulkResult, type ExamRow } from "../api/exams";
import { when } from "../i18n/format";

const PAGE_SIZE = 25;

export default function Exams() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [bulkResult, setBulkResult] = useState<BulkResult | null>(null);

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      status: params.get("status") || "",
      language: params.get("language") || "",
      level: params.get("level") || "",
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

  const meta = useQuery({ queryKey: ["exams-meta"], queryFn: examsApi.meta });
  const list = useQuery({ queryKey: ["exams", filters], queryFn: () => examsApi.list(filters) });

  useEffect(() => {
    setSelected([]);
    setBulkResult(null);
  }, [params]);

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const allSelected = rows.length > 0 && selected.length === rows.length;
  const nameOf = (id: string) => rows.find((row) => row.id === id)?.title || id.slice(0, 8);

  const refresh = () => qc.invalidateQueries({ queryKey: ["exams"] });

  const bulk = useMutation({
    mutationFn: (body: { action: string; status?: string }) =>
      examsApi.bulk({ exam_ids: selected, ...body }),
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
      action: "trash" | "restore" | "status" | "clone";
      status?: string;
    }) => {
      if (action === "trash") return examsApi.trash(id);
      if (action === "restore") return examsApi.restore(id);
      if (action === "clone") return examsApi.clone(id);
      return examsApi.setStatus(id, status || "draft");
    },
    onSuccess: () => {
      setMessage(null);
      refresh();
    },
    // A clone whose title is already taken answers 409 `exam_name_taken`, and the list did not
    // change: the teacher needs the sentence, not a table that silently looks the same.
    onError: (e: ApiError) => setMessage(e.message),
  });

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("exams.title")}</h1>
        <span className="spacer" />
        <Link className="btn secondary" to="/exams/grading">
          {t("exams.grading")}
        </Link>
        <Link className="btn" to="/exams/new">
          {t("exams.new")}
        </Link>
      </div>
      <p className="muted small">{t("exams.hint")}</p>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("exams.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 150 }} value={filters.status} onChange={(e) => setFilter({ status: e.target.value })}>
          <option value="">{t("exams.all_statuses")}</option>
          {(meta.data?.statuses || []).map((status) => (
            <option key={status} value={status}>
              {t(`status.${status}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 110 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          <option value="">{t("exams.all_levels")}</option>
          {(meta.data?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.language} onChange={(e) => setFilter({ language: e.target.value })}>
          <option value="">{t("exams.all_languages")}</option>
          {(meta.data?.languages || []).map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          {(meta.data?.views || ["bank", "trash", "all"]).map((view) => (
            <option key={view} value={view}>
              {t(`exams.view_${view}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {["updated_at", "created_at", "title", "available_from"].map((sort) => (
            <option key={sort} value={sort}>
              {t(`exams.sort_${sort}`)}
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
            {t("exams.bulk_result", {
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
          <strong className="small">{t("exams.selected", { n: selected.length })}</strong>
          {(meta.data?.statuses || []).map((status) => (
            <button key={status} type="button" className="btn secondary" onClick={() => bulk.mutate({ action: "status", status })}>
              {t("exams.mark_as", { status: t(`status.${status}`) })}
            </button>
          ))}
          <button type="button" className="btn secondary" onClick={() => bulk.mutate({ action: "trash" })}>
            {t("exams.trash")}
          </button>
          <button type="button" className="btn secondary" onClick={() => bulk.mutate({ action: "restore" })}>
            {t("exams.restore")}
          </button>
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
              <th>{t("exams.paper")}</th>
              <th>{t("exams.level")}</th>
              <th>{t("exams.contents")}</th>
              <th>{t("exams.audience")}</th>
              <th>{t("exams.sittings")}</th>
              <th>{t("exams.status")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <ExamRowView
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
                  {filters.view === "bank" ? t("exams.empty") : t("exams.empty_view")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
        <span className="small muted">{t("exams.showing", { total })}</span>
        <span className="spacer" />
        <button type="button" className="btn secondary" disabled={filters.page <= 1} onClick={() => setFilter({ page: filters.page - 1 })}>
          ‹
        </button>
        <span className="small">
          {filters.page} / {pages}
        </span>
        <button type="button" className="btn secondary" disabled={filters.page >= pages} onClick={() => setFilter({ page: filters.page + 1 })}>
          ›
        </button>
      </div>
    </div>
  );
}

function ExamRowView({
  row,
  selected,
  onToggle,
  onAction,
}: {
  row: ExamRow;
  selected: boolean;
  onToggle: (checked: boolean) => void;
  onAction: (action: "trash" | "restore" | "status" | "clone", status?: string) => void;
}) {
  const { t } = useTranslation();
  const window =
    row.available_from || row.available_to
      ? `${row.available_from ? when(row.available_from) : t("exams.anytime")} – ${
          row.available_to ? when(row.available_to) : t("exams.anytime")
        }`
      : null;

  return (
    <tr>
      <td>
        <input type="checkbox" checked={selected} onChange={(e) => onToggle(e.target.checked)} />
      </td>
      <td>
        <Link to={`/exams/${row.id}`}>{row.title}</Link>
        {row.duration_minutes ? (
          <div className="small muted">{t("exams.minutes_n", { n: row.duration_minutes })}</div>
        ) : (
          <div className="small muted">{t("exams.no_time_limit")}</div>
        )}
        {window ? <div className="small muted">{window}</div> : null}
        {row.learning_language ? <div className="small muted">{row.learning_language}</div> : null}
      </td>
      <td className="small">{row.level || "—"}</td>
      <td className="small">
        {row.item_count ? t("exams.questions_n", { n: row.item_count }) : t("exams.no_questions")}
        <div className="muted">{t("exams.points_n", { n: row.points })}</div>
      </td>
      <td className="small">
        {row.assigned_count ? t("exams.assigned_n", { n: row.assigned_count }) : t("exams.not_assigned")}
      </td>
      <td className="small">
        {row.attempt_count ? t("exams.sat_n", { sat: row.submitted_count, total: row.attempt_count }) : t("exams.not_sat")}
        {row.review_count ? (
          <div>
            {/* The number is the queue, so it opens the queue for this paper rather than
                describing a list the teacher then has to go and find. */}
            <Link className="small" to={`/exams/grading?exam_id=${row.id}`}>
              {t("exams.waiting_n", { n: row.review_count })}
            </Link>
          </div>
        ) : null}
      </td>
      <td className="small">{t(`status.${row.status}`)}</td>
      <td>
        <div className="row" style={{ gap: 2, flexWrap: "wrap" }}>
          {row.deleted_at ? (
            <button type="button" className="btn ghost" onClick={() => onAction("restore")}>
              {t("exams.restore")}
            </button>
          ) : (
            <>
              {row.status !== "active" ? (
                <button type="button" className="btn ghost" onClick={() => onAction("status", "active")}>
                  {t("exams.open_for_learners")}
                </button>
              ) : (
                <button type="button" className="btn ghost" onClick={() => onAction("status", "finished")}>
                  {t("exams.close")}
                </button>
              )}
              <button type="button" className="btn ghost" onClick={() => onAction("clone")}>
                {t("exams.make_copy")}
              </button>
              <button type="button" className="btn ghost" onClick={() => onAction("trash")}>
                {t("exams.trash")}
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}
