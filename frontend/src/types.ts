// Типы ответов API /api/v1/ — зеркало api/schemas.py (docs/API.md).

export type Who = "group" | "teacher" | "room";
export type Week = "odd" | "even";
export type AnyWeek = "both" | Week;

export interface Me {
  email: string;
  name: string;
  version: string;
  permissions: string[];
}

export interface Semester {
  id: number;
  name: string;
  start_date: string;
  end_date: string | null;
  status: string;
  days: number[];
  pairs: number[];
  active_run_id: number | null;
  approved_variant_id: number | null;
}

export interface Variant {
  id: number;
  semester_id: number;
  name: string;
  status: "draft" | "approved";
  total: number | null;
  solver_status: string;
  created_at: string;
  run_id: number | null;
  changes: number | null;
  room_changes: number | null;
}

export interface Entity {
  id: string;
  name: string;
}

export interface Lesson {
  item_id: number;
  week: AnyWeek;
}

export interface GridEntry {
  discipline: string;
  teachers: string;
  groups: string;
  room: string;
  marks: string[];
  lessons: Lesson[];
  /** синхронный блок (английский по подгруппам); "" — не блок */
  block_id?: string;
  block?: string;
}

export interface GridRow {
  pair: number;
  time: string;
  cells: GridEntry[][];
}

export interface Grid {
  by: Who;
  key: string;
  name: string;
  week: Week;
  days: string[];
  rows: GridRow[];
}

/** Перенос одного занятия: откуда (day, pair) и куда; неделя — как у назначения. */
export interface Move {
  item_id: number;
  week: AnyWeek;
  day: number;
  pair: number;
  to_day: number;
  to_pair: number;
}

export interface Violation {
  code: string;
  message: string;
  /** где: «день-пара»; null — не привязано к клетке */
  slot: string | null;
  /** odd | even; null — обе недели */
  week: Week | null;
  items: number[];
}

export interface Prefs {
  teacher: { id: number; name: string };
  semester_id: number;
  /** «день-пара» → −2…+2; нет ключа — «всё равно» */
  prefs: Record<string, number>;
}

export interface CheckResult {
  ok: boolean;
  hard: Violation[];
  warnings: Violation[];
  total: number;
  base_total: number;
  delta: number;
}

export interface Run {
  id: number;
  semester_id: number;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  status_label: string;
  progress: { variant?: number; of?: number; stage?: string; objective?: number | null };
  message: string;
  variant_ids: number[];
  reference_variant_id: number | null;
  cancel_requested: boolean;
}

export interface Placement {
  week: string;
  day: number;
  pair: number;
  room: string;
  label: string;
}

export interface Change {
  item_id: string;
  discipline: string;
  groups: string[];
  teachers: string[];
  kind: string;
  before: Placement[];
  after: Placement[];
}

export interface Diff {
  variant_id: number;
  against_variant_id: number;
  moved: number;
  room_changed: number;
  affected_groups: string[];
  affected_teachers: string[];
  affected_rooms: string[];
  changes: Change[];
}
