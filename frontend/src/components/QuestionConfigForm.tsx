// The type-specific part of the question editor.
//
// The backend owns each type's shape (`app/services/question_engine`) and rejects
// unknown keys, so this form only ever sends the fields that type declares. The
// list of types itself comes from `/questions/types`, never from a constant here.
import { useTranslation } from "react-i18next";

export type Config = Record<string, any>;

interface Props {
  type: string;
  config: Config;
  onChange: (next: Config) => void;
}

function emptyConfig(type: string): Config {
  switch (type) {
    case "multiple_choice":
    case "multi_select":
      return { options: [{ text: "", correct: false }, { text: "", correct: false }] };
    case "true_false":
      return { statement: "", correct: true };
    case "short_answer":
      return { accepted: [""] };
    case "gap_fill":
      return { text: "", blanks: [{ accepted: [""] }], word_bank: [] };
    case "matching":
      return { pairs: [{ left: "", right: "" }, { left: "", right: "" }], extra_rights: [] };
    case "ordering":
      return { items: ["", ""], separator: " ", show_separator: false };
    case "translation":
      return { source: "", accepted: [""] };
    case "essay":
      return { rubric: [] };
    default:
      return {};
  }
}

export { emptyConfig };

const NORMALIZATION_KEYS = [
  "case_insensitive",
  "trim",
  "collapse_spaces",
  "ignore_punctuation",
  "ignore_articles",
  "ignore_diacritics",
] as const;

/** Only the rules the teacher actually changed are sent, so the engine's defaults apply. */
function normalizationDiff(current: Config): Record<string, boolean> {
  const stored = (current.normalization || {}) as Record<string, boolean>;
  const diff: Record<string, boolean> = {};
  for (const key of NORMALIZATION_KEYS) if (stored[key] === false) diff[key] = false;
  return Object.keys(diff).length ? diff : (undefined as unknown as Record<string, boolean>);
}

