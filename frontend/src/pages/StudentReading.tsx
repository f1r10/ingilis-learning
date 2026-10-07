// A learner's reading: the text, then what it asks.
//
// The screen is the same projection the teacher previews (`/student/reading/{id}`), so the
// blocks and questions here cannot drift from what the author saw. The layout the teacher
// chose decides how the text and the questions sit next to each other, and a phone gets one
// column whatever was chosen.
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import LearnerPreview from "../components/LearnerPreview";
import { learnerReadingApi } from "../api/reading";

const PAGE_SIZE = 20;

export default function StudentReading() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<"text" | "questions">("text");

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      language: params.get("language") || "",
      level: params.get("level") || "",
      sort: params.get("sort") || "updated_at",
      order: params.get("order") || "desc",
      page: Number(params.get("page") || 1),
      page_size: PAGE_SIZE,
    }),
    [params],
  );

  const openId = params.get("text") || "";

  const setFilter = (patch: Record<string, string>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => {
      if (!value) next.delete(key);
      else next.set(key, value);
    });
    if (!("page" in patch)) next.delete("page");
    setParams(next);
  };

  const meta = useQuery({ queryKey: ["student-reading-meta"], queryFn: learnerReadingApi.meta });
  const list = useQuery({ queryKey: ["student-reading", filters], queryFn: () => learnerReadingApi.list(filters) });
  const detail = useQuery({
    queryKey: ["student-reading-one", openId],
    queryFn: () => learnerReadingApi.get(openId),
    enabled: Boolean(openId),
  });

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  if (openId) {
    if (detail.isError) {
      return (
        <div className="stack">
          <button className="btn secondary" onClick={() => setFilter({ text: "" })}>
            ‹ {t("reading.back_to_list")}
          </button>
          <div className="alert error">{t("common.could_not_load")} {(detail.error as ApiError).message}</div>
        </div>
      );
    }
    if (!detail.data) return <div className="card muted">{t("common.loading")}</div>;
    const page = detail.data;
    const split = page.layout === "split";
    const tabbed = page.layout === "tabbed";

    const body = (
      <div className="card stack">
        <div className="row small muted">
          <span>{page.level || "—"}</span>
          <span className="spacer" />
          <span>{t("reading.questions_n", { n: page.sets.reduce((n, set) => n + set.question_count, 0) })}</span>
        </div>
        <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.7 }}>{page.body}</div>
      </div>
    );
    const exercises = (
      <div className="stack">
        {page.sets.map((set) => (
          <div className="stack" key={set.id}>
            <h3 style={{ margin: 0 }}>{set.title}</h3>
            {set.instructions ? <p className="small muted">{set.instructions}</p> : null}
            {set.questions.map((question) => (
              <LearnerPreview key={question.id} view={question} />
            ))}
          </div>
        ))}
        {page.sets.length === 0 ? <div className="muted small">{t("reading.no_exercises")}</div> : null}
      </div>
    );

    return (
      <div className="stack">
        <div className="row">
          <button className="btn secondary" onClick={() => setFilter({ text: "" })}>
            ‹ {t("reading.back_to_list")}
          </button>
        </div>
        <h1 style={{ margin: 0 }}>{page.title}</h1>

        {split ? (
          <div className="two-col">
            {body}
            {exercises}
          </div>
        ) : tabbed ? (
          <div className="stack">
            <div className="tabs">
              <button type="button" className={`tab ${tab === "text" ? "active" : ""}`} onClick={() => setTab("text")}>
                {t("reading.tab_text")}
              </button>
              <button type="button" className={`tab ${tab === "questions" ? "active" : ""}`} onClick={() => setTab("questions")}>
                {t("reading.tab_questions")}
              </button>
            </div>
            {tab === "text" ? body : exercises}
          </div>
        ) : (
          <div className="stack">
            {body}
            {exercises}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="stack">
      <h1 style={{ margin: 0 }}>{t("reading.my_readings")}</h1>
      <p className="muted small">{t("reading.my_readings_hint")}</p>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("reading.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 120 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          <option value="">{t("reading.all_levels")}</option>
          {(meta.data?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.language} onChange={(e) => setFilter({ language: e.target.value })}>
          <option value="">{t("reading.all_languages")}</option>
          {(meta.data?.learning_languages || []).map((code) => (
            <option key={code} value={code}>
              {code === i18n.language ? `${code} · ${t("vocabulary.your_language")}` : code}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {(meta.data?.sortable || []).map((sort) => (
            <option key={sort} value={sort}>
              {t(`reading.sort_${sort}`)}
            </option>
          ))}
        </select>
      </div>

      {list.isError ? <div className="alert error">{t("common.could_not_load")} {t("vocabulary.try_again_hint")}</div> : null}

      <div className="card stack">
        {rows.map((row) => (
          <button
            type="button"
            key={row.id}
            onClick={() => setFilter({ text: row.id })}
            style={{
              border: 0,
              borderBottom: "1px solid var(--border)",
              background: "transparent",
              padding: "0.6rem 0",
              textAlign: "left",
              minHeight: 44,
            }}
          >
            <strong>{row.title}</strong>
            <div className="small muted">{row.excerpt}</div>
            <div className="row small muted" style={{ marginTop: 4 }}>
              <span className="chip">{row.level || "—"}</span>
              <span>{t("reading.questions_n", { n: row.question_count })}</span>
              <span className="spacer" />
              <span>{row.language || ""}</span>
            </div>
          </button>
        ))}
        {!list.isLoading && !list.isError && rows.length === 0 ? (
          <div className="muted small">{t("reading.nothing_for_you")}</div>
        ) : null}
      </div>

      <div className="row">
        <span className="small muted">{t("reading.showing", { total })}</span>
        <span className="spacer" />
        <button className="btn secondary" disabled={filters.page <= 1} onClick={() => setFilter({ page: String(filters.page - 1) })}>
          ‹
        </button>
        <span className="small">
          {filters.page} / {pages}
        </span>
        <button className="btn secondary" disabled={filters.page >= pages} onClick={() => setFilter({ page: String(filters.page + 1) })}>
          ›
        </button>
      </div>
    </div>
  );
}
