// A timestamp a screen shows is copy, not data. `new Date(x).toLocaleString()` with no arguments
// formats in the operating system's language, so a learner reading Azerbaijani would get an
// American-style deadline. The interface already knows which language it is in; dates follow it,
// and they stop at the minute because a paper never opens at 07 seconds past.
import i18n from "./index";

export function when(value: string | null | undefined): string {
  if (!value) return "";
  return new Date(value).toLocaleString(i18n.language, { dateStyle: "medium", timeStyle: "short" });
}

// A duration is copy as well. `152 s` is how a counter reads, not how a teacher describes a sitting,
// and only a screen that knows the language can pick the shape: hours for a long attempt, minutes
// and seconds for the usual one, seconds alone for the answer that took a moment.
export function span(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return i18n.t("common.duration_h_min", { h, m });
  if (m) return i18n.t("common.duration_min_s", { m, s });
  return i18n.t("common.duration_seconds", { n: s });
}
