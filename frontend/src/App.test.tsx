// Интерфейс целиком с подменённым API: загрузка, перенос мышью и без неё, проверка, пересборка, сравнение.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import App from "./App";

type Call = { method: string; url: string; body: unknown };

const grid = {
  by: "group",
  key: "11",
  name: "1ИТ1",
  week: "odd",
  days: ["Пн", "Вт", "Ср", "Чт", "Пт"],
  rows: [1, 2].map((p) => ({
    pair: p,
    time: p === 1 ? "9:00–10:30" : "10:40–12:10",
    cells: [0, 1, 2, 3, 4].map((d) =>
      d === 0 && p === 1
        ? [
            {
              discipline: "Математика",
              teachers: "Иванова А.А.",
              groups: "1ИТ1",
              room: "101",
              marks: [],
              lessons: [{ item_id: 5, week: "both" }],
            },
          ]
        : [],
    ),
  })),
};

function mockApi(opts: { me401?: boolean } = {}) {
  const calls: Call[] = [];
  let runPolls = 0;
  const variants = [{ id: 7, semester_id: 1, name: "Осень", status: "approved", total: 100, changes: null, room_changes: null }];
  const json = (status: number, body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    const p = url.replace("/api/v1", "");
    if (p === "/me")
      return opts.me401
        ? json(401, { detail: "Unauthorized" })
        : json(200, {
            email: "d@c.ru",
            name: "Диспетчер",
            version: "0.11.0",
            permissions: ["timetable.add_pin", "timetable.add_solverun"],
          });
    if (p === "/semesters")
      return json(200, [
        { id: 1, name: "Осень 2026", start_date: "2026-09-01", end_date: null, status: "active", days: [1, 2, 3, 4, 5], pairs: [1, 2], active_run_id: null, approved_variant_id: 7 },
      ]);
    if (p === "/semesters/1/variants") return json(200, variants);
    if (p.startsWith("/variants/7/entities")) return json(200, [{ id: "11", name: "1ИТ1" }]);
    if (p.startsWith("/variants/") && p.includes("/grid")) return json(200, grid);
    if (p === "/variants/7/check")
      return json(200, {
        ok: false,
        hard: [{ code: "teacher_busy", message: "Иванова А.А.: Вт 2 — занята" }],
        warnings: [],
        total: 130,
        base_total: 100,
        delta: 30,
        breakdown: [],
      });
    if (p === "/semesters/1/pins") return json(201, { id: 1 });
    if (p === "/semesters/1/runs")
      return json(201, { id: 31, semester_id: 1, status: "queued", status_label: "в очереди", progress: {}, message: "", variant_ids: [], reference_variant_id: 7, cancel_requested: false });
    if (p === "/runs/31") {
      runPolls += 1;
      if (runPolls >= 2) variants.push({ id: 8, semester_id: 1, name: "Перенос 1", status: "draft", total: 95, changes: 3, room_changes: 0 } as never);
      return json(200, {
        id: 31,
        semester_id: 1,
        status: runPolls >= 2 ? "done" : "running",
        status_label: runPolls >= 2 ? "готово" : "считается",
        progress: { variant: 1, of: 1, objective: 120 },
        message: runPolls >= 2 ? "Сохранено вариантов: 1 из 1." : "",
        variant_ids: runPolls >= 2 ? [8] : [],
        reference_variant_id: 7,
        cancel_requested: false,
      });
    }
    if (p.startsWith("/variants/8/diff"))
      return json(200, {
        variant_id: 8,
        against_variant_id: 7,
        moved: 2,
        room_changed: 0,
        affected_groups: ["1ИТ1"],
        affected_teachers: ["Иванова А.А."],
        affected_rooms: [],
        changes: [{ item_id: "5", discipline: "Математика", groups: ["1ИТ1"], teachers: ["Иванова А.А."], kind: "перенос", before: [{ label: "Пн 1 пара (нечёт)" }], after: [{ label: "Вт 2 пара (нечёт)" }] }],
      });
    return json(404, { detail: "Не найдено" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const lesson = async () => (await screen.findByText("Математика")).closest(".lesson") as HTMLElement;
const cell = (slot: string) => document.querySelector(`td[data-slot="${slot}"]`) as HTMLElement;
const checks = (calls: Call[]) => calls.filter((c) => c.url.endsWith("/check"));

afterEach(() => vi.unstubAllGlobals());

test("загружает семестр, утверждённый вариант и сетку", async () => {
  mockApi();
  render(<App />);
  expect((await lesson()).textContent).toContain("Иванова А.А.");
  const sel = screen.getByRole("combobox", { name: /Вариант/ }) as HTMLSelectElement;
  expect(sel.selectedOptions[0].textContent).toBe("Осень · штраф 100 · утверждён");
});

test("перетаскивание: мгновенная проверка, штраф и нарушения", async () => {
  const calls = mockApi();
  render(<App />);
  fireEvent.dragStart(await lesson());
  fireEvent.dragOver(cell("2-2"));
  fireEvent.drop(cell("2-2"));
  await screen.findByText(/Нельзя: нарушений 1/);
  expect(checks(calls).at(-1)!.body).toEqual({
    moves: [{ item_id: 5, week: "both", day: 1, pair: 1, to_day: 2, to_pair: 2 }],
  });
  screen.getByText("(+30)");
  screen.getByText("Иванова А.А.: Вт 2 — занята");
  within(cell("2-2")).getByText("Математика"); // показано на новом месте
  expect(within(cell("1-1")).queryByText("Математика")).toBeNull();
});

test("перенос без мыши: выбрать занятие и щёлкнуть клетку; «вернуть» отменяет", async () => {
  const calls = mockApi();
  render(<App />);
  const l = await lesson();
  fireEvent.keyDown(l, { key: "Enter" });
  expect(screen.getByRole("status").textContent).toContain("Выбрано «Математика»");
  fireEvent.click(cell("3-1"));
  await waitFor(() => expect(checks(calls).at(-1)!.body).toMatchObject({ moves: [{ to_day: 3, to_pair: 1 }] }));
  fireEvent.click(within(cell("3-1")).getByRole("button", { name: "вернуть" }));
  await within(cell("1-1")).findByText("Математика");
  screen.getByText(/Перетащите пару в другую клетку/);
});

test("закрепить и пересобрать → ход расчёта → что изменилось", async () => {
  const calls = mockApi();
  render(<App pollMs={10} />);
  fireEvent.dragStart(await lesson());
  fireEvent.drop(cell("2-2"));
  fireEvent.click(await screen.findByRole("button", { name: "Закрепить и пересобрать" }));
  await screen.findByText("Расчёт №31");
  expect(calls.find((c) => c.url.endsWith("/pins"))!.body).toEqual({ item_id: 5, week: "both", day: 2, pair: 2 });
  expect(calls.find((c) => c.url.endsWith("/runs") && c.method === "POST")!.body).toMatchObject({
    time_limit_s: 300,
    variants: 1,
    reference_variant_id: 7,
  });
  fireEvent.click(await screen.findByRole("button", { name: "что изменилось" }));
  expect((await screen.findByText(/Перенесено занятий/)).textContent).toContain("Перенесено занятий: 2");
  screen.getByText(/стало: Вт 2 пара/);
  expect(calls.some((c) => c.url.includes("/variants/8/diff?against=7"))).toBe(true);
});

test("без входа — ссылка на вход в админку", async () => {
  mockApi({ me401: true });
  render(<App />);
  const link = await screen.findByRole("link", { name: "Войдите в админку" });
  expect(link.getAttribute("href")).toContain("/admin/login/?next=%2Fapp%2F");
});
