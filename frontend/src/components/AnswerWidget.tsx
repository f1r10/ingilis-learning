// The interactive half of a learner's question: the same public projection the read-only
// preview renders, but with inputs instead of pictures of inputs.
//
// Two rules keep this honest. The payload shape is the grader's, not this file's - each
// kind builds exactly the body `question_engine` reads (`{"option_index"}`, `{"value"}`,
// `{"blanks"}`, `[{left_ref,right_ref}]`, …), so a widget cannot invent a key the server
// would grade as an empty answer. And nothing here decides whether the learner was right:
// `result` comes back from `/student/practice/answer`, and when the catalog withholds
// feedback the widget says so instead of showing a blank where a mark belongs.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { mediaUrl } from "../api/client";
import type { LearnerView } from "../api/questions";
import type { AnswerPayload, AnswerResult } from "../api/practice";

type Option = { index: number; text: string };
type Ref = { ref: string; text: string };
type Blank = { index: number; label: string | null };

export default function AnswerWidget({
  view,
  onSubmit,
  result,
  busy,
}: {
  view: LearnerView;
  onSubmit: (payload: AnswerPayload) => void;
  result: AnswerResult | null;
  busy: boolean;
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

  // A step that is navigated away from and back to must not keep the previous question's
  // answers in its inputs, so the local state is reset whenever the question changes.
  useEffect(() => {
    setSingle(null);
    setMulti([]);
    setBooleanValue(null);
    setText("");
    setBlanks([]);
    setPairs({});
  }, [view.id]);

  useEffect(() => {
    if (blankCount) setBlanks((prev) => (prev.length === blankCount ? prev : Array(blankCount).fill("")));
  }, [view.id, blankCount]);

  // The tokens arrive in the order the engine published them, which is deliberately not the
  // answer; the learner starts from exactly that sequence.
  useEffect(() => {
    setOrder(tokens.map((token) => token.ref));
  }, [view.id, tokenKey]);

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
        <span>{t("practice.exercise")}</span>
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
          {/* A question bound to a recording is useless without it, and the address comes
              from the same context the server built for this learner. */}
          {contextAudio ? <audio src={contextAudio} controls style={{ width: "100%" }} /> : null}
          {contextAudio && listening.show_transcript && listening.transcript ? (
            <div className="small" style={{ whiteSpace: "pre-wrap" }}>
              {listening.transcript}
            </div>
          ) : null}
          {!contextAudio ? <span className="small muted">{t("listening.no_audio_for_you")}</span> : null}
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
