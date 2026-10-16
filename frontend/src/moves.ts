// Черновик переносов: что диспетчер передвинул в сетке, но ещё не закрепил.
// Вариант на сервере не меняется — переносы только проверяются (/check) и, по кнопке, закрепляются.
import type { GridEntry, Grid, Move } from "./types";

/** Перенести запись сетки (со всеми её занятиями) из (day, pair) в (toDay, toPair). */
export function addMove(
  moves: Move[],
  entry: GridEntry,
  day: number,
  pair: number,
  toDay: number,
  toPair: number,
): Move[] {
  if (day === toDay && pair === toPair) return moves;
  let next = [...moves];
  for (const l of entry.lessons) {
    // уже перенесённое занятие двигают ещё раз — меняем цель исходного переноса
    const i = next.findIndex(
      (m) => m.item_id === l.item_id && m.week === l.week && m.to_day === day && m.to_pair === pair,
    );
    if (i >= 0) {
      const m = next[i];
      next =
        m.day === toDay && m.pair === toPair
          ? next.filter((_, j) => j !== i) // вернули на место — переноса больше нет
          : next.map((x, j) => (j === i ? { ...x, to_day: toDay, to_pair: toPair } : x));
    } else {
      next.push({ item_id: l.item_id, week: l.week, day, pair, to_day: toDay, to_pair: toPair });
    }
  }
  return next;
}

/** Убрать перенос записи, стоящей сейчас в (toDay, toPair). */
export function undoMove(moves: Move[], entry: GridEntry, toDay: number, toPair: number): Move[] {
  return moves.filter(
    (m) =>
      !entry.lessons.some(
        (l) => l.item_id === m.item_id && l.week === m.week && m.to_day === toDay && m.to_pair === toPair,
      ),
  );
}

export interface ShownEntry {
  entry: GridEntry;
  moved: boolean;
  /** откуда перенесено (для подсказки), если moved */
  from?: { day: number; pair: number };
  /** свёрнутый синхронный блок: записи подгрупп, из которых собрана карточка */
  parts?: GridEntry[];
}

/** Одна и та же запись сетки (по составу занятий) — объекты пересоздаются при каждой отрисовке. */
export const sameEntry = (a: GridEntry, b: GridEntry) =>
  a.lessons.length === b.lessons.length &&
  a.lessons.every((l, i) => l.item_id === b.lessons[i].item_id && l.week === b.lessons[i].week);

/** Записи одного синхронного блока в клетке (английский по подгруппам) → одна карточка.
 *  Блок по правилам стоит целиком в одном слоте, поэтому и переносится целиком. */
export function groupBlocks(items: ShownEntry[]): ShownEntry[] {
  const lists: ShownEntry[][] = [];
  const at = new Map<string, number>(); // блок (и «перенесён ли») → номер списка; порядок — по первой записи
  for (const s of items) {
    const key = s.entry.block_id ? `${s.entry.block_id}/${s.moved}` : null;
    if (key !== null && at.has(key)) lists[at.get(key)!].push(s);
    else {
      if (key !== null) at.set(key, lists.length);
      lists.push([s]);
    }
  }
  const uniq = (xs: string[]) => [...new Set(xs.filter(Boolean))];
  return lists.map((list) => {
    if (list.length === 1) return list[0];
    const parts = list.map((x) => x.entry);
    const first = parts[0];
    return {
      moved: list[0].moved,
      from: list[0].from,
      parts,
      entry: {
        discipline: first.discipline,
        teachers: uniq(parts.map((x) => x.teachers)).join(", "),
        groups: uniq(parts.flatMap((x) => x.groups.split(", "))).join(", "),
        room: "",
        marks: [],
        lessons: parts.flatMap((x) => x.lessons),
        block_id: first.block_id,
        block: first.block,
      },
    };
  });
}

export function plural(n: number, one: string, few: string, many: string) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}

/** Сетка с применёнными переносами: перенесённые записи — в новой клетке, в старой их нет. */
export function applyMoves(grid: Grid, days: number[], moves: Move[]): ShownEntry[][][] {
  const shown: ShownEntry[][][] = grid.rows.map((r) => r.cells.map(() => []));
  const rowOf = new Map(grid.rows.map((r, i) => [r.pair, i]));
  grid.rows.forEach((r, ri) =>
    r.cells.forEach((cell, di) =>
      cell.forEach((e) => {
        const day = days[di];
        const mv = moves.find((m) =>
          e.lessons.some((l) => l.item_id === m.item_id && l.week === m.week && m.day === day && m.pair === r.pair),
        );
        const ti = mv ? rowOf.get(mv.to_pair) : undefined;
        const tj = mv ? days.indexOf(mv.to_day) : -1;
        if (mv && ti !== undefined && tj >= 0) {
          shown[ti][tj].push({ entry: e, moved: true, from: { day, pair: r.pair } });
        } else {
          shown[ri][di].push({ entry: e, moved: false });
        }
      }),
    ),
  );
  return shown;
}
