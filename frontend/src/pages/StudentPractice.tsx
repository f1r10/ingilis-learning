// The learner's practice shelf: what they may open, what they did there, and what they
// saved to look at again.
//
// The list is built from `/student/practice/catalogs`, which already hides a catalog whose
// folder chain is not published and already counts this learner's runs of each row, so no
// number is computed here. A row that is a folder is practicable in its own right - it
// holds references too - so the screen does not pretend a folder is only a container.
import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { favoritesApi, practiceApi, type KnownStates, type PracticeDetail } from "../api/practice";

const PAGE_SIZE = 20;

export default function StudentPractice() {
  const { t, i18n } = useTranslation();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [shuffleThisTime, setShuffleThisTime] = useState<boolean | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const openId = params.get("open") || "";
  const tab = params.get("tab") === "saved" ? "saved" : "shelf";

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      language: params.get("language") || "",
      level: params.get("level") || "",
      sort: params.get("sort") || "name",
      order: params.get("order") || "asc",
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

  const meta = useQuery({ queryKey: ["practice-meta"], queryFn: practiceApi.meta });
  const list = useQuery({ queryKey: ["practice", filters], queryFn: () => practiceApi.list(filters) });
  const detail = useQuery({
    queryKey: ["practice-one", openId],
    queryFn: () => practiceApi.catalog(openId),
    enabled: Boolean(openId),
  });
  const favorites = useQuery({ queryKey: ["favorites"], queryFn: favoritesApi.list });
  const known = useQuery({
    queryKey: ["practice-known", openId],
    queryFn: () => practiceApi.known(openId),
    enabled: Boolean(openId),
  });

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const start = useMutation({
    mutationFn: (row: PracticeDetail) =>
      practiceApi.start(row.id, {
        shuffle: shuffleThisTime ?? row.shuffle_default,
        language: params.get("language") || undefined,
      }),
    onSuccess: (run) => {
      setMessage(null);
      nav(`/student/practice/run?session=${run.session_id}`);
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const removeFavorite = useMutation({
    mutationFn: ({ kind, refId }: { kind: string; refId: string }) => favoritesApi.remove(kind, refId),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["favorites"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  return (
    <div className="stack">
      <h1 style={{ margin: 0 }}>{t("practice.my_practice")}</h1>
      <p className="muted small">{t("practice.shelf_hint")}</p>

      <div className="tabs">
        <button type="button" className={`tab ${tab === "shelf" ? "active" : ""}`} onClick={() => setFilter({ tab: "" })}>
          {t("practice.shelf")}
        </button>
        <button type="button" className={`tab ${tab === "saved" ? "active" : ""}`} onClick={() => setFilter({ tab: "saved" })}>
          {t("practice.saved_tab")}
          {favorites.data?.items.length ? ` (${favorites.data.items.length})` : ""}
        </button>
      </div>

      {message ? <div className="alert error">{message}</div> : null}

      {tab === "saved" ? (
        <div className="card stack">
          {favorites.isError ? <div className="alert error">{t("common.could_not_load")}</div> : null}
          {(favorites.data?.items || []).map((row) => (
            <div className="row" key={row.id} style={{ gap: 8, borderBottom: "1px solid var(--border)", paddingBottom: 6 }}>
              <div style={{ flex: 1 }}>
                <div className="small">{row.title || t("practice.content_gone")}</div>
                <div className="row small muted" style={{ gap: 6 }}>
                  <span className="chip">{t(`catalogs.kind_${row.kind}`)}</span>
                  {row.detail ? <span>{row.detail}</span> : null}
                  {!row.available ? <span>{t("practice.no_longer_available")}</span> : null}
                </div>
              </div>
              <button
                type="button"
                className="btn ghost"
                onClick={() => removeFavorite.mutate({ kind: row.kind, refId: row.ref_id })}
              >
                {t("practice.take_off")}
              </button>
            </div>
          ))}
          {!favorites.isLoading && (favorites.data?.items || []).length === 0 ? (
            <div className="muted small">{t("practice.no_saved")}</div>
          ) : null}
        </div>
      ) : openId ? (
        <OpenCatalog
          page={detail.data}
          failed={detail.isError}
          error={detail.error as ApiError | undefined}
          onBack={() => setFilter({ open: "" })}
          onStart={() => detail.data && start.mutate(detail.data)}
          shuffling={shuffleThisTime ?? Boolean(detail.data?.shuffle_default)}
          onShuffle={setShuffleThisTime}
          known={known.data}
          starting={start.isPending}
        />
      ) : (
        <>
          <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
            <input
              className="input"
              style={{ maxWidth: 220 }}
              placeholder={t("practice.search")}
              value={filters.q}
              onChange={(e) => setFilter({ q: e.target.value })}
            />
            <select className="input" style={{ maxWidth: 110 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
              <option value="">{t("practice.all_levels")}</option>
              {(meta.data?.levels || []).map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
            <select className="input" style={{ maxWidth: 150 }} value={filters.language} onChange={(e) => setFilter({ language: e.target.value })}>
              <option value="">{t("practice.all_languages")}</option>
              {(meta.data?.learning_languages || []).map((code) => (
                <option key={code} value={code}>
                  {code === i18n.language ? `${code} · ${t("vocabulary.your_language")}` : code}
                </option>
              ))}
            </select>
            <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
              {(meta.data?.sortable || []).map((sort) => (
                <option key={sort} value={sort}>
                  {t(`practice.sort_${sort}`)}
                </option>
              ))}
            </select>
          </div>

          {list.isError ? (
            <div className="alert error">{t("common.could_not_load")} {(list.error as ApiError).message}</div>
          ) : null}

          <div className="card stack">
            {rows.map((row) => (
              <button
                type="button"
                key={row.id}
                onClick={() => setFilter({ open: row.id })}
                style={{
                  border: 0,
                  borderBottom: "1px solid var(--border)",
                  background: "transparent",
                  padding: "0.6rem 0",
                  textAlign: "left",
                  minHeight: 44,
                }}
              >
                <strong>{row.name}</strong>
                {row.description ? <div className="small muted">{row.description}</div> : null}
                <div className="row small muted" style={{ marginTop: 4, flexWrap: "wrap", gap: 6 }}>
                  <span className="chip">{row.level || "—"}</span>
                  <span>{t("practice.items_n", { n: row.item_count })}</span>
                  {row.runs ? <span>{t("practice.runs_n", { n: row.runs })}</span> : <span>{t("practice.not_tried_yet")}</span>}
                  <span className="spacer" />
                  <span>{row.learning_language || ""}</span>
                </div>
              </button>
            ))}
            {!list.isLoading && !list.isError && rows.length === 0 ? (
              <div className="muted small">{t("practice.nothing_for_you")}</div>
            ) : null}
          </div>

          <div className="row">
            <span className="small muted">{t("practice.showing", { total })}</span>
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
        </>
      )}

      {tab === "shelf" && !openId ? (
        <p className="small muted">
          <Link to="/student/practice?tab=saved">{t("practice.saved_hint")}</Link>
        </p>
      ) : null}
    </div>
  );
}

function OpenCatalog({
  page,
  failed,
  error,
  onBack,
  onStart,
  shuffling,
  onShuffle,
  known,
  starting,
}: {
  page: PracticeDetail | undefined;
  failed: boolean;
  error: ApiError | undefined;
  onBack: () => void;
  onStart: () => void;
  shuffling: boolean;
  onShuffle: (value: boolean) => void;
  known: KnownStates | undefined;
  starting: boolean;
}) {
  const { t } = useTranslation();

  if (failed) {
    return (
      <div className="stack">
        <button type="button" className="btn secondary" onClick={onBack}>
          ‹ {t("practice.back_to_shelf")}
        </button>
        <div className="alert error">{t("common.could_not_load")} {error?.message}</div>
      </div>
    );
  }
  if (!page) return <div className="card muted">{t("common.loading")}</div>;

  return (
    <div className="stack">
      <button type="button" className="btn secondary" onClick={onBack}>
        ‹ {t("practice.back_to_shelf")}
      </button>

      <div className="card stack">
        {page.path.length ? <div className="small muted">{page.path.map((crumb) => crumb.name).join(" › ")}</div> : null}
        <h1 style={{ margin: 0 }}>{page.name}</h1>
        {page.description ? <p style={{ margin: 0 }}>{page.description}</p> : null}
        <div className="row small muted" style={{ flexWrap: "wrap", gap: 6 }}>
          <span className="chip">{page.level || "—"}</span>
          <span>{t("practice.items_n", { n: page.item_count })}</span>
          <span>{t(`catalogs.timing_${page.feedback_timing}`)}</span>
        </div>

        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={shuffling} onChange={(e) => onShuffle(e.target.checked)} />
          {t("practice.shuffle_this_time")}
        </label>

        <button type="button" className="btn" onClick={onStart} disabled={starting || page.item_count === 0} style={{ minHeight: 44 }}>
          {starting ? t("common.loading") : t("practice.start")}
        </button>
        {page.item_count === 0 ? <p className="small muted">{t("practice.empty_catalog")}</p> : null}
        {page.feedback_timing === "after_session" ? (
          <p className="small muted">{t("practice.timing_note")}</p>
        ) : null}
      </div>

      {known?.enabled && known.word_count ? (
        <div className="card row small" style={{ flexWrap: "wrap", gap: 8 }}>
          <strong>{t("practice.your_marks")}</strong>
          <span>{t("practice.marked_known", { n: known.counts.known ?? 0 })}</span>
          <span>{t("practice.marked_learning", { n: known.counts.learning ?? 0 })}</span>
          <span className="muted">{t("practice.marked_unmarked", { n: known.counts.unmarked ?? 0 })}</span>
        </div>
      ) : null}

      <div className="card stack">
        <h3 style={{ margin: 0 }}>{t("practice.recent_runs")}</h3>
        {page.runs.length === 0 ? <div className="muted small">{t("practice.no_runs_yet")}</div> : null}
        {page.runs.map((run) => (
          <div className="row small" key={run.session_id} style={{ gap: 8, borderBottom: "1px solid var(--border)", paddingBottom: 6 }}>
            <span>{formatDate(run.started_at)}</span>
            <span className="spacer" />
            {run.finished ? (
              <span className="muted">{t("practice.result", { got: run.score, total: run.max_score })}</span>
            ) : (
              <Link to={`/student/practice/run?session=${run.session_id}`}>{t("practice.continue")}</Link>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function formatDate(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
