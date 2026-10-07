// One word, end to end: what it means, how it sounds in the class's languages, the
// sentences that show it in use, and where it is filed. The private note box says so on
// its label, because what a teacher writes for herself is not study material.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { tagsApi } from "../api/questions";
import { vocabularyApi, type Example, type Translation } from "../api/vocabulary";
import StudentCard from "../components/StudentCard";
import MediaPicker from "../components/MediaPicker";

interface Draft {
  word: string;
  learning_language: string;
  status: string;
  level: string;
  part_of_speech: string;
  ipa: string;
  definition: string;
  synonyms: string[];
  antonyms: string[];
  notes: string;
  audio_asset_id: string | null;
  translations: Translation[];
  examples: Example[];
  tag_ids: string[];
}

const BLANK: Draft = {
  word: "",
  learning_language: "",
  status: "ready",
  level: "",
  part_of_speech: "",
  ipa: "",
  definition: "",
  synonyms: [],
  antonyms: [],
  notes: "",
  audio_asset_id: null,
  translations: [],
  examples: [],
  tag_ids: [],
};

export default function VocabularyEditor() {
  const { t } = useTranslation();
  const { id } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Draft>(BLANK);
  const [message, setMessage] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const meta = useQuery({ queryKey: ["vocabulary-meta"], queryFn: vocabularyApi.meta });
  const tags = useQuery({ queryKey: ["tags"], queryFn: tagsApi.list });
  const entry = useQuery({
    queryKey: ["vocabulary-entry", id],
    queryFn: () => vocabularyApi.get(id as string),
    enabled: Boolean(id),
  });

  useEffect(() => {
    if (!id || !entry.data || loaded) return;
    const row = entry.data;
    setDraft({
      word: row.word,
      learning_language: row.learning_language || "",
      status: row.status,
      level: row.level || "",
      part_of_speech: row.part_of_speech || "",
      ipa: row.ipa || "",
      definition: row.definition || "",
      synonyms: row.synonyms,
      antonyms: row.antonyms,
      notes: row.notes || "",
      audio_asset_id: row.audio_asset_id,
      translations: row.translations,
      examples: row.examples,
      tag_ids: row.tags.map((tag) => tag.id),
    });
    setLoaded(true);
  }, [entry.data, id, loaded]);

  const patch = (fields: Partial<Draft>) => setDraft((prev) => ({ ...prev, ...fields }));

  const body = useMemo(
    () => ({
      word: draft.word.trim(),
      learning_language: draft.learning_language,
      definition: draft.definition.trim() || null,
      ipa: draft.ipa.trim() || null,
      part_of_speech: draft.part_of_speech.trim() || null,
      level: draft.level || null,
      synonyms: draft.synonyms,
      antonyms: draft.antonyms,
      notes: draft.notes.trim() || null,
      audio_asset_id: draft.audio_asset_id,
      // Sent whole, never merged: a set that arrives without one of its meanings means
      // the teacher deleted it. The ids the GET handed back are stripped - the endpoint
      // replaces the whole set, so an `id` here would promise a per-row update that does
      // not exist, and it is refused as an unknown field.
      translations: draft.translations
        .filter((row) => row.value.trim())
        .map((row) => ({ language: row.language, value: row.value })),
      examples: draft.examples
        .filter((row) => row.sentence.trim())
        .map((row) => ({ sentence: row.sentence, language: row.language, translation: row.translation })),
      tag_ids: draft.tag_ids,
    }),
    [draft],
  );

  const save = useMutation({
    mutationFn: async () => {
      if (id) return vocabularyApi.update(id, body);
      const created = await vocabularyApi.create({ ...body, status: draft.status });
      navigate(`/vocabulary/${created.id}`, { replace: true });
      return created;
    },
    onSuccess: (saved) => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["vocabulary"] });
      qc.invalidateQueries({ queryKey: ["vocabulary-entry", saved.id] });
      qc.invalidateQueries({ queryKey: ["vocabulary-preview", saved.id] });
      qc.invalidateQueries({ queryKey: ["tags"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const setStatus = useMutation({
    mutationFn: (status: string) => vocabularyApi.setStatus(id as string, status),
    onSuccess: (saved) => {
      setMessage(null);
      // The form loads once and then belongs to the teacher, so the only fields that
      // follow the server are the ones the server just changed. Without this the button
      // you clicked stays unlit and the header keeps naming the old state.
      setDraft((prev) => ({ ...prev, status: saved.status }));
      qc.invalidateQueries({ queryKey: ["vocabulary"] });
      qc.invalidateQueries({ queryKey: ["vocabulary-entry", id] });
      qc.invalidateQueries({ queryKey: ["vocabulary-preview", id] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const remove = useMutation({
    mutationFn: () => vocabularyApi.trash(id as string),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["vocabulary"] });
      navigate("/vocabulary");
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const preview = useQuery({
    queryKey: ["vocabulary-preview", id],
    queryFn: () => vocabularyApi.preview(id as string),
    enabled: Boolean(id),
  });

  const learningLanguages = meta.data?.learning_languages || [];
  // The word's own language is never one of its translation languages: an English word
  // is not translated into English, and the backend refuses it.
  const translationLanguages = (meta.data?.translation_languages || []).filter(
    (code) => code !== draft.learning_language,
  );
  // One meaning per language, so a new row opens on a language that is still free rather
  // than on the first one and making the teacher hit the duplicate rule on save.
  const usedLanguages = new Set(draft.translations.map((row) => row.language));
  const freeTranslationLanguages = translationLanguages.filter((code) => !usedLanguages.has(code));
  const exampleLanguages = meta.data?.example_languages || [];

  return (
    <div className="stack">
      <div className="row">
        <Link to="/vocabulary">‹ {t("vocabulary.back_to_bank")}</Link>
        <span className="spacer" />
        {id ? <span className="small muted">{t(`status.${draft.status}`)}</span> : null}
      </div>

      <h1 style={{ margin: 0 }}>{id ? t("vocabulary.edit") : t("vocabulary.new")}</h1>
      {id ? null : <p className="small muted">{t("vocabulary.lifecycle_hint")}</p>}
      {message ? <div className="alert error">{message}</div> : null}

      <div className="two-col">
        <div className="card stack">
          <div className="field">
            <label htmlFor="word">{t("vocabulary.word")}</label>
            <input id="word" className="input" value={draft.word} onChange={(e) => patch({ word: e.target.value })} />
          </div>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <div className="field" style={{ flex: 1, minWidth: 150 }}>
              <label htmlFor="learning_language">{t("vocabulary.learning_language")}</label>
              <select
                id="learning_language"
                className="input"
                value={draft.learning_language}
                onChange={(e) => patch({ learning_language: e.target.value })}
              >
                <option value="">{t("vocabulary.choose_language")}</option>
                {learningLanguages.map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1, minWidth: 120 }}>
              <label htmlFor="level">{t("vocabulary.level")}</label>
              <select id="level" className="input" value={draft.level} onChange={(e) => patch({ level: e.target.value })}>
                <option value="">{t("vocabulary.no_level")}</option>
                {(meta.data?.levels || []).map((level) => (
                  <option key={level} value={level}>
                    {level}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1, minWidth: 140 }}>
              <label htmlFor="part_of_speech">{t("vocabulary.part_of_speech")}</label>
              <input
                id="part_of_speech"
                className="input"
                list="parts-of-speech"
                value={draft.part_of_speech}
                onChange={(e) => patch({ part_of_speech: e.target.value })}
                placeholder={t("vocabulary.part_of_speech_placeholder")}
              />
              <datalist id="parts-of-speech">
                {(meta.data?.parts_of_speech || []).map((part) => (
                  <option key={part} value={part} />
                ))}
              </datalist>
            </div>
          </div>

          <div className="field">
            <label htmlFor="definition">{t("vocabulary.definition")}</label>
            <textarea
              id="definition"
              className="input"
              rows={2}
              value={draft.definition}
              onChange={(e) => patch({ definition: e.target.value })}
            />
            <p className="small muted">{t("vocabulary.definition_hint")}</p>
          </div>

          <div className="field">
            <label htmlFor="ipa">{t("vocabulary.ipa")}</label>
            <input id="ipa" className="input" value={draft.ipa} onChange={(e) => patch({ ipa: e.target.value })} />
          </div>

          <div className="field">
            <label>{t("vocabulary.pronunciation")}</label>
            <MediaPicker
              value={draft.audio_asset_id}
              onChange={(assetId) => patch({ audio_asset_id: assetId })}
              kind="audio"
              copy={{
                none: t("vocabulary.no_audio_chosen"),
                choose: t("vocabulary.choose_audio"),
                detach: t("vocabulary.detach_audio"),
                trashed: t("vocabulary.trashed_audio_hint"),
              }}
            />
            <p className="small muted">{t("vocabulary.pronunciation_hint")}</p>
          </div>

          <WordList label={t("vocabulary.synonyms")} value={draft.synonyms} onChange={(synonyms) => patch({ synonyms })} />
          <WordList label={t("vocabulary.antonyms")} value={draft.antonyms} onChange={(antonyms) => patch({ antonyms })} />

          <div className="field">
            <label htmlFor="notes">{t("vocabulary.notes")}</label>
            <textarea id="notes" className="input" rows={2} value={draft.notes} onChange={(e) => patch({ notes: e.target.value })} />
            <p className="small muted">{t("vocabulary.teacher_only")}</p>
          </div>

          <div className="field">
            <label>{t("vocabulary.filed_under")}</label>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {(tags.data?.items || []).map((tag) => {
                const on = draft.tag_ids.includes(tag.id);
                return (
                  <button
                    type="button"
                    key={tag.id}
                    className={on ? "chip on" : "chip"}
                    onClick={() =>
                      patch({ tag_ids: on ? draft.tag_ids.filter((existing) => existing !== tag.id) : [...draft.tag_ids, tag.id] })
                    }
                  >
                    {tag.name}
                  </button>
                );
              })}
              {(tags.data?.items || []).length === 0 ? <span className="small muted">{t("vocabulary.no_tags_yet")}</span> : null}
            </div>
          </div>

          {!id ? (
            <div className="field">
              <label htmlFor="new_status">{t("vocabulary.initial_status")}</label>
              <select
                id="new_status"
                className="input"
                value={draft.status}
                onChange={(e) => patch({ status: e.target.value })}
              >
                {/* `/vocabulary/meta` lists draft, ready and archived. Archived is a way
                    out of the bank, not a way into it, so a new word picks between the
                    two states it can actually start in. */}
                {(meta.data?.statuses || [])
                  .filter((status) => status !== "archived")
                  .map((status) => (
                    <option key={status} value={status}>
                      {t(`status.${status}`)}
                    </option>
                  ))}
              </select>
              <p className="small muted">{t("vocabulary.initial_status_hint")}</p>
            </div>
          ) : null}

          <div className="row">
            <button className="btn" onClick={() => save.mutate()} disabled={save.isPending}>
              {id ? t("vocabulary.save") : t("vocabulary.create")}
            </button>
            {save.isSuccess ? <span className="small muted">{t("vocabulary.saved")}</span> : null}
          </div>
        </div>

        <div className="stack">
          <div className="card stack">
            <strong>{t("vocabulary.meanings")}</strong>
            <p className="small muted">{t("vocabulary.meanings_hint")}</p>
            {draft.translations.map((row, index) => (
              <div className="row" key={index} style={{ gap: 6 }}>
                <select
                  className="input"
                  style={{ maxWidth: 90 }}
                  value={row.language}
                  onChange={(e) =>
                    patch({ translations: draft.translations.map((r, i) => (i === index ? { ...r, language: e.target.value } : r)) })
                  }
                >
                  <option value="">{t("vocabulary.choose_language")}</option>
                  {translationLanguages.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
                <input
                  className="input"
                  value={row.value}
                  placeholder={t("vocabulary.meaning_placeholder")}
                  onChange={(e) =>
                    patch({ translations: draft.translations.map((r, i) => (i === index ? { ...r, value: e.target.value } : r)) })
                  }
                />
                <button
                  type="button"
                  className="chip-x"
                  aria-label={t("vocabulary.remove")}
                  onClick={() => patch({ translations: draft.translations.filter((_, i) => i !== index) })}
                >
                  ×
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn secondary"
              disabled={freeTranslationLanguages.length === 0}
              onClick={() =>
                patch({ translations: [...draft.translations, { language: freeTranslationLanguages[0], value: "" }] })
              }
            >
              {t("vocabulary.add_language")}
            </button>
          </div>

          <div className="card stack">
            <strong>{t("vocabulary.examples")}</strong>
            <p className="small muted">{t("vocabulary.examples_hint")}</p>
            {draft.examples.map((row, index) => (
              <div className="stack" key={index}>
                <div className="row" style={{ gap: 6 }}>
                  <input
                    className="input"
                    value={row.sentence}
                    placeholder={t("vocabulary.sentence_placeholder")}
                    onChange={(e) =>
                      patch({ examples: draft.examples.map((r, i) => (i === index ? { ...r, sentence: e.target.value } : r)) })
                    }
                  />
                  <select
                    className="input"
                    style={{ maxWidth: 80 }}
                    value={row.language || ""}
                    onChange={(e) =>
                      patch({ examples: draft.examples.map((r, i) => (i === index ? { ...r, language: e.target.value || null } : r)) })
                    }
                  >
                    <option value="">{t("vocabulary.unlabelled")}</option>
                    {exampleLanguages.map((code) => (
                      <option key={code} value={code}>
                        {code}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="chip-x"
                    aria-label={t("vocabulary.remove")}
                    onClick={() => patch({ examples: draft.examples.filter((_, i) => i !== index) })}
                  >
                    ×
                  </button>
                </div>
                <input
                  className="input"
                  value={row.translation || ""}
                  placeholder={t("vocabulary.example_translation_placeholder")}
                  onChange={(e) =>
                    patch({
                      examples: draft.examples.map((r, i) => (i === index ? { ...r, translation: e.target.value || null } : r)),
                    })
                  }
                />
              </div>
            ))}
            <button
              type="button"
              className="btn secondary"
              onClick={() => patch({ examples: [...draft.examples, { sentence: "", language: draft.learning_language || null, translation: null }] })}
            >
              {t("vocabulary.add_example")}
            </button>
          </div>

          {id && entry.data ? (
            <div className="card stack">
              <strong>{t("vocabulary.lifecycle")}</strong>
              <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                {(meta.data?.statuses || []).map((status) => (
                  <button
                    key={status}
                    className={status === draft.status ? "btn" : "btn secondary"}
                    disabled={status === draft.status || setStatus.isPending}
                    onClick={() => setStatus.mutate(status)}
                  >
                    {t(`status.${status}`)}
                  </button>
                ))}
                <span className="spacer" />
                <button className="btn danger" onClick={() => remove.mutate()}>
                  {t("vocabulary.trash")}
                </button>
              </div>
              <p className="small muted">{t("vocabulary.lifecycle_hint")}</p>
            </div>
          ) : null}

          {id && preview.data ? (
            <div className="stack">
              <strong className="small">{t("vocabulary.student_card")}</strong>
              <StudentCard card={preview.data} />
              <p className="small muted">{t("vocabulary.student_card_hint")}</p>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function WordList({ label, value, onChange }: { label: string; value: string[]; onChange: (next: string[]) => void }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const add = () => {
    const word = text.trim();
    if (!word) return;
    if (value.some((existing) => existing.toLowerCase() === word.toLowerCase())) {
      setText("");
      return;
    }
    onChange([...value, word]);
    setText("");
  };
  return (
    <div className="field">
      <label>{label}</label>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        {value.map((word, index) => (
          <span className="chip" key={word}>
            {word}
            <button
              type="button"
              className="chip-x"
              aria-label={t("vocabulary.remove")}
              onClick={() => onChange(value.filter((_, i) => i !== index))}
            >
              ×
            </button>
          </span>
        ))}
        <input
          className="input"
          style={{ maxWidth: 160 }}
          value={text}
          placeholder={t("vocabulary.add_word")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
        />
        <button type="button" className="btn secondary" onClick={add}>
          {t("common.add")}
        </button>
      </div>
    </div>
  );
}
