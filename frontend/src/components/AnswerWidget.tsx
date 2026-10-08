// The interactive half of a learner's question: the same public projection the read-only
// preview renders, but with inputs instead of pictures of inputs.
//
// Two rules keep this honest. The payload shape is the grader's, not this file's - each
// kind builds exactly the body `question_engine` reads (`{"option_index"}`, `{"value"}`,
// `{"blanks"}`, `[{left_ref,right_ref}]`, …), so a widget cannot invent a key the server
// would grade as an empty answer. And nothing here decides whether the learner was right:
// `result` comes back from `/student/practice/answer`, and when the catalog withholds
// feedback the widget says so instead of showing a blank where a mark belongs.
//
// It serves exam sittings as well as practice runs, which is what the two extra props are for.
// An exam line is keyed by `stepKey` (`exam_item_id`) rather than by the question, because the
// paper asks for the version it pinned and a resumed sitting has to land back on the same line;
// `initial` is the learner's own last answer coming back from the server, so a reloaded tab shows
// what was already typed instead of an empty box that invites them to answer twice.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { mediaUrl } from "../api/client";
import type { LearnerView } from "../api/questions";
import type { AnswerPayload } from "../api/practice";

type Option = { index: number; text: string };
type Ref = { ref: string; text: string };
type Blank = { index: number; label: string | null };

/** Whatever the server said about the answer just sent. Practice returns an explanation with it;
 * an exam line returns nothing extra when the paper withholds the key, and this type is the only
 * place the two shapes are allowed to meet. */
export type AnswerVerdict = {
  withheld: boolean;
  correct: boolean | null;
  score: number | null;
  max_score: number | null;
  requires_manual: boolean;
  explanation?: string | null;
};

/** The learner's own last answer, read defensively. It was written against the version this line
 * is pinned to, so a shape this widget does not recognise shows an empty box rather than half of
 * an answer - and never a payload the grader would not have accepted. */
function seedOf(value: unknown) {
  const saved = (value ?? {}) as Record<string, any>;
  const pairs = Array.isArray(saved.pairs)
    ? saved.pairs
        .filter((pair: any) => pair && typeof pair.left_ref === "string")
        .reduce((acc: Record<string, string>, pair: any) => {
          acc[pair.left_ref] = String(pair.right_ref ?? "");
          return acc;
        }, {})
    : null;
  return {
    option: typeof saved.option_index === "number" ? (saved.option_index as number) : null,
    options: Array.isArray(saved.option_indexes) ? (saved.option_indexes as number[]) : null,
    value: typeof saved.value === "boolean" ? (saved.value as boolean) : null,
    text: typeof saved.text === "string" ? (saved.text as string) : null,
    blanks: Array.isArray(saved.blanks) ? saved.blanks.map((entry: unknown) => String(entry ?? "")) : null,
    pairs: pairs as Record<string, string> | null,
    order: Array.isArray(saved.order) ? saved.order.map((entry: unknown) => String(entry ?? "")) : null,
  };
}

