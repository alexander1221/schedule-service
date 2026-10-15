// Боковые панели: черновик переносов с проверкой, ход расчёта, сравнение вариантов.
import { useState } from "react";
import type { CheckResult, Diff, Move, Run, Variant } from "./types";

const DAYS = ["", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
const WEEKS: Record<string, string> = { both: "каждую неделю", odd: "нечётная", even: "чётная" };
const slot = (day: number, pair: number) => `${DAYS[day] ?? day} ${pair}`;
const short = (xs: string[], n = 12) => xs.slice(0, n).join(", ") + (xs.length > n ? ` и ещё ${xs.length - n}` : "");

export function MovesPanel(props: {
  moves: Move[];
  labels: Record<number, string>;
  check: CheckResult | null;
  checking: boolean;
  checkError: string;
  canRebuild: boolean;
  busy: boolean;
  onReset: () => void;
  onRebuild: (minutes: number, variants: number) => void;
}) {
  const { moves, labels, check, checking, checkError } = props;
  const [minutes, setMinutes] = useState(5);
  const [variants, setVariants] = useState(1);
  if (!moves.length) {
    return (
      <section className="panel" aria-label="Переносы">
        <h2>Переносы</h2>
        <p className="muted">
          Перетащите пару в другую клетку — сервис сразу проверит, что получится. Вариант при этом не меняется.
        </p>
      </section>
    );
  }
  return (
    <section className="panel" aria-label="Переносы">
      <h2>Переносы ({moves.length})</h2>
      <ul className="moves">
        {moves.map((m, i) => (
          <li key={i}>
            {labels[m.item_id] ?? `занятие ${m.item_id}`}: {slot(m.day, m.pair)} → {slot(m.to_day, m.to_pair)}
            <span className="muted"> ({WEEKS[m.week]})</span>
          </li>
        ))}
      </ul>
      <div className="check" aria-live="polite">
        {checking && <p className="muted">Проверяю…</p>}
        {checkError && <p className="bad">{checkError}</p>}
        {check && !checking && (
          <>
            <p className={check.ok ? "good" : "bad"}>
              {check.ok ? "Жёстких нарушений нет." : `Нельзя: нарушений ${check.hard.length}.`}
            </p>
            <p>
              Штраф: {check.base_total} → <b>{check.total}</b>{" "}
              <span className={check.delta > 0 ? "bad" : check.delta < 0 ? "good" : "muted"}>
                ({check.delta > 0 ? "+" : ""}
                {check.delta})
              </span>
            </p>
            {check.hard.length > 0 && (
              <ul className="violations">
                {check.hard.slice(0, 12).map((v, i) => (
                  <li key={i}>{v.message}</li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      <div className="actions">
        <button type="button" onClick={props.onReset} disabled={props.busy}>
          Сбросить
        </button>
      </div>
      {props.canRebuild && (
        <fieldset className="rebuild">
          <legend>Закрепить переносы и пересобрать</legend>
          <p className="muted">
            Перенесённые пары закрепляются, остальное решатель подстроит, стараясь ничего лишнего не двигать. Новый
            вариант появится рядом; текущий не изменится.
          </p>
          <label>
            Минут на вариант{" "}
            <input type="number" min={1} max={60} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} />
          </label>
          <label>
            Вариантов{" "}
            <input type="number" min={1} max={5} value={variants} onChange={(e) => setVariants(Number(e.target.value))} />
          </label>
          <button type="button" className="primary" disabled={props.busy} onClick={() => props.onRebuild(minutes, variants)}>
            Закрепить и пересобрать
          </button>
        </fieldset>
      )}
    </section>
  );
}

export function RunPanel(props: {
  run: Run;
  onStop: () => void;
  onOpen: (variantId: number) => void;
  onCompare: (variantId: number, against: number) => void;
}) {
  const { run } = props;
  const active = run.status === "queued" || run.status === "running";
  const p = run.progress || {};
  return (
    <section className="panel" aria-label="Расчёт">
      <h2>Расчёт №{run.id}</h2>
      <p aria-live="polite">
        <b>{run.status_label}</b>
        {run.status === "running" && p.variant ? ` — вариант ${p.variant} из ${p.of}` : ""}
        {run.status === "running" && p.objective != null ? `, штраф ≈ ${Math.round(p.objective)}` : ""}
        {run.cancel_requested && active ? " (остановка запрошена)" : ""}
      </p>
      {run.status === "queued" && <p className="muted">Ждёт обработчик расчётов на сервере.</p>}
      {run.message && <p className={run.status === "failed" ? "bad" : "muted"}>{run.message}</p>}
      {active && (
        <button type="button" onClick={props.onStop} disabled={run.cancel_requested}>
          Остановить и сохранить найденное
        </button>
      )}
      {run.variant_ids.length > 0 && (
        <ul className="moves">
          {run.variant_ids.map((id) => (
            <li key={id}>
              Вариант {id}{" "}
              <button type="button" onClick={() => props.onOpen(id)}>
                открыть
              </button>{" "}
              {run.reference_variant_id && (
                <button type="button" onClick={() => props.onCompare(id, run.reference_variant_id!)}>
                  что изменилось
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function DiffPanel(props: {
  variants: Variant[];
  variantId: number;
  diff: Diff | null;
  error: string;
  onCompare: (variantId: number, against: number) => void;
}) {
  const { diff, variants, variantId } = props;
  const others = variants.filter((v) => v.id !== variantId);
  const [against, setAgainst] = useState<number | "">("");
  return (
    <section className="panel" aria-label="Сравнение">
      <h2>Сравнение</h2>
      <label>
        С вариантом{" "}
        <select value={against} onChange={(e) => setAgainst(e.target.value ? Number(e.target.value) : "")}>
          <option value="">— выберите —</option>
          {others.map((v) => (
            <option key={v.id} value={v.id}>
              {v.name}
              {v.status === "approved" ? " (утверждён)" : ""}
            </option>
          ))}
        </select>
      </label>{" "}
      <button type="button" disabled={against === ""} onClick={() => against !== "" && props.onCompare(variantId, against)}>
        Сравнить
      </button>
      {props.error && <p className="bad">{props.error}</p>}
      {diff && diff.variant_id === variantId && (
        <div className="diff">
          {diff.changes.length === 0 ? (
            <p>Варианты совпадают.</p>
          ) : (
            <>
              <p>
                Перенесено занятий: <b>{diff.moved}</b>, сменили аудиторию: <b>{diff.room_changed}</b>
                <span className="muted"> (по неделям)</span>
              </p>
              <p>
                Группы ({diff.affected_groups.length}): {short(diff.affected_groups)}
              </p>
              <p>
                Преподаватели ({diff.affected_teachers.length}): {short(diff.affected_teachers)}
              </p>
              <ul className="changes" aria-label="Изменения">
                {diff.changes.slice(0, 40).map((c) => (
                  <li key={c.item_id}>
                    <b>{c.discipline}</b> ({c.groups.join(", ")}) — {c.kind}
                    <div className="muted">было: {c.before.map((x) => x.label).join("; ") || "—"}</div>
                    <div>стало: {c.after.map((x) => x.label).join("; ") || "—"}</div>
                  </li>
                ))}
              </ul>
              {diff.changes.length > 40 && <p className="muted">… и ещё {diff.changes.length - 40}</p>}
            </>
          )}
        </div>
      )}
    </section>
  );
}
