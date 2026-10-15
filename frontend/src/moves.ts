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
