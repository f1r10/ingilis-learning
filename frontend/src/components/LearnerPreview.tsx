// What a learner receives: `/questions/{id}/preview` already has the answer key
// removed, and this component only renders that payload. It is deliberately the
// same view the exam runner will use later, so a question that looks wrong here
// looks wrong to the student too.
import { useTranslation } from "react-i18next";
import { mediaUrl } from "../api/client";
import type { LearnerView } from "../api/questions";

export default function LearnerPreview({ view }: { view: LearnerView }) {
  const { t } = useTranslation();
  const config = (view.config || {}) as Record<string, any>;
  const kind = String(config.kind || "");
  const context = view.context || {};
  const reading = context.reading as Record<string, any> | undefined;
  const listening = context.listening as Record<string, any> | undefined;
  const media = view.media;
  const src = mediaUrl(media?.content_url);

  return (
    <div className="card stack">
      <div className="row small muted">
        <span>{t("questions.learner_view")}</span>
        <span className="spacer" />
        <span>{view.score}</span>
      </div>

      {reading ? (
        <div className="stack">
          <strong>{reading.title}</strong>
          <div style={{ whiteSpace: "pre-wrap" }}>{reading.body}</div>
        </div>
      ) : null}
      {listening ? (
        <div className="stack">
          <strong>{listening.title}</strong>
          <span className="small muted">
            {t("questions.replay_limit", { n: listening.replay_limit ?? 0 })}
          </span>
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
          {((config.options || []) as { index: number; text: string }[]).map((option) => (
            <li key={option.index} className="choice">
              <span className="choice-mark">
                {config.multi ? "☐" : "○"} {String.fromCharCode(65 + (option.index % 26))}
              </span>{" "}
              {option.text}
            </li>
          ))}
        </ul>
      ) : null}

      {kind === "boolean" ? (
        <div className="stack">
          {config.statement ? <p style={{ margin: 0 }}>{String(config.statement)}</p> : null}
          <div className="row">
            <span className="btn secondary">{t("questions.true")}</span>
            <span className="btn secondary">{t("questions.false")}</span>
          </div>
        </div>
      ) : null}

      {kind === "text" ? (
        <div className="stack">
          {config.source ? <p style={{ margin: 0 }}>{String(config.source)}</p> : null}
          <input className="input" placeholder={t("questions.student_answer_placeholder")} readOnly />
          {config.hint ? <p className="small muted">{String(config.hint)}</p> : null}
          <p className="small muted">
            {config.max_characters
              ? t("questions.max_characters_note", { n: config.max_characters })
              : null}
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
            {(((config.blanks || []) as { index: number; label: string | null }[])).map((blank) => (
              <input
                key={blank.index}
                className="input"
                style={{ maxWidth: 160 }}
                placeholder={blank.label || String(blank.index + 1)}
                readOnly
              />
            ))}
          </div>
          {(config.word_bank as string[] | undefined)?.length ? (
            <div className="row" style={{ flexWrap: "wrap" }}>
              {((config.word_bank || []) as string[]).map((word, index) => (
                <span className="chip" key={index}>
                  {word}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {kind === "matching" ? (
        <div className="two-col">
          <div className="stack">
            {((config.lefts || []) as { ref: string; text: string }[]).map((item) => (
              <div className="choice" key={item.ref}>
                {item.text}
              </div>
            ))}
          </div>
          <div className="stack">
            {((config.rights || []) as { ref: string; text: string }[]).map((item) => (
              <div className="choice" key={item.ref}>
                {item.text}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {kind === "ordering" ? (
        <div className="row" style={{ flexWrap: "wrap" }}>
          {((config.tokens || []) as { ref: string; text: string }[]).map((token) => (
            <span className="chip" key={token.ref}>
              {token.text}
            </span>
          ))}
        </div>
      ) : null}

      {kind === "essay" ? (
        <div className="stack">
          <textarea className="input" rows={6} readOnly placeholder={t("questions.student_answer_placeholder")} />
          <p className="small muted">
            {[
              config.min_words ? t("questions.min_words_note", { n: config.min_words }) : null,
              config.max_words ? t("questions.max_words_note", { n: config.max_words }) : null,
            ]
              .filter(Boolean)
              .join(" ")}
          </p>
          {((config.rubric || []) as { name: string; max_score?: number }[]).map((criterion, index) => (
            <div className="small" key={index}>
              • {criterion.name}
              {criterion.max_score ? ` (${criterion.max_score})` : ""}
            </div>
          ))}
        </div>
      ) : null}

      {view.requires_manual_grading ? (
        <p className="small muted">{t("questions.manual_note")}</p>
      ) : null}
      {view.explanation_available ? (
        <p className="small muted">{t("questions.explanation_note")}</p>
      ) : null}
    </div>
  );
}
