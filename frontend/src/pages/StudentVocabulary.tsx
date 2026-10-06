// The student's word list: search it, narrow it to the language they study in, and open
// a card. The card is the same projection the teacher previews, fetched from the learner
// endpoint - so there is no second copy of the rules here.
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { learnerVocabularyApi } from "../api/vocabulary";
import StudentCard from "../components/StudentCard";

const PAGE_SIZE = 30;

export default function StudentVocabulary() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();

  const filters = useMemo(
    () => ({
      q: params.get("q") || "",
      level: params.get("level") || "",
      translation_language: params.get("meaning_language") || "",
      sort: params.get("sort") || "word",
      page: Number(params.get("page") || 1),
      page_size: PAGE_SIZE,
    }),
    [params],
  );

  const openId = params.get("word") || "";

  const setFilter = (patch: Record<string, string>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => {
      if (!value) next.delete(key);
      else next.set(key, value);
    });
    if (!("page" in patch)) next.delete("page");
    setParams(next);
  };

  const meta = useQuery({ queryKey: ["student-vocabulary-meta"], queryFn: learnerVocabularyApi.meta });
  const list = useQuery({ queryKey: ["student-vocabulary", filters], queryFn: () => learnerVocabularyApi.list(filters) });
  const card = useQuery({
    queryKey: ["student-vocabulary-card", openId, filters.translation_language],
    queryFn: () => learnerVocabularyApi.card(openId, filters.translation_language || undefined),
    enabled: Boolean(openId),
  });

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="stack">
      <h1 style={{ margin: 0 }}>{t("vocabulary.my_words")}</h1>
      <p className="muted small">{t("vocabulary.my_words_hint")}</p>

      {openId ? (
        <div className="stack">
          <div className="row">
            <button className="btn secondary" onClick={() => setFilter({ word: "" })}>
              ‹ {t("vocabulary.back_to_list")}
            </button>
          </div>
          {card.data ? (
            <StudentCard card={card.data} />
          ) : card.isError ? (
            <div className="alert error">{t("common.could_not_load")}</div>
          ) : (
            <div className="card muted">{t("common.loading")}</div>
          )}
        </div>
      ) : (
        <>
          <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
            <input
              className="input"
              style={{ maxWidth: 220 }}
              placeholder={t("vocabulary.search_words")}
              value={filters.q}
              onChange={(e) => setFilter({ q: e.target.value })}
            />
            <select className="input" style={{ maxWidth: 120 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
              <option value="">{t("vocabulary.all_levels")}</option>
              {(meta.data?.levels || []).map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
            <select
              className="input"
              style={{ maxWidth: 190 }}
              value={filters.translation_language}
              onChange={(e) => setFilter({ meaning_language: e.target.value })}
            >
              <option value="">{t("vocabulary.all_meaning_languages")}</option>
              {(meta.data?.translation_languages || []).map((code) => (
                <option key={code} value={code}>
                  {code === i18n.language ? `${code} · ${t("vocabulary.your_language")}` : code}
                </option>
              ))}
            </select>
          </div>

          {list.isError ? (
            <div className="alert error">{t("common.could_not_load")} {t("vocabulary.try_again_hint")}</div>
          ) : null}

          <div className="card stack">
            {rows.map((row) => (
              <button
                type="button"
                key={row.id}
                className="row"
                style={{ border: 0, background: "transparent", padding: "0.5rem 0", borderBottom: "1px solid var(--border)", textAlign: "left", minHeight: 44 }}
                onClick={() => setFilter({ word: row.id })}
              >
                <span style={{ flex: 1 }}>
                  <strong>{row.word}</strong>
                  {row.ipa ? <span className="small muted"> {row.ipa}</span> : null}
                  <div className="small muted">{row.definition || ""}</div>
                </span>
                {row.level ? <span className="chip">{row.level}</span> : null}
                <span className="small muted">{row.translation_languages.join(", ")}</span>
              </button>
            ))}
            {!list.isLoading && !list.isError && rows.length === 0 ? (
              <div className="muted small">{t("vocabulary.no_words_yet")}</div>
            ) : null}
          </div>

          <div className="row">
            <span className="small muted">{t("vocabulary.showing", { total })}</span>
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
        </>
      )}
    </div>
  );
}
