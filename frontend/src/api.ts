// Клиент API /api/v1/ — та же сессия, что у админки; для изменений — CSRF-токен из cookie.
import type { CheckResult, Diff, Entity, Grid, Me, Move, Prefs, Run, Semester, Variant, Week, Who } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public body: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken();
  const resp = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 204) return undefined as T;
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    let detail = data?.detail;
    if (Array.isArray(detail)) detail = detail.map((d: { msg?: string }) => d.msg).join("; ");
    const text =
      resp.status === 401
        ? "Нужно войти в админку"
        : resp.status === 403 && !detail
          ? "Недостаточно прав"
          : String(detail || `Ошибка сервера (${resp.status})`);
    throw new ApiError(text, resp.status, data);
  }
  return data as T;
}

const q = (params: Record<string, string | number | undefined>) =>
  new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined)
      .map(([k, v]) => [k, String(v)]),
  ).toString();

export const api = {
  me: () => request<Me>("GET", "/me"),
  semesters: () => request<Semester[]>("GET", "/semesters"),
  semester: (id: number) => request<Semester>("GET", `/semesters/${id}`),
  variants: (semId: number) => request<Variant[]>("GET", `/semesters/${semId}/variants`),
  entities: (variantId: number, by: Who) => request<Entity[]>("GET", `/variants/${variantId}/entities?${q({ by })}`),
  grid: (variantId: number, by: Who, key: string, week: Week) =>
    request<Grid>("GET", `/variants/${variantId}/grid?${q({ by, key, week })}`),
  check: (variantId: number, moves: Move[]) => request<CheckResult>("POST", `/variants/${variantId}/check`, { moves }),
  rebuild: (variantId: number, body: { moves: Move[]; time_limit_s: number; variants: number }) =>
    request<Run>("POST", `/variants/${variantId}/rebuild`, body),
  run: (id: number) => request<Run>("GET", `/runs/${id}`),
  cancelRun: (id: number) => request<Run>("POST", `/runs/${id}/cancel`),
  approve: (variantId: number) => request<Variant>("POST", `/variants/${variantId}/approve`),
  prefs: (semId: number, teacherId: number) => request<Prefs>("GET", `/semesters/${semId}/teachers/${teacherId}/prefs`),
  setPref: (semId: number, teacherId: number, slot: string, value: number) =>
    request<Prefs>("PATCH", `/semesters/${semId}/teachers/${teacherId}/prefs`, { set: { [slot]: value } }),
  diff: (variantId: number, against?: number) =>
    request<Diff>("GET", `/variants/${variantId}/diff?${q({ against })}`),
};
