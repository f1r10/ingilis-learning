// The card exactly as the backend projects it for a learner: no private note, no
// provenance, no lifecycle state. The teacher's preview panel and the student's own
// screen render this one component, so what the teacher sees is what the student gets.
import { useTranslation } from "react-i18next";
import { mediaUrl } from "../api/client";
import type { StudyCard } from "../api/vocabulary";

export default function StudentCard({ card }: { card: StudyCard }) {
  const { t } = useTranslation();
  const audio = mediaUrl(card.audio_url);
  return (
    <div className="card stack">
      <div className="row">
        <strong style={{ fontSize: "1.2rem" }}>{card.word}</strong>
        {card.ipa ? <span className="small muted">{card.ipa}</span> : null}
        <span className="spacer" />
        {card.part_of_speech ? <span className="chip">{card.part_of_speech}</span> : null}
        {card.level ? <span className="chip">{card.level}</span> : null}
      </div>

      {/* The address is written by the server and a student session authorises its bytes,
          so the card plays the library's file without ever naming the store. */}
      {card.has_audio && audio ? <audio src={audio} controls style={{ width: "100%" }} /> : null}
      {card.has_audio && !audio ? <p className="small muted">{t("media.not_available")}</p> : null}

      {card.definition ? <p style={{ margin: 0 }}>{card.definition}</p> : null}

      {card.translations.length ? (
        <div className="stack">
          {card.translations.map((row) => (
            <div className="row" key={row.id ?? row.language}>
              <span className="small muted" style={{ minWidth: 28 }}>
                {row.language}
              </span>
              <span>{row.value}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="small muted">{t("vocabulary.no_meanings_yet")}</p>
      )}

      {card.examples.length ? (
        <div className="stack">
          {card.examples.map((row) => (
            <div className="small" key={row.id ?? row.sentence}>
              <div>{row.sentence}</div>
              {row.translation ? <div className="muted">{row.translation}</div> : null}
            </div>
          ))}
        </div>
      ) : null}

      {card.synonyms.length || card.antonyms.length ? (
        <div className="row small" style={{ flexWrap: "wrap", gap: 12 }}>
          {card.synonyms.length ? (
            <span>
              <span className="muted">{t("vocabulary.synonyms")}: </span>
              {card.synonyms.join(", ")}
            </span>
          ) : null}
          {card.antonyms.length ? (
            <span>
              <span className="muted">{t("vocabulary.antonyms")}: </span>
              {card.antonyms.join(", ")}
            </span>
          ) : null}
        </div>
      ) : null}

      {card.tags.length ? (
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          {card.tags.map((tag) => (
            <span className="chip" key={tag.id}>
              {tag.name}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
