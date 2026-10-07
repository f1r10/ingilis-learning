// The media library: upload a file, caption it, see what uses it, and take it out again.
//
// Three things this screen deliberately does not do:
//   * it never names a file's type - the server reads the bytes, and the pickers here are
//     built from `/media/meta` so the promise matches the rule;
//   * it never shows an object-store address - a row carries a `content_url` served by the
//     application under the teacher's own session;
//   * it does not guess a duration or a pixel size. The player reports what it measured,
//     and the library shows "not measured" until something has actually played the file.
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError, mediaUrl } from "../api/client";
import { mediaApi, type MediaAsset, type MediaSummary } from "../api/media";

const BYTES_PER_MB = 1024 * 1024;

export default function MediaLibrary() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [message, setMessage] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [refused, setRefused] = useState<{ id: string; reason: string }[]>([]);

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      kind: params.get("kind") || "",
      source_origin: params.get("source_origin") || "",
      view: params.get("view") || "bank",
      sort: params.get("sort") || "created_at",
      order: params.get("order") || "desc",
      page: Number(params.get("page") || 1),
      page_size: 24,
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

  const meta = useQuery({ queryKey: ["media-meta"], queryFn: mediaApi.meta });
  const list = useQuery({ queryKey: ["media", filters], queryFn: () => mediaApi.list(filters) });

  useEffect(() => {
    setSelected([]);
    setRefused([]);
  }, [params]);

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / filters.page_size));
  const allSelected = rows.length > 0 && selected.length === rows.length;
  const nameOf = (id: string) => {
    const row = rows.find((item) => item.id === id);
    return row ? row.label || row.original_filename || id.slice(0, 8) : id.slice(0, 8);
  };

  const refresh = () => qc.invalidateQueries({ queryKey: ["media"] });

  // A file a lesson still holds on to is refused by the library, and the library writes
  // that refusal in its own working language. The teacher reads it in theirs instead.
  const failed = (e: ApiError) =>
    setMessage(e.code === "asset_in_use" ? t("media.in_use_refusal") : e.message);
  const bulk = useMutation({
    mutationFn: (action: string) => mediaApi.bulk({ asset_ids: selected, action }),
    onSuccess: (result) => {
      setRefused(result.refused);
      setMessage(null);
      setSelected([]);
      refresh();
    },
    onError: failed,
  });

  const rowAction = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "trash" | "restore" }) =>
      action === "trash" ? mediaApi.trash(id) : mediaApi.restore(id),
    onSuccess: () => {
      setMessage(null);
      refresh();
    },
    onError: failed,
  });

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("media.title")}</h1>
        <span className="spacer" />
        <span className="small muted">{t("media.showing", { total })}</span>
      </div>

      <UploadCard
        formats={meta.data?.formats || []}
        maxBytesByKind={meta.data?.max_upload_bytes}
        maxMbByKind={meta.data?.max_upload_mb}
        onDone={(note) => {
          setMessage(note);
          refresh();
        }}
        onError={failed}
      />

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("media.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 140 }} value={filters.kind} onChange={(e) => setFilter({ kind: e.target.value })}>
          <option value="">{t("media.all_kinds")}</option>
          {(meta.data?.kinds || []).map((kind) => (
            <option key={kind} value={kind}>
              {t(`media.kind_${kind}`)}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 150 }} value={filters.view} onChange={(e) => setFilter({ view: e.target.value })}>
          <option value="bank">{t("media.view_bank")}</option>
          <option value="trash">{t("media.view_trash")}</option>
          <option value="all">{t("media.view_all")}</option>
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {(meta.data?.sortable || []).map((sort) => (
            <option key={sort} value={sort}>
              {t(`media.sort_${sort}`)}
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

      {message ? <div className="alert">{message}</div> : null}
      {list.isError ? (
        <div className="alert error">
          {t("common.could_not_load")} {(list.error as ApiError).message}
        </div>
      ) : null}

      {refused.length ? (
        <div className="card stack">
          <strong className="small">{t("media.bulk_refused")}</strong>
          <ul className="small muted">
            {refused.map((item) => (
              <li key={item.id}>
                {nameOf(item.id)} — {item.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {selected.length ? (
        <div className="card row" style={{ gap: 8 }}>
          <strong className="small">{t("media.selected", { n: selected.length })}</strong>
          <button className="btn secondary" onClick={() => bulk.mutate("trash")}>
            {t("media.trash")}
          </button>
          <button className="btn secondary" onClick={() => bulk.mutate("restore")}>
            {t("media.restore")}
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
              <th>{t("media.file")}</th>
              <th>{t("media.kind")}</th>
              <th>{t("media.size")}</th>
              <th>{t("media.measured")}</th>
              <th>{t("media.used_by")}</th>
              <th>{t("media.state")}</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <MediaRow
                key={row.id}
                row={row}
                selected={selected.includes(row.id)}
                onToggle={(checked) =>
                  setSelected((prev) => (checked ? [...prev, row.id] : prev.filter((id) => id !== row.id)))
                }
                onAction={(action) => rowAction.mutate({ id: row.id, action })}
                onMeasured={() => refresh()}
              />
            ))}
            {!list.isLoading && rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="muted">
                  {filters.view === "bank" ? t("media.empty") : t("media.empty_view")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <div className="row">
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

function UploadCard({
  formats,
  maxBytesByKind,
  maxMbByKind,
  onDone,
  onError,
}: {
  formats: { mime_type: string; kind: string; extension: string; label: string }[];
  maxBytesByKind?: Record<string, number>;
  maxMbByKind?: Record<string, number>;
  onDone: (note: string | null) => void;
  onError: (e: ApiError) => void;
}) {
  const { t, i18n } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  const accepted = formats.map((format) => `.${format.extension}`).join(",");
  const upload = useMutation({
    mutationFn: () => mediaApi.upload(file as File, label.trim() || undefined),
    onSuccess: (asset) => {
      setBusy(false);
      setFile(null);
      setLabel("");
      if (input.current) input.current.value = "";
      onDone(asset.deduplicated ? t("media.deduplicated", { name: asset.original_filename || asset.id }) : null);
    },
    onError: (e: ApiError) => {
      setBusy(false);
      onError(e);
    },
  });

  const pick = (chosen: File | null) => {
    setRefusal(null);
    setFile(chosen);
    if (!chosen) return;
    const dot = chosen.name.lastIndexOf(".");
    const suffix = dot < 0 ? "" : chosen.name.slice(dot + 1).toLowerCase();
    const format = formats.find((item) => item.extension.toLowerCase() === suffix);
    // The bytes decide what a file is, and only the server can read them, so a suffix this
    // screen does not recognise is not a refusal: the file is sent and the library answers
    // with its own sentence. What is stopped here is the one mistake a browser can prove
    // without opening the file - a size that could never be stored, which would otherwise
    // travel the whole way before anything said so.
    const ceiling = format ? maxBytesByKind?.[format.kind] : undefined;
    if (format && ceiling && chosen.size > ceiling) {
      setRefusal(
        t("media.too_large", {
          label: format.label,
          mb: maxMbByKind?.[format.kind] ?? Math.round(ceiling / BYTES_PER_MB),
          size: formatSize(chosen.size, i18n.language),
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
          disabled={busy}
          onChange={(e) => pick(e.target.files?.[0] || null)}
        />
        <input
          className="input"
          style={{ maxWidth: 260 }}
          placeholder={t("media.label_placeholder")}
          value={label}
          maxLength={200}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button
          className="btn"
          disabled={!file || busy}
          onClick={() => {
            setBusy(true);
            upload.mutate();
          }}
        >
          {busy ? t("media.uploading") : t("media.upload")}
        </button>
      </div>
      <div className="small muted">{t("media.upload_hint")}</div>
      <div className="small muted">
        {formats.map((format) => (
          <span key={format.mime_type} className="chip">
            {format.label} {maxMbByKind?.[format.kind] ? `≤ ${maxMbByKind[format.kind]} MB` : ""}
          </span>
        ))}
      </div>
      {refusal ? <div className="alert error">{refusal}</div> : null}
    </div>
  );
}

function MediaRow({
  row,
  selected,
  onToggle,
  onAction,
  onMeasured,
}: {
  row: MediaSummary;
  selected: boolean;
  onToggle: (checked: boolean) => void;
  onAction: (action: "trash" | "restore") => void;
  onMeasured: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [detail, setDetail] = useState<MediaAsset | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const source = detail || row;
  const url = mediaUrl(row.content_url);

  const patch = useMutation({
    mutationFn: (body: { duration_seconds?: number; width?: number; height?: number; label?: string }) =>
      mediaApi.patch(row.id, body),
    onSuccess: (asset) => {
      setDetail(asset);
      setError(null);
      onMeasured();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const load = useMutation({
    mutationFn: () => mediaApi.get(row.id),
    onSuccess: (asset) => setDetail(asset),
    onError: (e: ApiError) => setError(e.message),
  });

  // The player is the only one who can say how long the recording is. Report it once.
  const reportDuration = (seconds: number) => {
    if (!Number.isFinite(seconds) || seconds <= 0) return;
    if (row.duration_seconds) return;
    patch.mutate({ duration_seconds: Math.round(seconds * 100) / 100 });
  };

  const reportPixels = (width: number, height: number) => {
    if (!width || !height) return;
    if (row.width && row.height) return;
    patch.mutate({ width, height });
  };

  return (
    <>
      <tr>
        <td>
          <input type="checkbox" checked={selected} onChange={(e) => onToggle(e.target.checked)} />
        </td>
        <td>
          <div className="row" style={{ gap: 8 }}>
            <Preview row={row} url={url} onDuration={reportDuration} onPixels={reportPixels} />
            <span>
              <button className="btn ghost" style={{ padding: 0 }} onClick={() => {
                setOpen(!open);
                if (!detail) load.mutate();
              }}>
                {source.label || source.original_filename || t("media.unnamed")}
              </button>
              <div className="small muted">
                {source.format_label || source.mime_type || "—"}
                {source.original_filename ? ` · ${source.original_filename}` : ""}
              </div>
            </span>
          </div>
        </td>
        <td className="small">{t(`media.kind_${row.kind}`)}</td>
        <td className="small">{row.size_bytes ? formatSize(row.size_bytes, i18n.language) : "—"}</td>
        <td className="small">
          {row.kind === "image"
            ? row.width && row.height
              ? `${row.width}×${row.height}`
              : t("media.not_measured")
            : row.duration_seconds
              ? `${Math.round(row.duration_seconds)} s`
              : t("media.not_measured")}
        </td>
        <td className="small">
          {row.reference_count ? t("media.used_by_n", { n: row.reference_count }) : "—"}
        </td>
        <td className="small">
          {row.state === "trashed" ? (
            <span className="chip">{t("media.state_trashed")}</span>
          ) : (
            <span className="small muted">{t("media.state_available")}</span>
          )}
        </td>
        <td>
          <div className="row" style={{ gap: 2 }}>
            {row.state === "trashed" ? (
              <button className="btn ghost" onClick={() => onAction("restore")}>
                {t("media.restore")}
              </button>
            ) : (
              <button className="btn ghost" onClick={() => onAction("trash")}>
                {t("media.trash")}
              </button>
            )}
          </div>
        </td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={8}>
            <div className="stack" style={{ gap: 6 }}>
              {error ? <div className="alert error">{error}</div> : null}
              <CaptionEditor
                row={source}
                saving={patch.isPending}
                onSave={(label) => patch.mutate({ label })}
              />
              {detail ? (
                <div className="small muted">
                  {t("media.references_detail", {
                    words: detail.referenced_by.words,
                    listenings: detail.referenced_by.listenings,
                    questions: detail.referenced_by.questions,
                  })}
                </div>
              ) : null}
              <div className="small muted">
                {t("media.provenance", {
                  origin: source.source_origin ? t(`media.origin_${source.source_origin}`) : "—",
                  created: new Date(source.created_at).toLocaleDateString(i18n.language),
                })}
              </div>
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

function CaptionEditor({
  row,
  saving,
  onSave,
}: {
  row: MediaSummary;
  saving: boolean;
  onSave: (label: string) => void;
}) {
  const { t } = useTranslation();
  const [label, setLabel] = useState(row.label || "");
  return (
    <div className="row" style={{ gap: 6 }}>
      <input
        className="input"
        style={{ maxWidth: 300 }}
        value={label}
        maxLength={200}
        placeholder={t("media.label_placeholder")}
        onChange={(e) => setLabel(e.target.value)}
        aria-label={t("media.label_placeholder")}
      />
      <button className="btn secondary" disabled={saving || label.trim() === (row.label || "")} onClick={() => onSave(label)}>
        {t("media.save_caption")}
      </button>
    </div>
  );
}

function Preview({
  row,
  url,
  onDuration,
  onPixels,
}: {
  row: MediaSummary;
  url: string | null;
  onDuration: (seconds: number) => void;
  onPixels: (width: number, height: number) => void;
}) {
  if (!url) return null;
  if (row.kind === "image") {
    return <img src={url} alt="" width={48} height={48} style={{ objectFit: "cover" }} onLoad={(e) => {
      const img = e.currentTarget;
      onPixels(img.naturalWidth, img.naturalHeight);
    }} />;
  }
  if (row.kind === "video") {
    return (
      <video
        src={url}
        width={72}
        preload="metadata"
        onLoadedMetadata={(e) => {
          const el = e.currentTarget;
          onDuration(el.duration);
          onPixels(el.videoWidth, el.videoHeight);
        }}
      />
    );
  }
  return (
    <audio
      src={url}
      preload="metadata"
      controls
      style={{ width: 180 }}
      onLoadedMetadata={(e) => onDuration(e.currentTarget.duration)}
    />
  );
}

function formatSize(bytes: number, locale: string) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: unit ? 1 : 0 }).format(value)} ${units[unit]}`;
}