export default function AnswerWidget({
  view,
  onSubmit,
  result,
  busy,
  stepKey,
  initial,
  heading,
}: {
  view: LearnerView;
  onSubmit: (payload: AnswerPayload) => void;
  result: AnswerVerdict | null;
  busy: boolean;
  /** Defaults to the question's own id. An exam passes its line id instead, which is the only key
   * that stays correct when a paper holds two versions of the same question. */
  stepKey?: string;
  /** `step.saved` - the answer already on this line, in the payload shape above. */
  initial?: unknown;
  /** The word for this line in the screen's own header. Practice calls it an exercise; a paper
   * numbers its questions, and the number comes from the runner, which knows the order. */
  heading?: string;
}) {
  const { t } = useTranslation();
  const config = (view.config || {}) as Record<string, any>;
  const kind = String(config.kind || "");
  const context = view.context || {};
  const reading = context.reading as Record<string, any> | undefined;
  const listening = context.listening as Record<string, any> | undefined;
  const media = view.media;
  const src = mediaUrl(media?.content_url);
  const contextAudio = mediaUrl((listening?.audio as Record<string, any> | undefined)?.content_url);

  const [single, setSingle] = useState<number | null>(null);
  const [multi, setMulti] = useState<number[]>([]);
  const [booleanValue, setBooleanValue] = useState<boolean | null>(null);
  const [text, setText] = useState("");
  const [blanks, setBlanks] = useState<string[]>([]);
  const [pairs, setPairs] = useState<Record<string, string>>({});
  const [order, setOrder] = useState<string[]>([]);

  const options = ((config.options || []) as Option[]).slice();
  const lefts = ((config.lefts || []) as Ref[]).slice();
  const rights = ((config.rights || []) as Ref[]).slice();
  const tokens = ((config.tokens || []) as Ref[]).slice();
  const blankCount = ((config.blanks || []) as Blank[]).length;
  // The refs are what the order is made of, and their joined form is the only thing stable
  // enough to hang an effect on: `config` is a new object on every render, so an effect that
  // depended on it would re-run, re-set state, and render forever.
  const tokenKey = tokens.map((token) => token.ref).join(",");

  // The line this widget belongs to. Practice steps name the question; an exam step names its own
  // row, because a paper holds a version rather than the bank's current one.
  const lineKey = stepKey || view.id;
  // A value rather than the object it came from: a refetch hands back the same answer inside a new
  // wrapper, and re-seeding on object identity would wipe what the learner is typing right now.
  const seedKey = JSON.stringify(initial ?? null);

  // A step navigated away from and back to must not keep the previous line's answer in its inputs,
  // and a resumed sitting must not show an empty box where the learner's own words are stored.
  useEffect(() => {
    const seed = seedOf(initial);
    setSingle(seed.option);
    setMulti(seed.options ?? []);
    setBooleanValue(seed.value);
    setText(seed.text ?? "");
    setPairs(seed.pairs ?? {});
    setBlanks(seed.blanks && seed.blanks.length === blankCount ? seed.blanks : Array(blankCount).fill(""));
  }, [lineKey, seedKey, blankCount]);

  // The tokens arrive in the order the engine published them, which is deliberately not the
  // answer; a learner who already started keeps the sequence they made, and anyone else begins
  // from exactly what the server published.
  useEffect(() => {
    const seed = seedOf(initial);
    setOrder(seed.order && seed.order.length === tokens.length ? seed.order : tokens.map((token) => token.ref));
  }, [lineKey, tokenKey, seedKey]);

  const matchingComplete = lefts.length > 0 && lefts.every((item) => pairs[item.ref]);
  const chosen = Object.values(pairs);
  const matchingUsable = matchingComplete && new Set(chosen).size === chosen.length;

  const payload = (): AnswerPayload | null => {
    if (kind === "options") {
      if (config.multi) return multi.length ? { option_indexes: [...multi].sort((a, b) => a - b) } : null;
      return single === null ? null : { option_index: single };
    }
    if (kind === "boolean") return booleanValue === null ? null : { value: booleanValue };
    if (kind === "text" || kind === "essay") return text.trim() ? { text } : null;
    if (kind === "blanks") return blanks.some((value) => value.trim()) ? { blanks } : null;
    if (kind === "matching") return matchingUsable ? { pairs: lefts.map((item) => ({ left_ref: item.ref, right_ref: pairs[item.ref] })) } : null;
    if (kind === "ordering") return order.length === tokens.length && order.length > 0 ? { order } : null;
    return null;
  };

  const body = payload();

  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    setOrder((prev) => {
      const next = prev.slice();
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const fillBlank = (word: string) => {
    setBlanks((prev) => {
      const next = prev.slice();
      const empty = next.findIndex((value) => !value.trim());
      if (empty === -1) return prev;
      next[empty] = word;
      return next;
    });
  };

  return (
    <div className="card stack">
      <div className="row small muted">
        <span>{heading || t("practice.exercise")}</span>
        <span className="spacer" />
        <span>{t("practice.points_n", { n: view.score })}</span>
      </div>

      {reading ? (
        <div className="stack">
          <strong>{reading.title}</strong>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.7 }}>{reading.body}</div>
        </div>
      ) : null}
      {listening ? (
        <div className="stack">
          <strong>{listening.title}</strong>
          {listening.block_instructions ? (
            <p className="small muted" style={{ margin: 0 }}>
              {listening.block_instructions}
            </p>
          ) : null}
          {/* A question bound to a recording is useless without it, and the address comes
              from the same context the server built for this learner. */}
          {contextAudio ? <audio src={contextAudio} controls style={{ width: "100%" }} /> : null}
          {contextAudio && listening.show_transcript && listening.transcript ? (
            <div className="small" style={{ whiteSpace: "pre-wrap" }}>
              {listening.transcript}
            </div>
          ) : null}
          {!contextAudio ? <span className="small muted">{t("listening.no_audio_for_you")}</span> : null}
          {/* A paper can ask about one part of a recording. The slice the teacher cut is the one
              frozen in the sitting, so the learner is told which part they are being asked about. */}
          {listening.start_seconds !== null && listening.start_seconds !== undefined ? (
            <span className="small muted">
              {t("listening.block_slice", {
                from: listening.start_seconds,
                to: listening.end_seconds ?? "end",
              })}
            </span>
          ) : null}
          {listening.replay_limit !== null && listening.replay_limit !== undefined ? (
            <span className="small muted">{t("questions.replay_limit", { n: listening.replay_limit })}</span>
          ) : null}
        </div>
      ) : null}

      {view.prompt ? <p style={{ margin: 0 }}>{view.prompt}</p> : null}

      {media && src ? (
        media.kind === "image" ? (
          <img src={src} alt="" style={{ maxWidth: "100%" }} />
        ) : media.kind === "video" ? (
          <video src={src} controls style={{ width: "100%" }} />
        ) : (
          <audio src={src} controls style={{ width: "100%" }} />
        )
      ) : null}
      {media && !src ? <p className="small muted">{t("media.not_available")}</p> : null}

      {kind === "options" ? (
        <ul className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {options.map((option) => {
            const picked = config.multi ? multi.includes(option.index) : single === option.index;
            return (
              <li key={option.index}>
                <button
                  type="button"
                  className="choice"
                  style={{ width: "100%", textAlign: "left", minHeight: 44, borderColor: picked ? "var(--accent)" : undefined }}
                  aria-pressed={picked}
                  onClick={() =>
                    config.multi
                      ? setMulti((prev) => (prev.includes(option.index) ? prev.filter((i) => i !== option.index) : [...prev, option.index]))
                      : setSingle(option.index)
                  }
                >
                  <span className="choice-mark">
                    {config.multi ? (picked ? "☑" : "☐") : picked ? "●" : "○"}{" "}
                    {String.fromCharCode(65 + (option.index % 26))}
                  </span>{" "}
                  {option.text}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}

      {kind === "boolean" ? (
        <div className="stack">
          {config.statement ? <p style={{ margin: 0 }}>{String(config.statement)}</p> : null}
          <div className="row">
            {[true, false].map((value) => (
              <button
                key={String(value)}
                type="button"
                className="btn secondary"
                aria-pressed={booleanValue === value}
                style={{ borderColor: booleanValue === value ? "var(--accent)" : undefined, minHeight: 44 }}
                onClick={() => setBooleanValue(value)}
              >
                {value ? t("questions.true") : t("questions.false")}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {kind === "text" ? (
        <div className="stack">
          {config.source ? <p style={{ margin: 0 }}>{String(config.source)}</p> : null}
          <input
            className="input"
            value={text}
            maxLength={config.max_characters ? Number(config.max_characters) : undefined}
            placeholder={t("questions.student_answer_placeholder")}
            onChange={(e) => setText(e.target.value)}
          />
          {config.hint ? <p className="small muted">{String(config.hint)}</p> : null}
          <p className="small muted">
            {config.max_characters ? t("questions.max_characters_note", { n: config.max_characters }) : null}
            {config.max_words ? t("questions.max_words_note", { n: config.max_words }) : null}
          </p>
        </div>
      ) : null}

      {kind === "blanks" ? (
        <div className="stack">
          <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {String(config.text || "").replace(/_{3,}|\{\{\s*\}\}/g, "______")}
          </p>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {((config.blanks || []) as Blank[]).map((blank, index) => (
              <input
                key={blank.index}
                className="input"
                style={{ maxWidth: 160 }}
                value={blanks[index] || ""}
                placeholder={blank.label || String(blank.index + 1)}
                onChange={(e) =>
                  setBlanks((prev) => {
                    const next = prev.slice();
                    next[index] = e.target.value;
                    return next;
                  })
                }
              />
            ))}
          </div>
          {(config.word_bank as string[] | undefined)?.length ? (
            <div className="row" style={{ flexWrap: "wrap" }}>
              {((config.word_bank || []) as string[]).map((word, index) => (
                <button type="button" className="chip" key={index} style={{ minHeight: 32 }} onClick={() => fillBlank(word)}>
                  {word}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {kind === "matching" ? (
        <div className="stack">
          {lefts.map((item) => (
            <div className="row" key={item.ref} style={{ flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <span style={{ flex: "1 1 12rem" }}>{item.text}</span>
              <select
                className="input"
                style={{ maxWidth: 220 }}
                value={pairs[item.ref] || ""}
                onChange={(e) => setPairs((prev) => ({ ...prev, [item.ref]: e.target.value }))}
              >
                <option value="">{t("practice.choose_match")}</option>
                {rights.map((right) => (
                  <option key={right.ref} value={right.ref}>
                    {right.text}
                  </option>
                ))}
              </select>
            </div>
          ))}
          {matchingComplete && !matchingUsable ? (
            <p className="small muted">{t("practice.match_reused")}</p>
          ) : null}
        </div>
      ) : null}

      {kind === "ordering" ? (
        <div className="stack">
          <p className="small muted" style={{ margin: 0 }}>
            {config.show_separator && config.separator ? t("practice.order_separator", { s: String(config.separator) }) : t("practice.order_hint")}
          </p>
          <div className="stack">
            {order.map((ref, index) => (
              <div className="row" key={ref} style={{ gap: 6 }}>
                <span className="small muted" style={{ minWidth: 18 }}>
                  {index + 1}
                </span>
                <span className="chip" style={{ flex: 1 }}>
                  {tokens.find((token) => token.ref === ref)?.text || ""}
                </span>
                <button type="button" className="btn ghost" disabled={index === 0} onClick={() => move(index, -1)} aria-label={t("practice.move_up")}>
                  ↑
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  disabled={index === order.length - 1}
                  onClick={() => move(index, 1)}
                  aria-label={t("practice.move_down")}
                >
                  ↓
                </button>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {kind === "essay" ? (
        <div className="stack">
          {config.guidance ? <p className="small muted" style={{ margin: 0 }}>{String(config.guidance)}</p> : null}
          <textarea
            className="input"
            rows={6}
            value={text}
            placeholder={t("questions.student_answer_placeholder")}
            onChange={(e) => setText(e.target.value)}
          />
          <p className="small muted">
            {[
              config.min_words ? t("questions.min_words_note", { n: config.min_words }) : null,
              config.max_words ? t("questions.max_words_note", { n: config.max_words }) : null,
            ]
              .filter(Boolean)
              .join(" ")}
          </p>
          {((config.rubric || []) as { name: string; max_score?: number; guidance?: string }[]).map((criterion, index) => (
            <div className="small" key={index}>
              • {criterion.name}
              {criterion.max_score ? ` (${criterion.max_score})` : ""}
              {criterion.guidance ? <div className="muted">{criterion.guidance}</div> : null}
            </div>
          ))}
        </div>
      ) : null}

      {view.requires_manual_grading ? <p className="small muted">{t("questions.manual_note")}</p> : null}

      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <button type="button" className="btn" disabled={busy || !body} onClick={() => body && onSubmit(body)} style={{ minHeight: 44 }}>
          {busy ? t("common.loading") : result ? t("practice.send_again") : t("practice.send_answer")}
        </button>
        {!body ? <span className="small muted">{t("practice.answer_first")}</span> : null}
      </div>

      {result ? (
        result.withheld ? (
          <p className="small muted">{t("practice.held_until_end")}</p>
        ) : (
          <div className="stack">
            <div className="row small" style={{ gap: 8 }}>
              {result.requires_manual ? (
                <span className="chip">{t("practice.not_auto_marked")}</span>
              ) : result.correct === true ? (
                <span className="chip">{t("practice.right")}</span>
              ) : result.correct === false ? (
                <span className="chip">{t("practice.wrong")}</span>
              ) : (
                // No verdict and no teacher in the loop: the answer is filed, and nothing
                // here may claim a mark the server did not give.
                <span className="muted">{t("practice.not_marked")}</span>
              )}
              {result.max_score !== null ? (
                <span className="muted">
                  {t("practice.score_of", { got: result.score ?? 0, total: result.max_score })}
                </span>
              ) : null}
            </div>
            {result.explanation ? <p className="small" style={{ margin: 0 }}>{result.explanation}</p> : null}
          </div>
        )
      ) : null}

      {/* Named only so the widget can tell the learner that a verdict exists somewhere: the
          text itself arrives with the run, never on the question. */}
      {view.explanation_available && !result ? <p className="small muted">{t("questions.explanation_note")}</p> : null}
    </div>
  );
}
