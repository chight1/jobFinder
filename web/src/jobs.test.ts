import { expect, test } from "vitest";
import type { JobCard } from "./api/client";
import { insertByScore, localToday, statusName } from "./jobs";

const job = (id: number, score: number) => ({ id, score }) as JobCard;
const ids = (jobs: JobCard[]) => jobs.map((j) => j.id);

test("arrivals slot in by score", () => {
  const shown = [job(1, 90), job(2, 80), job(3, 40)];
  expect(ids(insertByScore(shown, [job(4, 85), job(5, 10), job(6, 99)]))).toEqual([6, 1, 4, 2, 3, 5]);
});

test("a job already shown is not added twice", () => {
  expect(ids(insertByScore([job(1, 50)], [job(1, 50)]))).toEqual([1]);
});

test("status names", () => {
  expect(statusName("apply")).toBe("to apply");
  expect(statusName("ghosted")).toBe("ghosted");
});

test("today is local", () => {
  expect(localToday(new Date(2026, 9, 9, 23, 30))).toBe("2026-10-09");
});
