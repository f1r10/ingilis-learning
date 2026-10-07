// A learner's recording: play it under the rules the teacher set, read the transcript only
// if it was offered, and see what each block asks.
//
// The rules are applied here because they are part of the exercise, not decoration: a
// replay limit the player ignores is a different lesson from the one the teacher made. The
// bytes come from `content_url`, which the application serves under the learner's own
// session - the object store is never named in this file.
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError, mediaUrl } from "../api/client";
import LearnerPreview from "../components/LearnerPreview";
import { learnerListeningApi } from "../api/listening";

const PAGE_SIZE = 20;

export default function StudentListening() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();

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

  const openId = params.get("recording") || "";

  const setFilter = (patch: Record<string, string>) => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => {
      if (!value) next.delete(key);
      else next.set(key, value);
    });
    if (!("page" in patch)) next.delete("page");
    setParams(next);
  };

  const meta = useQuery({ queryKey: ["student-listening-meta"], queryFn: learnerListeningApi.meta });
  const list = useQuery({ queryKey: ["student-listening", filters], queryFn: () => learnerListeningApi.list(filters) });
  const detail = useQuery({
    queryKey: ["student-listening-one", openId],
    queryFn: () => learnerListeningApi.get(openId),
    enabled: Boolean(openId),
  });

  if (openId) {
    if (detail.isError) {
      return (
        <div className="stack">
          <button className="btn secondary" onClick={() => setFilter({ recording: "" })}>
            ‹ {t("listening.back_to_list")}
          </button>
          <div className="alert error">{t("common.could_not_load")} {(detail.error as ApiError).message}</div>
        </div>
      );
    }
    if (!detail.data) return <div className="card muted">{t("common.loading")}</div>;
    return <Recording page={detail.data} onBack={() => setFilter({ recording: "" })} />;
  }

  const rows = list.data?.items || [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="stack">
      <h1 style={{ margin: 0 }}>{t("listening.my_listenings")}</h1>
      <p className="muted small">{t("listening.my_listenings_hint")}</p>

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("listening.search")}
          value={filters.q}
          onChange={(e) => setFilter({ q: e.target.value })}
        />
        <select className="input" style={{ maxWidth: 120 }} value={filters.level} onChange={(e) => setFilter({ level: e.target.value })}>
          <option value="">{t("listening.all_levels")}</option>
          {(meta.data?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={filters.language} onChange={(e) => setFilter({ language: e.target.value })}>
          <option value="">{t("listening.all_languages")}</option>
          {(meta.data?.learning_languages || []).map((code) => (
            <option key={code} value={code}>
              {code === i18n.language ? `${code} · ${t("vocabulary.your_language")}` : code}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 170 }} value={filters.sort} onChange={(e) => setFilter({ sort: e.target.value })}>
          {(meta.data?.sortable || []).map((sort) => (
            <option key={sort} value={sort}>
              {t(`listening.sort_${sort}`)}
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
            onClick={() => setFilter({ recording: row.id })}
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
            <div className="row small muted" style={{ marginTop: 4 }}>
              <span className="chip">{row.level || "—"}</span>
              {row.has_audio ? (
                <span>{row.duration_seconds ? t("listening.seconds", { n: Math.round(row.duration_seconds) }) : t("listening.has_audio")}</span>
              ) : (
                <span className="chip">{t("listening.no_audio")}</span>
              )}
              <span>{t("listening.questions_n", { n: row.question_count })}</span>
              <span className="spacer" />
              {row.show_transcript ? <span className="muted">{t("listening.transcript_shown")}</span> : null}
            </div>
          </button>
        ))}
        {!list.isLoading && !list.isError && rows.length === 0 ? (
          <div className="muted small">{t("listening.nothing_for_you")}</div>
        ) : null}
      </div>

      <div className="row">
        <span className="small muted">{t("listening.showing", { total })}</span>
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

type RecordingPage = NonNullable<Awaited<ReturnType<typeof learnerListeningApi.get>>>;

function Recording({ page, onBack }: { page: RecordingPage; onBack: () => void }) {
  const { t } = useTranslation();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [plays, setPlays] = useState(0);
  const [stopped, setStopped] = useState(false);
  const [slice, setSlice] = useState<{ start: number; end: number | null } | null>(null);
  const src = mediaUrl(page.audio?.content_url);
  const full = Boolean(src) && !slice;
  const rules = {
    allowPause: page.allow_pause,
    allowSeek: page.allow_seek,
  };

  // The limit counts replays, so the first listening is always allowed and the budget is
  // `1 + replay_limit` plays in total. `plays` counts the listenings that have already
  // started, so the last allowed one is the point where the budget runs out: comparing
  // with `>` would hand the learner one replay more than the teacher wrote.
  const usedUp = page.replay_limit !== null && plays >= page.replay_limit + 1;

  useEffect(() => {
    const element = audioRef.current;
    if (!element || !slice) return;
    const stop = () => {
      if (slice.end !== null && element.currentTime >= slice.end) {
        element.pause();
        setStopped(true);
        setSlice(null);
      }
    };
    element.addEventListener("timeupdate", stop);
    return () => element.removeEventListener("timeupdate", stop);
  }, [slice]);

  const playSlice = (start: number, end: number | null) => {
    const element = audioRef.current;
    if (!element) return;
    setSlice({ start, end });
    setStopped(false);
    element.currentTime = start;
    void element.play();
  };

  const onPlay = () => setPlays((value) => value + 1);
  const onPause = () => {
    if (rules.allowPause) return;
    const element = audioRef.current;
    // No pause control was offered, so a pause that was not asked for is resumed: the
    // teacher's rule is that this recording runs through.
    if (element && !element.ended) void element.play();
  };

  return (
    <div className="stack">
      <div className="row">
        <button className="btn secondary" onClick={onBack}>
          ‹ {t("listening.back_to_list")}
        </button>
      </div>
      <h1 style={{ margin: 0 }}>{page.title}</h1>
      <div className="small muted">
        {page.level || "—"} · {t("listening.plays", { n: plays })}
        {page.replay_limit !== null ? ` · ${t("questions.replay_limit", { n: page.replay_limit })}` : ""}
      </div>

      {src ? (
        <div className="card stack">
          {usedUp ? (
            <div className="alert">{t("listening.no_plays_left")}</div>
          ) : null}
          <audio
            ref={audioRef}
            src={full ? src : undefined}
            // A seek bar and a pause button are only drawn when the rules allow them: a
            // control the learner is not allowed to use is not a control worth showing.
            // Once the replays are spent the controls go too - a player that invites a
            // fourth listening under a notice that says there is none left is not a rule,
            // it is a decoration.
            controls={full && rules.allowSeek && rules.allowPause && !usedUp}
            onPlay={onPlay}
            onPause={onPause}
            style={{ width: "100%" }}
          />
          {!full || !rules.allowSeek || !rules.allowPause ? (
            <div className="row" style={{ gap: 6 }}>
              <button
                className="btn"
                disabled={usedUp}
                onClick={() => {
                  const element = audioRef.current;
                  if (!element) return;
                  if (slice) element.currentTime = slice.start;
                  void element.play();
                }}
              >
                {t("listening.play")}
              </button>
              {!rules.allowPause ? <span className="small muted">{t("listening.no_pause")}</span> : null}
              {!rules.allowSeek ? <span className="small muted">{t("listening.no_seek")}</span> : null}
              {stopped ? <span className="small muted">{t("listening.part_finished")}</span> : null}
            </div>
          ) : null}
          <div className="small muted">{t("listening.playback_rules_hint")}</div>
        </div>
      ) : (
        <div className="alert">{t("listening.no_audio_for_you")}</div>
      )}

      {page.show_transcript ? (
        <div className="card stack">
          <strong className="small">{t("listening.transcript")}</strong>
          {page.transcript_timestamps.length ? (
            <div className="stack small">
              {page.transcript_timestamps.map((cue, index) => (
                <div className="row" key={index} style={{ gap: 6, alignItems: "baseline" }}>
                  <span className="muted">{formatSeconds(Number(cue.start ?? 0))}</span>
                  <span style={{ flex: 1 }}>{String(cue.text ?? "")}</span>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ whiteSpace: "pre-wrap" }}>{page.transcript || ""}</div>
          )}
        </div>
      ) : null}

      {page.sets.map((set) => (
        <div className="stack" key={set.id}>
          <div className="row">
            <h3 style={{ margin: 0 }}>{set.title}</h3>
            <span className="spacer" />
            {set.start_seconds !== null ? (
              <button
                className="btn secondary"
                disabled={usedUp || !src}
                onClick={() => playSlice(set.start_seconds as number, set.end_seconds)}
              >
                {t("listening.play_this_part")}
              </button>
            ) : null}
          </div>
          {set.instructions ? <p className="small muted">{set.instructions}</p> : null}
          {set.questions.map((question) => (
            <LearnerPreview key={question.id} view={question} />
          ))}
        </div>
      ))}
      {page.sets.length === 0 ? <div className="muted small">{t("listening.no_exercises")}</div> : null}
    </div>
  );
}

function formatSeconds(value: number) {
  const total = Math.max(0, Math.round(value));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}
