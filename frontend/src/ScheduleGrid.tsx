// Сетка «дни × пары» с переносом занятий: мышью (перетаскивание) или без неё
// (выбрать занятие щелчком / Enter, затем щёлкнуть клетку; Esc — отменить выбор).
import { useEffect, useRef, useState } from "react";
import type { ShownEntry } from "./moves";
import type { Grid, GridEntry } from "./types";

interface Picked {
  entry: GridEntry;
  day: number;
  pair: number;
}

export const PREF_LABEL: Record<number, string> = {
  [-2]: "нельзя",
  [-1]: "нежелательно",
  0: "всё равно",
  1: "удобно",
  2: "очень удобно",
};

interface Props {
  grid: Grid;
  days: number[];
  shown: ShownEntry[][][];
  readOnly?: boolean;
  /** клетки с нарушениями черновика: «день-пара» → тексты; и элементы, из-за которых нарушения */
  bad?: Map<string, string[]>;
  badItems?: Set<number>;
  /** режим пожеланий преподавателя: значения по клеткам и изменение */
  prefs?: Record<string, number> | null;
  onPref?: (slot: string, value: number) => void;
  onMove: (entry: GridEntry, day: number, pair: number, toDay: number, toPair: number) => void;
  onUndo: (entry: GridEntry, day: number, pair: number) => void;
}

export function ScheduleGrid({ grid, days, shown, readOnly, bad, badItems, prefs, onPref, onMove, onUndo }: Props) {
  const dragging = useRef<Picked | null>(null);
  const [picked, setPicked] = useState<Picked | null>(null);
  const [over, setOver] = useState<string | null>(null);

  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setPicked(null);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);

  const drop = (toDay: number, toPair: number, src: Picked | null) => {
    if (src) onMove(src.entry, src.day, src.pair, toDay, toPair);
    dragging.current = null;
    setPicked(null);
    setOver(null);
  };

  return (
    <div className="grid-wrap">
      <table className="grid" aria-label={`Расписание: ${grid.name}`}>
        <thead>
          <tr>
            <th className="pair-head">Пара</th>
            {grid.days.map((d) => (
              <th key={d}>{d}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.rows.map((row, ri) => (
            <tr key={row.pair}>
              <th className="pair-head">
                {row.pair}
                <span className="time">{row.time}</span>
              </th>
              {days.map((day, di) => {
                const id = `${day}-${row.pair}`;
                const items = shown[ri]?.[di] ?? [];
                const problems = bad?.get(id);
                const pref = prefs ? (prefs[id] ?? 0) : null;
                return (
                  <td
                    key={id}
                    data-slot={id}
                    aria-label={`${grid.days[di]}, ${row.pair}-я пара${problems ? ", нарушение" : ""}`}
                    title={problems?.join("\n")}
                    className={`cell${over === id ? " over" : ""}${picked ? " target" : ""}${problems ? " bad" : ""}${
                      pref !== null ? ` pref pref${pref}` : ""
                    }`}
                    onDragOver={(e) => {
                      if (readOnly || !dragging.current) return;
                      e.preventDefault();
                      setOver(id);
                    }}
                    onDragLeave={() => setOver((o) => (o === id ? null : o))}
                    onDrop={(e) => {
                      e.preventDefault();
                      drop(day, row.pair, dragging.current);
                    }}
                    onClick={() => picked && drop(day, row.pair, picked)}
                  >
                    {pref !== null && (
                      <select
                        className="pref-select"
                        aria-label={`Пожелание: ${grid.days[di]}, ${row.pair}-я пара`}
                        value={pref}
                        disabled={!onPref}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => onPref?.(id, Number(e.target.value))}
                      >
                        {[2, 1, 0, -1, -2].map((v) => (
                          <option key={v} value={v}>
                            {v > 0 ? `+${v}` : v} {PREF_LABEL[v]}
                          </option>
                        ))}
                      </select>
                    )}
                    {items.map((s, k) => {
                      const canMove = !readOnly && s.entry.lessons.length > 0;
                      const isBad = !!problems && s.entry.lessons.some((l) => badItems?.has(l.item_id));
                      const isPicked = picked?.entry === s.entry;
                      return (
                        <div
                          key={k}
                          className={`lesson${s.moved ? " moved" : ""}${isPicked ? " picked" : ""}${isBad ? " bad" : ""}`}
                          draggable={canMove}
                          role={canMove ? "button" : undefined}
                          tabIndex={canMove ? 0 : undefined}
                          aria-pressed={canMove ? isPicked : undefined}
                          title={
                            s.moved && s.from
                              ? `Перенесено с: ${grid.days[days.indexOf(s.from.day)]}, ${s.from.pair}-я пара`
                              : canMove
                                ? "Перетащите в другую клетку (или выберите и щёлкните клетку)"
                                : undefined
                          }
                          onDragStart={(e) => {
                            dragging.current = { entry: s.entry, day, pair: row.pair };
                            e.dataTransfer?.setData("text/plain", s.entry.discipline);
                          }}
                          onDragEnd={() => {
                            dragging.current = null;
                            setOver(null);
                          }}
                          onClick={(e) => {
                            if (!canMove) return;
                            e.stopPropagation();
                            setPicked(isPicked ? null : { entry: s.entry, day, pair: row.pair });
                          }}
                          onKeyDown={(e) => {
                            if (canMove && (e.key === "Enter" || e.key === " ")) {
                              e.preventDefault();
                              setPicked(isPicked ? null : { entry: s.entry, day, pair: row.pair });
                            }
                          }}
                        >
                          <div className="disc">{s.entry.discipline}</div>
                          <div className="who">
                            {grid.by === "group" ? s.entry.teachers : s.entry.groups}
                            {s.entry.room && <span className="room"> · {s.entry.room}</span>}
                          </div>
                          {s.entry.marks.length > 0 && <div className="marks">{s.entry.marks.join(" · ")}</div>}
                          {s.moved && (
                            <button
                              type="button"
                              className="undo"
                              onClick={(e) => {
                                e.stopPropagation();
                                onUndo(s.entry, day, row.pair);
                              }}
                            >
                              вернуть
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {picked && (
        <p className="hint" role="status">
          Выбрано «{picked.entry.discipline}». Щёлкните клетку, куда перенести, или нажмите Esc.
        </p>
      )}
    </div>
  );
}
