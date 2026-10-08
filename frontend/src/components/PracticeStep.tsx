// One step of a practice run, rendered by the kind of content the reference names.
//
// The step's `view` is the bank's own learner projection, so this file decides only how to
// lay that projection out and where the answer inputs go - it never rebuilds a question or
// re-states an answer-key rule. A passage or a recording is a lesson in itself: its blocks
// are shown with their own questions underneath, and each of those is answered through the
// same widget as a standalone exercise.
//
// `sessionId` is null on the teacher's preview, where nothing can be answered: a preview
// writes no run, so there is no token to file an answer against and the widget is replaced
// by the read-only view the author already knows.
import { useTranslation } from "react-i18next";
import { mediaUrl } from "../api/client";
import type { LearnerView } from "../api/questions";
import type { StudyCard } from "../api/vocabulary";
import type { LearnerReading } from "../api/reading";
import type { ListeningLearner } from "../api/listening";
import type { AnswerPayload, AnswerResult, Step } from "../api/practice";
import AnswerWidget from "./AnswerWidget";
import LearnerPreview from "./LearnerPreview";
import StudentCard from "./StudentCard";

export default function PracticeStep({
  step,
  sessionId,
  results,
  busyId,
  onAnswer,
  knownEnabled,
  known,
  onMark,
  favorites,
  onFavorite,
}: {
  step: Step;
  sessionId: string | null;
  results: Record<string, AnswerResult>;
  busyId: string | null;
  onAnswer: (questionId: string, payload: AnswerPayload) => void;
  knownEnabled: boolean;
  known: Record<string, string>;
  onMark: (refId: string, state: string) => void;
  favorites: Record<string, boolean>;
  onFavorite: (kind: string, refId: string, saved: boolean) => void;
}) {
  const { t } = useTranslation();
  const view = step.view as Record<string, any>;

  const question = (item: LearnerView, key: string) =>
    sessionId ? (
      <AnswerWidget
        key={key}
        view={item}
        busy={busyId === item.id}
        result={results[item.id] || null}
        onSubmit={(payload) => onAnswer(item.id, payload)}
      />
    ) : (
      <LearnerPreview key={key} view={item} />
    );

  const favoriteKey = `${step.kind}:${step.ref_id}`;
  const saved = Boolean(favorites[favoriteKey]);

  if (step.kind === "question") {
    return (
      <div className="stack">
        {question(view as unknown as LearnerView, step.item_id)}
        <FavoriteRow
          kind="question"
          refId={step.ref_id}
          saved={saved}
          onToggle={onFavorite}
        />
      </div>
    );
  }

  if (step.kind === "vocabulary") {
    const card = view as unknown as StudyCard;
    const mark = known[step.ref_id] || "";
    return (
      <div className="stack">
        <StudentCard card={card} />
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          {knownEnabled ? (
            <>
              {(["known", "learning"] as const).map((state) => (
                <button
                  key={state}
                  type="button"
                  className="btn secondary"
                  aria-pressed={mark === state}
                  style={{ borderColor: mark === state ? "var(--accent)" : undefined, minHeight: 44 }}
                  onClick={() => onMark(step.ref_id, state)}
                >
                  {t(`practice.mark_${state}`)}
                </button>
              ))}
            </>
          ) : null}
          <FavoriteRow kind="vocabulary" refId={step.ref_id} saved={saved} onToggle={onFavorite} />
        </div>
      </div>
    );
  }

  if (step.kind === "reading") {
    const page = view as unknown as LearnerReading;
    return (
      <div className="stack">
        <div className="card stack">
          <div className="row small muted">
            <strong>{page.title}</strong>
            <span className="spacer" />
            <span className="chip">{page.level || "—"}</span>
          </div>
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.7 }}>{page.body}</div>
        </div>
        {page.sets.map((set) => (
          <div className="stack" key={set.id}>
            {set.title ? <h3 style={{ margin: 0 }}>{set.title}</h3> : null}
            {set.instructions ? <p className="small muted">{set.instructions}</p> : null}
            {set.questions.map((item) => question(item as LearnerView, `${set.id}:${item.id}`))}
          </div>
        ))}
        {page.unfiled.map((item) => question(item as LearnerView, `pool:${item.id}`))}
      </div>
    );
  }

  const recording = view as unknown as ListeningLearner;
  const audio = mediaUrl(recording.audio?.content_url);
  return (
    <div className="stack">
      <div className="card stack">
        <div className="row small muted">
          <strong>{recording.title}</strong>
          <span className="spacer" />
          <span className="chip">{recording.level || "—"}</span>
        </div>
        {recording.audio && audio ? (
          <audio src={audio} controls style={{ width: "100%" }} />
        ) : (
          <p className="small muted">{t("listening.no_audio_for_you")}</p>
        )}
        {recording.replay_limit ? (
          <span className="small muted">{t("questions.replay_limit", { n: recording.replay_limit })}</span>
        ) : null}
        {recording.show_transcript && recording.transcript ? (
          <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.7 }}>{recording.transcript}</div>
        ) : null}
      </div>
      {recording.sets.map((set) => (
        <div className="stack" key={set.id}>
          {set.title ? <h3 style={{ margin: 0 }}>{set.title}</h3> : null}
          {set.instructions ? <p className="small muted">{set.instructions}</p> : null}
          {set.questions.map((item) => question(item as LearnerView, `${set.id}:${item.id}`))}
        </div>
      ))}
      {recording.unfiled.map((item) => question(item as LearnerView, `pool:${item.id}`))}
    </div>
  );
}

function FavoriteRow({
  kind,
  refId,
  saved,
  onToggle,
}: {
  kind: string;
  refId: string;
  saved: boolean;
  onToggle: (kind: string, refId: string, saved: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      className="btn ghost"
      aria-pressed={saved}
      style={{ minHeight: 44 }}
      onClick={() => onToggle(kind, refId, saved)}
    >
      {saved ? t("practice.saved") : t("practice.save_for_later")}
    </button>
  );
}
