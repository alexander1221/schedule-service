import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { addMove, applyMoves, undoMove } from "./moves";
import { DiffPanel, MovesPanel, RunPanel } from "./Panels";
import { ScheduleGrid } from "./ScheduleGrid";
import type { CheckResult, Diff, Entity, Grid, GridEntry, Me, Move, Run, Semester, Variant, Week, Who } from "./types";

const WHO: Record<Who, string> = { group: "Группа", teacher: "Преподаватель", room: "Аудитория" };

const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export default function App({ pollMs = 3000 }: { pollMs?: number }) {
  const initial = useMemo(() => new URLSearchParams(window.location.search), []);
  const [me, setMe] = useState<Me | null>(null);
  const [needLogin, setNeedLogin] = useState(false);
  const [error, setError] = useState("");

  const [semesters, setSemesters] = useState<Semester[]>([]);
  const [semId, setSemId] = useState<number | null>(Number(initial.get("semester")) || null);
  const [variants, setVariants] = useState<Variant[]>([]);
  const [variantId, setVariantId] = useState<number | null>(Number(initial.get("variant")) || null);
  const [by, setBy] = useState<Who>("group");
  const [entities, setEntities] = useState<Entity[]>([]);
  const [key, setKey] = useState("");
  const [week, setWeek] = useState<Week>("odd");
  const [grid, setGrid] = useState<Grid | null>(null);

  const [moves, setMoves] = useState<Move[]>([]);
  const [labels, setLabels] = useState<Record<number, string>>({});
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const [busy, setBusy] = useState(false);

  const [run, setRun] = useState<Run | null>(null);
  const [diff, setDiff] = useState<Diff | null>(null);
  const [diffError, setDiffError] = useState("");

  const sem = semesters.find((s) => s.id === semId) ?? null;
  const can = (p: string) => !!me?.permissions.includes(`timetable.${p}`);

  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) setNeedLogin(true);
    else setError(msg(e));
  }, []);

  // начало: кто я и семестры
  useEffect(() => {
    Promise.all([api.me(), api.semesters()])
      .then(([m, ss]) => {
        setMe(m);
        setSemesters(ss);
        setSemId((cur) => (cur && ss.some((s) => s.id === cur) ? cur : (ss[0]?.id ?? null)));
      })
      .catch(fail);
  }, [fail]);

  const loadVariants = useCallback(
    (id: number, pick?: number) =>
      api.variants(id).then((vs) => {
        setVariants(vs);
        setVariantId((cur) => {
          const want = pick ?? cur;
          if (want && vs.some((v) => v.id === want)) return want;
          return (vs.find((v) => v.status === "approved") ?? vs[0])?.id ?? null;
        });
      }),
    [],
  );

  useEffect(() => {
    if (semId) loadVariants(semId).catch(fail);
  }, [semId, loadVariants, fail]);

  // новый вариант или сетка — черновик переносов к нему не относится
  useEffect(() => {
    setMoves([]);
    setCheck(null);
  }, [variantId]);

  useEffect(() => {
    if (!variantId) return;
    api
      .entities(variantId, by)
      .then((es) => {
        setEntities(es);
        setKey((cur) => (es.some((e) => e.id === cur) ? cur : (es[0]?.id ?? "")));
      })
      .catch(fail);
  }, [variantId, by, fail]);

  useEffect(() => {
    if (!variantId || !key) return setGrid(null);
    api.grid(variantId, by, key, week).then(setGrid).catch(fail);
  }, [variantId, by, key, week, fail]);

  // мгновенная проверка черновика независимым валидатором
  const checkSeq = useRef(0);
  useEffect(() => {
    if (!variantId || !moves.length) {
      setCheck(null);
      setCheckError("");
      return;
    }
    const seq = ++checkSeq.current;
    setChecking(true);
    api
      .check(variantId, moves)
      .then((r) => seq === checkSeq.current && (setCheck(r), setCheckError("")))
      .catch((e) => seq === checkSeq.current && setCheckError(msg(e)))
      .finally(() => seq === checkSeq.current && setChecking(false));
  }, [variantId, moves]);

  // ход расчёта
  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.status)) return;
    const t = setTimeout(() => {
      api
        .run(run.id)
        .then((r) => {
          setRun(r);
          if (!["queued", "running"].includes(r.status) && semId) loadVariants(semId);
        })
        .catch(fail);
    }, pollMs);
    return () => clearTimeout(t);
  }, [run, pollMs, semId, loadVariants, fail]);

  const onMove = (entry: GridEntry, day: number, pair: number, toDay: number, toPair: number) => {
    setLabels((l) => ({ ...l, ...Object.fromEntries(entry.lessons.map((x) => [x.item_id, entry.discipline])) }));
    setMoves((ms) => addMove(ms, entry, day, pair, toDay, toPair));
  };

  const rebuild = async (minutes: number, nvariants: number) => {
    if (!semId || !variantId) return;
    setBusy(true);
    setError("");
    try {
      const seen = new Set<string>();
      for (const m of moves) {
        const k = `${m.item_id}/${m.week}/${m.to_day}/${m.to_pair}`;
        if (seen.has(k)) continue;
        seen.add(k);
        try {
          await api.createPin(semId, { item_id: m.item_id, week: m.week, day: m.to_day, pair: m.to_pair });
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 409)) throw e; // уже закреплено так же — не ошибка
        }
      }
      const r = await api.createRun(semId, {
        time_limit_s: Math.round(minutes * 60),
        variants: nvariants,
        reference_variant_id: variantId,
      });
      setRun(r);
      setMoves([]);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.body.active_run_id) {
        setError(`${e.message} (расчёт №${e.body.active_run_id})`);
        api.run(Number(e.body.active_run_id)).then(setRun).catch(fail);
      } else fail(e);
    } finally {
      setBusy(false);
    }
  };

  const compare = (vid: number, against: number) => {
    setDiffError("");
    setVariantId(vid);
    api
      .diff(vid, against)
      .then(setDiff)
      .catch((e) => setDiffError(msg(e)));
  };

  if (needLogin) {
    return (
      <main className="app narrow">
        <h1>Нужно войти</h1>
        <p>
          <a href={`/admin/login/?next=${encodeURIComponent("/app/" + window.location.search)}`}>Войдите в админку</a>{" "}
          под учётной записью диспетчера и вернитесь сюда.
        </p>
      </main>
    );
  }

  const variant = variants.find((v) => v.id === variantId) ?? null;
  const shown = grid && sem ? applyMoves(grid, sem.days, moves) : [];

  return (
    <div className="app">
      <header className="top">
        <h1>Расписание</h1>
        <label>
          Семестр{" "}
          <select value={semId ?? ""} onChange={(e) => setSemId(Number(e.target.value))}>
            {semesters.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Вариант{" "}
          <select value={variantId ?? ""} onChange={(e) => setVariantId(Number(e.target.value))}>
            {variants.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name} · штраф {v.total ?? "—"}
                {v.status === "approved" ? " · утверждён" : ""}
              </option>
            ))}
          </select>
        </label>
        <span className="me">
          {me ? `${me.name} · версия ${me.version}` : ""} · <a href="/admin/">админка</a>
        </span>
      </header>

      {error && (
        <p className="error" role="alert">
          {error}{" "}
          <button type="button" onClick={() => setError("")}>
            ×
          </button>
        </p>
      )}
      {semesters.length === 0 && me && <p>Семестров пока нет — заведите их в админке.</p>}
      {sem && variants.length === 0 && (
        <p>
          У семестра ещё нет вариантов расписания. Постройте первый в админке: «Семестры» → «Построить расписание».
        </p>
      )}

      {variant && (
        <div className="layout">
          <main>
            <div className="toolbar">
              <div role="group" aria-label="Чьё расписание" className="seg">
                {(Object.keys(WHO) as Who[]).map((w) => (
                  <button key={w} type="button" aria-pressed={by === w} onClick={() => setBy(w)}>
                    {WHO[w]}
                  </button>
                ))}
              </div>
              <select aria-label={WHO[by]} value={key} onChange={(e) => setKey(e.target.value)}>
                {entities.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                  </option>
                ))}
              </select>
              <div role="group" aria-label="Неделя" className="seg">
                <button type="button" aria-pressed={week === "odd"} onClick={() => setWeek("odd")}>
                  нечётная
                </button>
                <button type="button" aria-pressed={week === "even"} onClick={() => setWeek("even")}>
                  чётная
                </button>
              </div>
            </div>
            {grid && sem && (
              <ScheduleGrid
                grid={grid}
                days={sem.days}
                shown={shown}
                readOnly={busy}
                onMove={onMove}
                onUndo={(entry, day, pair) => setMoves((ms) => undoMove(ms, entry, day, pair))}
              />
            )}
          </main>
          <aside>
            <MovesPanel
              moves={moves}
              labels={labels}
              check={check}
              checking={checking}
              checkError={checkError}
              canRebuild={can("add_pin") && can("add_solverun")}
              busy={busy}
              onReset={() => setMoves([])}
              onRebuild={rebuild}
            />
            {run && (
              <RunPanel
                run={run}
                onStop={() => api.cancelRun(run.id).then(setRun).catch(fail)}
                onOpen={(id) => setVariantId(id)}
                onCompare={compare}
              />
            )}
            <DiffPanel variants={variants} variantId={variant.id} diff={diff} error={diffError} onCompare={compare} />
          </aside>
        </div>
      )}
    </div>
  );
}