export default function QuestionConfigForm({ type, config, onChange }: Props) {
  const { t } = useTranslation();

  const set = (patch: Config) => onChange({ ...config, ...patch });
  const options = (config.options || []) as { text: string; correct: boolean }[];
  const accepted = (config.accepted || []) as string[];
  const blanks = (config.blanks || []) as { accepted: string[]; label?: string | null }[];
  const pairs = (config.pairs || []) as { left: string; right: string }[];
  const items = (config.items || []) as string[];
  const rubric = (config.rubric || []) as { name: string; max_score?: number | null; guidance?: string | null }[];
  const wordBank = (config.word_bank || []) as string[];
  const extraRights = (config.extra_rights || []) as string[];

  const norm = {
    case_insensitive: true,
    trim: true,
    collapse_spaces: true,
    ignore_punctuation: false,
    ignore_articles: false,
    ignore_diacritics: false,
    ...((config.normalization || {}) as Record<string, boolean>),
  };

  function textList(
    values: string[],
    key: string,
    placeholder: string,
    opts: { max?: number } = {},
  ) {
    const update = (next: string[]) => set({ [key]: next } as Config);
    return (
      <div className="stack">
        {values.map((value, index) => (
          <div className="row" key={index}>
            <input
              className="input"
              value={value}
              placeholder={placeholder}
              onChange={(e) => {
                const next = [...values];
                next[index] = e.target.value;
                update(next);
              }}
            />
            <button
              type="button"
              className="btn ghost"
              onClick={() => update(values.filter((_, i) => i !== index))}
              disabled={values.length <= 1}
            >
              {t("questions.remove")}
            </button>
          </div>
        ))}
        <div>
          <button
            type="button"
            className="btn secondary"
            onClick={() => update([...values, ""])}
            disabled={!!opts.max && values.length >= opts.max}
          >
            {t("questions.add_option")}
          </button>
        </div>
      </div>
    );
  }

  if (type === "multiple_choice" || type === "multi_select") {
    const single = type === "multiple_choice";
    return (
      <div className="stack">
        <p className="small muted">
          {single ? t("questions.hint_single") : t("questions.hint_multi")}
        </p>
        {options.map((option, index) => (
          <div className="row" key={index}>
            <input
              className="input"
              value={option.text}
              placeholder={t("questions.option_n", { n: index + 1 })}
              onChange={(e) => {
                const next = [...options];
                next[index] = { ...next[index], text: e.target.value };
                set({ options: next });
              }}
            />
            <label className="row small">
              <input
                type={single ? "radio" : "checkbox"}
                name={single ? "correct-option" : undefined}
                checked={option.correct}
                onChange={() => {
                  const next = single
                    ? options.map((o, i) => ({ ...o, correct: i === index }))
                    : options.map((o, i) => (i === index ? { ...o, correct: !o.correct } : o));
                  set({ options: next });
                }}
              />
              {t("questions.correct")}
            </label>
            <button
              type="button"
              className="btn ghost"
              onClick={() => set({ options: options.filter((_, i) => i !== index) })}
              disabled={options.length <= 2}
            >
              {t("questions.remove")}
            </button>
          </div>
        ))}
        <div>
          <button
            type="button"
            className="btn secondary"
            onClick={() => set({ options: [...options, { text: "", correct: false }] })}
            disabled={options.length >= 26}
          >
            {t("questions.add_option")}
          </button>
        </div>
      </div>
    );
  }

  if (type === "true_false") {
    return (
      <div className="stack">
        <div className="field">
          <label>{t("questions.statement")}</label>
          <textarea
            className="input"
            rows={3}
            value={config.statement || ""}
            onChange={(e) => set({ statement: e.target.value })}
          />
        </div>
        <div className="row">
          <label className="small">
            <input
              type="radio"
              checked={config.correct === true}
              onChange={() => set({ correct: true })}
            />{" "}
            {t("questions.true")}
          </label>
          <label className="small">
            <input
              type="radio"
              checked={config.correct === false}
              onChange={() => set({ correct: false })}
            />{" "}
            {t("questions.false")}
          </label>
        </div>
      </div>
    );
  }

  if (type === "short_answer") {
    return (
      <div className="stack">
        <div className="field">
          <label>{t("questions.accepted")}</label>
          {textList(accepted, "accepted", t("questions.accepted_placeholder"), { max: 40 })}
        </div>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.max_characters")}</label>
            <input
              className="input"
              type="number"
              min={1}
              value={config.max_characters ?? ""}
              onChange={(e) =>
                set({ max_characters: e.target.value === "" ? null : Number(e.target.value) })
              }
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.hint")}</label>
            <input
              className="input"
              value={config.hint || ""}
              onChange={(e) => set({ hint: e.target.value || null })}
            />
          </div>
        </div>
        {normalizationEditor()}
      </div>
    );
  }

  if (type === "gap_fill") {
    const markers = (String(config.text || "").match(/_{3,}|\{\{\s*\}\}/g) || []).length;
    return (
      <div className="stack">
        <div className="field">
          <label>{t("questions.text_with_gaps")}</label>
          <textarea
            className="input"
            rows={4}
            value={config.text || ""}
            onChange={(e) => set({ text: e.target.value })}
          />
          <p className="small muted">
            {t("questions.gap_marker_hint")}{" "}
            {markers === blanks.length
              ? t("questions.gap_marker_ok", { n: markers })
              : t("questions.gap_marker_mismatch", { markers, blanks: blanks.length })}
          </p>
        </div>
        <div className="field">
          <label>{t("questions.blanks")}</label>
          <div className="stack">
            {blanks.map((blank, index) => (
              <div className="card" key={index}>
                <div className="row">
                  <strong className="small">{t("questions.blank_n", { n: index + 1 })}</strong>
                  <input
                    className="input"
                    style={{ maxWidth: 160 }}
                    placeholder={t("questions.blank_label")}
                    value={blank.label || ""}
                    onChange={(e) => {
                      const next = [...blanks];
                      next[index] = { ...next[index], label: e.target.value || null };
                      set({ blanks: next });
                    }}
                  />
                  <button
                    type="button"
                    className="btn ghost"
                    onClick={() => set({ blanks: blanks.filter((_, i) => i !== index) })}
                    disabled={blanks.length <= 1}
                  >
                    {t("questions.remove")}
                  </button>
                </div>
                <div className="stack" style={{ marginTop: 8 }}>
                  {(blank.accepted || [""]).map((answer, answerIndex) => (
                    <input
                      className="input"
                      key={answerIndex}
                      value={answer}
                      placeholder={t("questions.accepted_placeholder")}
                      onChange={(e) => {
                        const next = [...blanks];
                        const answers = [...(next[index].accepted || [])];
                        answers[answerIndex] = e.target.value;
                        next[index] = { ...next[index], accepted: answers };
                        set({ blanks: next });
                      }}
                    />
                  ))}
                  <div className="row">
                    <button
                      type="button"
                      className="btn secondary"
                      onClick={() => {
                        const next = [...blanks];
                        next[index] = { ...next[index], accepted: [...(next[index].accepted || []), ""] };
                        set({ blanks: next });
                      }}
                    >
                      {t("questions.add_accepted")}
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => {
                        const next = [...blanks];
                        const answers = (next[index].accepted || []).filter((_, i) => i !== 0);
                        next[index] = { ...next[index], accepted: answers.length ? answers : [""] };
                        set({ blanks: next });
                      }}
                      disabled={(blank.accepted || []).length <= 1}
                    >
                      {t("questions.remove")}
                    </button>
                  </div>
                </div>
              </div>
            ))}
            <div>
              <button
                type="button"
                className="btn secondary"
                onClick={() => set({ blanks: [...blanks, { accepted: [""] }] })}
                disabled={blanks.length >= 40}
              >
                {t("questions.add_blank")}
              </button>
            </div>
          </div>
        </div>
        <div className="field">
          <label>{t("questions.word_bank")}</label>
          {textList(wordBank, "word_bank", t("questions.word_placeholder"), { max: 60 })}
        </div>
        {normalizationEditor()}
      </div>
    );
  }

  if (type === "matching") {
    return (
      <div className="stack">
        <p className="small muted">{t("questions.matching_hint")}</p>
        {pairs.map((pair, index) => (
          <div className="row" key={index}>
            <input
              className="input"
              value={pair.left}
              placeholder={t("questions.left_item")}
              onChange={(e) => {
                const next = [...pairs];
                next[index] = { ...next[index], left: e.target.value };
                set({ pairs: next });
              }}
            />
            <input
              className="input"
              value={pair.right}
              placeholder={t("questions.right_item")}
              onChange={(e) => {
                const next = [...pairs];
                next[index] = { ...next[index], right: e.target.value };
                set({ pairs: next });
              }}
            />
            <button
              type="button"
              className="btn ghost"
              onClick={() => set({ pairs: pairs.filter((_, i) => i !== index) })}
              disabled={pairs.length <= 2}
            >
              {t("questions.remove")}
            </button>
          </div>
        ))}
        <div className="row">
          <button
            type="button"
            className="btn secondary"
            onClick={() => set({ pairs: [...pairs, { left: "", right: "" }] })}
            disabled={pairs.length >= 40}
          >
            {t("questions.add_pair")}
          </button>
        </div>
        <div className="field">
          <label>{t("questions.extra_rights")}</label>
          {textList(extraRights, "extra_rights", t("questions.distractor_placeholder"), { max: 40 })}
        </div>
      </div>
    );
  }

  if (type === "ordering") {
    return (
      <div className="stack">
        <p className="small muted">{t("questions.ordering_hint")}</p>
        {items.map((item, index) => (
          <div className="row" key={index}>
            <span className="small muted">{index + 1}</span>
            <input
              className="input"
              value={item}
              onChange={(e) => {
                const next = [...items];
                next[index] = e.target.value;
                set({ items: next });
              }}
            />
            <button
              type="button"
              className="btn ghost"
              disabled={index === 0}
              onClick={() => {
                const next = [...items];
                [next[index - 1], next[index]] = [next[index], next[index - 1]];
                set({ items: next });
              }}
            >
              ↑
            </button>
            <button
              type="button"
              className="btn ghost"
              disabled={index >= items.length - 1}
              onClick={() => {
                const next = [...items];
                [next[index + 1], next[index]] = [next[index], next[index + 1]];
                set({ items: next });
              }}
            >
              ↓
            </button>
            <button
              type="button"
              className="btn ghost"
              onClick={() => set({ items: items.filter((_, i) => i !== index) })}
              disabled={items.length <= 2}
            >
              {t("questions.remove")}
            </button>
          </div>
        ))}
        <div className="row">
          <button
            type="button"
            className="btn secondary"
            onClick={() => set({ items: [...items, ""] })}
            disabled={items.length >= 60}
          >
            {t("questions.add_item")}
          </button>
        </div>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.separator")}</label>
            <input
              className="input"
              value={config.separator ?? " "}
              onChange={(e) => set({ separator: e.target.value })}
            />
          </div>
          <label className="small">
            <input
              type="checkbox"
              checked={!!config.show_separator}
              onChange={(e) => set({ show_separator: e.target.checked })}
            />{" "}
            {t("questions.show_separator")}
          </label>
        </div>
      </div>
    );
  }

  if (type === "translation") {
    return (
      <div className="stack">
        <div className="field">
          <label>{t("questions.source_text")}</label>
          <textarea
            className="input"
            rows={3}
            value={config.source || ""}
            onChange={(e) => set({ source: e.target.value })}
          />
        </div>
        <div className="field">
          <label>{t("questions.accepted")}</label>
          {textList(accepted, "accepted", t("questions.accepted_placeholder"), { max: 40 })}
        </div>
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.source_language")}</label>
            <input
              className="input"
              value={config.source_language || ""}
              onChange={(e) => set({ source_language: e.target.value || null })}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.target_language")}</label>
            <input
              className="input"
              value={config.target_language || ""}
              onChange={(e) => set({ target_language: e.target.value || null })}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.max_words")}</label>
            <input
              className="input"
              type="number"
              min={1}
              value={config.max_words ?? ""}
              onChange={(e) => set({ max_words: e.target.value === "" ? null : Number(e.target.value) })}
            />
          </div>
        </div>
        {normalizationEditor()}
      </div>
    );
  }

  if (type === "essay") {
    return (
      <div className="stack">
        <div className="row">
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.min_words")}</label>
            <input
              className="input"
              type="number"
              min={0}
              value={config.min_words ?? ""}
              onChange={(e) => set({ min_words: e.target.value === "" ? null : Number(e.target.value) })}
            />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>{t("questions.max_words")}</label>
            <input
              className="input"
              type="number"
              min={1}
              value={config.max_words ?? ""}
              onChange={(e) => set({ max_words: e.target.value === "" ? null : Number(e.target.value) })}
            />
          </div>
        </div>
        <div className="field">
          <label>{t("questions.guidance")}</label>
          <textarea
            className="input"
            rows={3}
            value={config.guidance || ""}
            onChange={(e) => set({ guidance: e.target.value || null })}
          />
        </div>
        <div className="field">
          <label>{t("questions.rubric")}</label>
          <div className="stack">
            {rubric.map((criterion, index) => (
              <div className="row" key={index}>
                <input
                  className="input"
                  placeholder={t("questions.criterion_name")}
                  value={criterion.name}
                  onChange={(e) => {
                    const next = [...rubric];
                    next[index] = { ...next[index], name: e.target.value };
                    set({ rubric: next });
                  }}
                />
                <input
                  className="input"
                  style={{ maxWidth: 110 }}
                  type="number"
                  min={0.01}
                  step="0.25"
                  placeholder={t("questions.criterion_max")}
                  value={criterion.max_score ?? ""}
                  onChange={(e) => {
                    const next = [...rubric];
                    next[index] = {
                      ...next[index],
                      max_score: e.target.value === "" ? null : Number(e.target.value),
                    };
                    set({ rubric: next });
                  }}
                />
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => set({ rubric: rubric.filter((_, i) => i !== index) })}
                >
                  {t("questions.remove")}
                </button>
              </div>
            ))}
            <div>
              <button
                type="button"
                className="btn secondary"
                onClick={() => set({ rubric: [...rubric, { name: "" }] })}
                disabled={rubric.length >= 20}
              >
                {t("questions.add_criterion")}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return <p className="muted">{t("questions.unknown_type")}</p>;

  function normalizationEditor() {
    return (
      <div className="field">
        <label>{t("questions.normalization")}</label>
        <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
          {NORMALIZATION_KEYS.map((key) => (
            <label className="small" key={key}>
              <input
                type="checkbox"
                checked={norm[key]}
                onChange={(e) => {
                  const next = { ...norm, [key]: e.target.checked };
                  const diff = normalizationDiff({ normalization: next });
                  if (diff) set({ normalization: diff });
                  else {
                    const copy: Config = { ...config };
                    delete copy.normalization;
                    onChange(copy);
                  }
                }}
              />{" "}
              {t(`questions.norm_${key}`)}
            </label>
          ))}
        </div>
      </div>
    );
  }
}
