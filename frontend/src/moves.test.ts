import { addMove, applyMoves, undoMove } from "./moves";
import type { Grid, GridEntry } from "./types";

const math: GridEntry = {
  discipline: "Математика",
  teachers: "Иванова",
  groups: "1ИТ1",
  room: "101",
  marks: [],
  lessons: [{ item_id: 5, week: "both" }],
};
const stream: GridEntry = { ...math, discipline: "Кино", lessons: [{ item_id: 6, week: "odd" }, { item_id: 7, week: "odd" }] };
const grid: Grid = {
  by: "group",
  key: "11",
  name: "1ИТ1",
  week: "odd",
  days: ["Пн", "Вт"],
  rows: [
    { pair: 1, time: "9:00–10:30", cells: [[math], [stream]] },
    { pair: 2, time: "10:40–12:10", cells: [[], []] },
  ],
};

test("перенос, повторный перенос и возврат на место", () => {
  let ms = addMove([], math, 1, 1, 2, 2);
  expect(ms).toEqual([{ item_id: 5, week: "both", day: 1, pair: 1, to_day: 2, to_pair: 2 }]);
  ms = addMove(ms, math, 2, 2, 1, 2); // двигаем уже перенесённое — меняется цель
  expect(ms).toEqual([{ item_id: 5, week: "both", day: 1, pair: 1, to_day: 1, to_pair: 2 }]);
  expect(addMove(ms, math, 1, 2, 1, 1)).toEqual([]); // вернули на исходное место
  expect(addMove([], math, 1, 1, 1, 1)).toEqual([]); // в ту же клетку — ничего
});

test("поток переносится всеми своими занятиями", () => {
  const ms = addMove([], stream, 2, 1, 2, 2);
  expect(ms.map((m) => m.item_id)).toEqual([6, 7]);
  expect(undoMove(ms, stream, 2, 2)).toEqual([]);
});

test("applyMoves показывает перенесённое в новой клетке", () => {
  const shown = applyMoves(grid, [1, 2], addMove([], math, 1, 1, 2, 2));
  expect(shown[0][0]).toEqual([]);
  expect(shown[1][1]).toEqual([{ entry: math, moved: true, from: { day: 1, pair: 1 } }]);
  expect(shown[0][1][0].moved).toBe(false);
});
