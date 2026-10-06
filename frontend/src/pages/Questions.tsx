// The question bank: one list that can be filtered, sorted, paginated and worked
// on in bulk, plus the trash the teacher can restore from.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  LEVELS,
  QUESTION_STATUSES,
  questionsApi,
  tagsApi,
  topicsApi,
  type BulkResult,
  type QuestionSummary,
  type TopicNode,
} from "../api/questions";
import { ApiError } from "../api/client";

const SORTS = ["updated_at", "created_at", "type", "level", "difficulty", "prompt", "status"];

function flatten(nodes: TopicNode[], depth = 0): { node: TopicNode; depth: number }[] {
  return nodes.flatMap((node) => [{ node, depth }, ...flatten(node.children, depth + 1)]);
}

export default function Questions() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [bulkResult, setBulkResult] = useState<BulkResult | null>(null);

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      type: params.get("type") || "",
      status: params.get("status") || "",
      level: params.get("level") || "",
      learning_language: params.get("learning_language") || "",
      topic_id: params.get("topic_id") || "",
      tag_id: params.get("tag_id") || "",
      view: params.get("view") || "bank",
      sort: params.get("sort") || "updated_at",
      order: params.get("order") || "desc",
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

  const types = useQuery({ queryKey: ["question-types"], queryFn: questionsApi.types });
  const topics = useQuery({ queryKey: ["topics"], queryFn: topicsApi.list });
  const tags = useQuery({ queryKey: ["tags"], queryFn: tagsApi.list });
  const list = useQuery({
    queryKey: ["questions", filters],
    queryFn: () => questionsApi.list(filters),
  });

  useEffect(() => {
    setSelected([]);
  }, [params]);

  const tree = useMemo(() => flatten(topics.data?.items || []), [topics.data]);

  const bulk = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      questionsApi.bulk({ question_ids: selected, ...(body as object) } as Parameters<typeof questionsApi.bulk>[0]),
    onSuccess: (result) => {
      setBulkResult(result);
      setMessage(null);
      setSelected([]);
      qc.invalidateQueries({ queryKey: ["questions"] });
      qc.invalidateQueries({ queryKey: ["topics"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const rowAction = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "trash" | "restore" | "clone" | "status" }) => {
      if (action === "trash") return questionsApi.trash(id);
      if (action === "restore") return questionsApi.restore(id);
      if (action === "clone") return questionsApi.clone(id);
      return questionsApi.setStatus(id, "ready");
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["questions"] });
      qc.invalidateQueries({ queryKey: ["question-types"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const rows = list.data?.items || [];
  const nameOf = (id: string) => rows.find((row) => row.id === id)?.prompt || id.slice(0, 8);
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / filters.page_size));
  const allSelected = rows.length > 0 && selected.length === rows.length;

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("questions.title")}</h1>
        <span className="spacer" />
        <Link className="btn" to="/questions/new">
          {t("questions.new")}
        </Link>
      </div>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("questions.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 190 }} value={filters.type} onChange={(e) => setFilter({ type: e.target.value })}>
          <option value="">{t("questions.all_types")}</option>
          {(types.data?.items || []).map((spec) => (
            <option key={spec.type} value={spec.type}>
              {spec.label}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.status} onChange={(e) => setFilter({ status: e.target.value })}>
          <option value="">{t("questions.all_statuses")}</option>
          {QUESTION_STATUSES.map((status) => (
            <option key={status} value={status}>
              {t(`status.${status}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 120 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          <option value="">{t("questions.all_levels")}</option>
          {LEVELS.map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 200 }} value={filters.topic_id} onChange={(e) => setFilter({ topic_id: e.target.value })}>
          <option value="">{t("questions.all_topics")}</option>
          {tree.map(({ node, depth }) => (
            <option key={node.id} value={node.id}>
              {"—".repeat(depth)} {node.name}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 160 }} value={filters.tag_id} onChange={(e) => setFilter({ tag_id: e.target.value })}>
          <option value="">{t("questions.all_tags")}</option>
          {(tags.data?.items || []).map((tag) => (
            <option key={tag.id} value={tag.id}>
              {tag.name}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          <option value="bank">{t("questions.view_bank")}</option>
          <option value="trash">{t("questions.view_trash")}</option>
          <option value="all">{t("questions.view_all")}</option>
        </select>
        <select className="input" style={{ maxWidth: 160 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {SORTS.map((sort) => (
            <option key={sort} value={sort}>
              {t(`questions.sort_${sort}`)}
            </option>
          ))}
        </select>
        <button type="button" className="btn secondary" onClick={() => setFilter({ order: filters.order === "desc" ? "asc" : "desc" })}>
          {filters.order === "desc" ? "↓" : "↑"}
        </button>
      </div>

      {message ? <div className="alert error" style={{ color: "var(--text)" }}>{message}</div> : null}

      {list.isError ? (
        <div className="alert error" style={{ color: "var(--text)" }}>
          {t("common.could_not_load")} {(list.error as ApiError).message}
        </div>
      ) : null}

      {bulkResult ? (
        <div className="card stack">
          <div className="small">
            {t("questions.bulk_result", {
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
          <strong className="small">{t("questions.selected", { n: selected.length })}</strong>
          {QUESTION_STATUSES.map((status) => (
            <button key={status} className="btn secondary" onClick={() => bulk.mutate({ action: "status", status })}>
              {t("questions.mark_as", { status: t(`status.${status}`) })}
            </button>
          ))}
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "trash" })}>
            {t("questions.trash")}
          </button>
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "restore" })}>
            {t("questions.restore")}
          </button>
          <BulkTaxonomy kind="topic" options={(topics.data?.items || []).flatMap((node) => flatten([node])).map(({ node }) => ({ id: node.id, name: node.name }))} onPick={(id, remove) => bulk.mutate({ action: remove ? "remove_topic" : "add_topic", topic_id: id })} />
          <BulkTaxonomy kind="tag" options={(tags.data?.items || []).map((tag) => ({ id: tag.id, name: tag.name }))} onPick={(id, remove) => bulk.mutate({ action: remove ? "remove_tag" : "add_tag", tag_id: id })} />
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "set_level", level: filters.level })} disabled={!filters.level}>
            {t("questions.set_level", { level: filters.level })}
          </button>
          <button className="btn secondary" onClick={() => bulk.mutate({ action: "set_language", learning_language: filters.learning_language })} disabled={!filters.learning_language}>
            {t("questions.set_language")}
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
              <th>{t("questions.prompt")}</th>
              <th>{t("questions.type")}</th>
              <th>{t("questions.level")}</th>
              <th>{t("questions.filed_under")}</th>
              <th>{t("questions.status")}</th>
              <th>{t("questions.version")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <QuestionRow key={row.id} row={row} selected={selected.includes(row.id)} onToggle={(checked) => setSelected((prev) => (checked ? [...prev, row.id] : prev.filter((id) => id !== row.id)))} onAction={(action) => rowAction.mutate({ id: row.id, action })} />
            ))}
            {!list.isLoading && rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="muted">
                  {t("questions.empty")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
        <span className="small muted">{t("questions.showing", { total })}</span>
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

function QuestionRow({
  row,
  selected,
  onToggle,
  onAction,
}: {
  row: QuestionSummary;
  selected: boolean;
  onToggle: (checked: boolean) => void;
  onAction: (action: "trash" | "restore" | "clone" | "status") => void;
}) {
  const { t } = useTranslation();
  return (
    <tr>
      <td>
        <input type="checkbox" checked={selected} onChange={(e) => onToggle(e.target.checked)} />
      </td>
      <td>
        <Link to={`/questions/${row.id}`}>{row.prompt || t("questions.no_prompt")}</Link>
        {row.context_kind !== "independent" ? (
          <span className="chip">{t(`questions.context_${row.context_kind}`)}</span>
        ) : null}
        {row.has_media ? <span className="chip">{t("questions.has_media")}</span> : null}
      </td>
      <td className="small">{row.type}</td>
      <td className="small">{row.level || "—"}</td>
      <td className="small muted">{[...row.topic_names, ...row.tag_names].join(", ") || "—"}</td>
      <td className="small">{t(`status.${row.status}`)}</td>
      <td className="small">v{row.current_version}</td>
      <td>
        <div className="row" style={{ gap: 2 }}>
          {row.deleted_at ? (
            <button className="btn ghost" onClick={() => onAction("restore")}>
              {t("questions.restore")}
            </button>
          ) : (
            <>
              <button className="btn ghost" onClick={() => onAction("status")} title={t("questions.publish_hint")}>
                {t("questions.publish")}
              </button>
              <button className="btn ghost" onClick={() => onAction("clone")}>
                {t("questions.clone")}
              </button>
              <button className="btn ghost" onClick={() => onAction("trash")}>
                {t("questions.trash")}
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}

function BulkTaxonomy({
  kind,
  options,
  onPick,
}: {
  kind: "topic" | "tag";
  options: { id: string; name: string }[];
  onPick: (id: string, remove: boolean) => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  if (options.length === 0) return null;
  return (
    <span className="row" style={{ gap: 4 }}>
      <select className="input" style={{ maxWidth: 160 }} value={value} onChange={(e) => setValue(e.target.value)}>
        <option value="">{t(kind === "topic" ? "questions.all_topics" : "questions.all_tags")}</option>
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
