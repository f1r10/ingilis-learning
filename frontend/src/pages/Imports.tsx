// The teacher's documents: send a paper, watch the worker read it, open its queue.
//
// This screen is the front door of the importer and it owns one decision only - the bytes. The
// format is read from the file itself, so there is no parser selector and no content type to
// claim here; a screen that let a teacher say "this is a CSV" would be a way to store something
// behind a label the file does not support.
//
// The list is the worker's progress as a table. A job is read from the server rather than from
// anything this screen remembers, and while a paper is still being queued or read the list
// re-reads itself: there is no push channel, and a queue that looks frozen when it is running is
// worse than one that says it is asking again.
import { useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { importsApi, type ImportJob } from "../api/imports";
import { size, when, wordsFor } from "../i18n/format";

const PAGE_SIZE = 25;

/** The two statuses that mean the worker still has this job: everything else is settled. */
const IN_FLIGHT = new Set(["queued", "processing"]);

/** A queue can be empty for a reason the teacher can act on. A failure says its own sentence in
 * `job.error`, so nothing here guesses at a code it has no words for. */
const EMPTY_FINDINGS = new Set(["no_text", "no_text_layer"]);

export default function Imports() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [message, setMessage] = useState<string | null>(null);

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      status: params.get("status") || "",
      sort: params.get("sort") || "created_at",
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

  const meta = useQuery({ queryKey: ["imports-meta"], queryFn: importsApi.meta });
  const list = useQuery({
    queryKey: ["imports", filters],
    queryFn: () => importsApi.list(filters),
    // Only while a paper is actually being read, so an idle history costs nothing.
    refetchInterval: (query) =>
      (query.state.data?.items || []).some((job: ImportJob) => IN_FLIGHT.has(job.status)) ? 2_000 : false,
  });

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["imports"] });
  };

  const rowAction = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "retry" | "remove" }) => {
      if (action === "retry") return importsApi.retry(id);
      return importsApi.remove(id);
    },
    onSuccess: (_result, variables) => {
      setMessage(null);
      if (variables.action === "remove") setFilter({ page: 1 });
      refresh();
    },
    // A retry refused because the queue already has candidates, and a delete refused because the
    // bank still holds what it filed, are both rules about this paper - the list has not changed,
    // so the sentence is the only thing the teacher gets.
    onError: (e: ApiError) => setMessage(e.message),
  });

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("imports.title")}</h1>
      </div>
      <p className="muted small">{t("imports.hint")}</p>

      <UploadCard
        formats={meta.data?.formats || []}
        maxBytes={meta.data?.max_document_bytes}
        maxMb={meta.data?.max_document_mb}
        autoModeDefault={meta.data?.auto_mode_default ?? false}
        onOpened={(job, duplicate) => {
          setMessage(t(duplicate ? "imports.duplicate" : "imports.opened", { name: job.source?.title || job.id }));
          refresh();
        }}
        onError={(e) => setMessage(e.message)}
      />

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("imports.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select
          className="input"
          style={{ maxWidth: 170 }}
          value={filters.status}
          onChange={(e) => setFilter({ status: e.target.value })}
        >
          <option value="">{t("imports.all_statuses")}</option>
          {(meta.data?.job_statuses || []).map((status) => (
            <option key={status} value={status}>
              {t(`imports.job_status_${status}`)}
            </option>
          ))}
        </select>
        <select
          className="input"
          style={{ maxWidth: 170 }}
          value={filters.sort}
          onChange={(e) => setFilter({ sort: e.target.value })}
        >
          {(meta.data?.sortable_jobs || ["created_at"]).map((sort) => (
            <option key={sort} value={sort}>
              {t(`imports.sort_job_${sort}`)}
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
        <span className="spacer" />
        <button type="button" className="btn ghost" onClick={refresh}>
          {t("imports.refresh")}
        </button>
      </div>

      {message ? <div className="alert">{message}</div> : null}
      {list.isError ? (
        <div className="alert error">
          {t("common.could_not_load")} {(list.error as ApiError).message}
        </div>
      ) : null}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>{t("imports.document")}</th>
              <th>{t("imports.read")}</th>
              <th>{t("imports.queue")}</th>
              <th>{t("imports.status")}</th>
              <th>{t("imports.added")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                busy={rowAction.isPending}
                onAction={(action) => rowAction.mutate({ id: job.id, action })}
              />
            ))}
            {!list.isLoading && rows.length === 0 ? (
              <tr>
                <td colSpan={6} className="muted">
                  {t("imports.empty")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
        <span className="small muted">{t("imports.showing", { total })}</span>
        <span className="spacer" />
        <button
          type="button"
          className="btn secondary"
          disabled={filters.page <= 1}
          onClick={() => setFilter({ page: filters.page - 1 })}
        >
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

function UploadCard({
  formats,
  maxBytes,
  maxMb,
  autoModeDefault,
  onOpened,
  onError,
}: {
  formats: { name: string; extension: string; label: string }[];
  maxBytes?: number;
  maxMb?: number;
  autoModeDefault: boolean;
  onOpened: (job: ImportJob & { duplicate: boolean }, duplicate: boolean) => void;
  onError: (e: ApiError) => void;
}) {
  const { t, i18n } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  // The platform's default is the starting point, and the teacher may move off it for one paper:
  // auto mode is a request about this document, not a setting stored on the account.
  const [autoMode, setAutoMode] = useState<boolean | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);

  const accepted = formats.map((format) => `.${format.extension}`).join(",");
  const wantsAuto = autoMode ?? autoModeDefault;

  const upload = useMutation({
    mutationFn: () =>
      importsApi.upload(file as File, {
        title: title.trim() || undefined,
        autoMode: autoMode === null ? undefined : autoMode,
      }),
    onSuccess: (job) => {
      setFile(null);
      setTitle("");
      setAutoMode(null);
      if (input.current) input.current.value = "";
      onOpened(job, job.duplicate);
    },
    onError: (e: ApiError) => onError(e),
  });

  const pick = (chosen: File | null) => {
    setRefusal(null);
    setFile(chosen);
    if (!chosen) return;
    // The bytes decide the format and only the server can read them, so an unfamiliar suffix is
    // sent rather than refused here. What a browser can prove without opening the file is size,
    // and a document that could never be stored should not travel the whole way first.
    if (maxBytes && chosen.size > maxBytes) {
      setRefusal(
        t("imports.too_large", {
          mb: maxMb ?? Math.round(maxBytes / (1024 * 1024)),
          size: size(chosen.size, i18n.language),
        }),
      );
      setFile(null);
    }
  };

  return (
    <div className="card stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          ref={input}
          className="input"
          type="file"
          accept={accepted}
          disabled={upload.isPending}
          onChange={(e) => pick(e.target.files?.[0] || null)}
        />
        <input
          className="input"
          style={{ maxWidth: 260 }}
          placeholder={t("imports.title_placeholder")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <label className="row small" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={wantsAuto}
            onChange={(e) => setAutoMode(e.target.checked)}
          />
          {t("imports.auto_mode")}
        </label>
        <span className="spacer" />
        <button
          type="button"
          className="btn"
          disabled={!file || upload.isPending}
          onClick={() => upload.mutate()}
        >
          {upload.isPending ? t("imports.uploading") : t("imports.upload")}
        </button>
      </div>
      <div className="small muted">{t("imports.upload_hint", { formats: formats.map((format) => format.label).join(", ") })}</div>
      {refusal ? <div className="alert error">{refusal}</div> : null}
      {upload.isError ? <div className="alert error">{(upload.error as ApiError).message}</div> : null}
    </div>
  );
}

function JobRow({
  job,
  busy,
  onAction,
}: {
  job: ImportJob;
  busy: boolean;
  onAction: (action: "retry" | "remove") => void;
}) {
  const { t, i18n } = useTranslation();
  const source = job.source;
  const progress = job.progress || {};
  const counts = job.counts;
  const running = IN_FLIGHT.has(job.status);
  const outcome = typeof progress.outcome === "string" ? progress.outcome : null;
  const candidates = typeof progress.candidates === "number" ? progress.candidates : null;

  return (
    <tr>
      <td>
        <Link to={`/imports/${job.id}`}>{source?.title || job.id.slice(0, 8)}</Link>
        <div className="small muted">
          {source?.format_label || source?.mime_type || "—"}
          {source?.bytes ? ` · ${size(source.bytes, i18n.language)}` : ""}
          {source?.page_count ? ` · ${t("imports.pages_n", { n: source.page_count })}` : ""}
        </div>
        {job.auto_mode ? <div className="small muted">{t("imports.auto_mode_on")}</div> : null}
      </td>
      <td className="small">
        {running ? (
          t("imports.reading")
        ) : job.status === "failed" ? (
          t("imports.not_read")
        ) : outcome && EMPTY_FINDINGS.has(outcome) ? (
          t(`imports.outcome_${outcome}`)
        ) : (
          t("imports.candidates_n", { n: candidates ?? counts.total })
        )}
        {job.error ? <div className="small muted">{wordsFor(outcome, job.error)}</div> : null}
      </td>
      <td className="small">
        {counts.pending ? (
          <Link to={`/imports/${job.id}`}>{t("imports.waiting_n", { n: counts.pending })}</Link>
        ) : (
          t("imports.nothing_waiting")
        )}
        {counts.incomplete ? (
          <div className="small muted">{t("imports.incomplete_n", { n: counts.incomplete })}</div>
        ) : null}
        <div className="small muted">
          {t("imports.decided_counts", {
            approved: counts.approved,
            edited: counts.edited,
            rejected: counts.rejected,
          })}
        </div>
      </td>
      <td className="small">
        <span className="chip">{t(`imports.job_status_${job.status}`)}</span>
      </td>
      <td className="small">{job.created_at ? when(job.created_at) : "—"}</td>
      <td>
        <div className="row" style={{ gap: 2, flexWrap: "wrap" }}>
          <Link className="btn ghost" to={`/imports/${job.id}`}>
            {t("imports.open")}
          </Link>
          {job.status !== "processing" ? (
            <a className="btn ghost" href={importsApi.documentUrl(job.id) || "#"}>
              {t("imports.original")}
            </a>
          ) : null}
          {/* Both of these can be refused - a queue that already produced candidates does not
              re-read the paper, and a queue that filed content that is still in the bank is the
              record of where it came from. The counts below are the teacher's hint, not the rule,
              so the buttons are offered and the server's sentence is what answers. */}
          {job.status !== "processing" ? (
            <>
              <button type="button" className="btn ghost" disabled={busy} onClick={() => onAction("retry")}>
                {t("imports.retry")}
              </button>
              <button type="button" className="btn ghost" disabled={busy} onClick={() => onAction("remove")}>
                {t("imports.remove")}
              </button>
            </>
          ) : null}
        </div>
      </td>
    </tr>
  );
}
