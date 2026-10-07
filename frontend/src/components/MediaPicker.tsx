// Choosing one file from the media library, by id.
//
// Every screen that attaches a file does it this way: the picker lists the library and
// hands back an id, so a browser never gets to name a storage address or a content type.
// The `kind` filter narrows the list to what the calling screen can actually use -
// a listening wants audio, a vocabulary card may want either.
import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { mediaUrl } from "../api/client";
import { mediaApi } from "../api/media";

export interface PickerCopy {
  none: string;
  choose: string;
  detach: string;
  trashed: string;
}

export default function MediaPicker({
  value,
  onChange,
  kind,
  copy,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  kind?: string;
  copy: PickerCopy;
}) {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const [picking, setPicking] = useState(false);
  const chosen = useQuery({
    queryKey: ["media-one", value],
    queryFn: () => mediaApi.get(value as string),
    enabled: Boolean(value),
  });
  const library = useQuery({
    queryKey: ["media", "picker", kind || "any", search],
    queryFn: () =>
      mediaApi.list({
        q: search,
        kind,
        view: "bank",
        page_size: 30,
        sort: "created_at",
        order: "desc",
      }),
    enabled: picking,
  });
  const url = mediaUrl(chosen.data?.content_url);

  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        {chosen.data ? (
          <span className="small">
            {chosen.data.label || chosen.data.original_filename}
            {chosen.data.state === "trashed" ? <span className="chip">{t("media.state_trashed")}</span> : null}
          </span>
        ) : (
          <span className="small muted">{copy.none}</span>
        )}
        <button className="btn secondary" onClick={() => setPicking(!picking)}>
          {copy.choose}
        </button>
        {value ? (
          <button className="btn ghost" onClick={() => onChange(null)}>
            {copy.detach}
          </button>
        ) : null}
      </div>
      {url && chosen.data?.kind === "audio" ? <audio src={url} controls style={{ width: "100%" }} /> : null}
      {url && chosen.data?.kind === "image" ? (
        <img src={url} alt={chosen.data.label || ""} style={{ maxWidth: "100%" }} />
      ) : null}
      {url && chosen.data?.kind === "video" ? <video src={url} controls style={{ width: "100%" }} /> : null}
      {chosen.data?.state === "trashed" ? <div className="small muted">{copy.trashed}</div> : null}
      {picking ? (
        <div className="stack" style={{ gap: 4 }}>
          <input
            className="input"
            style={{ maxWidth: 240 }}
            placeholder={t("media.search")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {(library.data?.items || []).map((asset) => (
            <button
              type="button"
              key={asset.id}
              className="row small"
              style={{ background: "transparent", border: 0, padding: "2px 0", textAlign: "left" }}
              onClick={() => {
                onChange(asset.id);
                setPicking(false);
              }}
            >
              <span style={{ flex: 1 }}>{asset.label || asset.original_filename || asset.id.slice(0, 8)}</span>
              <span className="muted">{asset.duration_seconds ? `${Math.round(asset.duration_seconds)} s` : "—"}</span>
            </button>
          ))}
          {!library.isLoading && (library.data?.items.length ?? 0) === 0 ? (
            <div className="small muted">{t("media.empty")}</div>
          ) : null}
          <Link className="btn ghost" to="/media" style={{ alignSelf: "flex-start" }}>
            {t("nav.media")}
          </Link>
        </div>
      ) : null}
    </div>
  );
}
