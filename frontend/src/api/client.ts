// Thin fetch client for the backend REST API (v1).
// Cookies carry the session; the CSRF token is read from the readable
// `llp_csrf` cookie and echoed back on state-changing requests (double-submit).

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api/v1";

const CSRF_COOKIE = "llp_csrf";
const CSRF_HEADER = "X-CSRF-Token";

function readCookie(name: string): string | undefined {
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=")[1]) : undefined;
}

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (method !== "GET" && method !== "HEAD") {
    headers.set("Content-Type", "application/json");
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }

  const resp = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...options,
    headers,
  });

  if (resp.status === 204) return undefined as T;

  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;

  if (!resp.ok) {
    const err = data?.error || {};
    throw new ApiError(resp.status, err.code || "error", err.message || resp.statusText);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: body ? JSON.stringify(body) : undefined }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

export interface Branding {
  system_name: string;
  short_name: string;
  logo_url?: string | null;
  favicon_url?: string | null;
  login_image_url?: string | null;
  login_title?: string | null;
  welcome_message?: string | null;
  login_instructions?: string | null;
  footer?: string | null;
  support_text?: string | null;
  accent_color?: string | null;
}

export interface UiConfig {
  enabled_ui_languages: string[];
  default_ui_language: string;
  learning_languages: string[];
  translation_languages: string[];
}

export interface Bootstrap {
  branding: Branding;
  ui: UiConfig;
}
