import { expect, test } from "vitest";
import type { JobCard } from "./api/client";
import { CARD_BUTTONS } from "./cardButtons";
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
  expect(statusName("apply")).toBe("To apply");
  expect(statusName("not_interested")).toBe("Not interested");
  expect(statusName("none")).toBe("No status");
});

test("today is local", () => {
  expect(localToday(new Date(2026, 9, 9, 23, 30))).toBe("2026-10-09");
});

test("a card shows only the buttons whose rule holds", () => {
  const shown = (status: JobCard["status"], stage = "kept") =>
    CARD_BUTTONS.filter((b) => b.shows({ status, stage } as JobCard)).map((b) => b.label);
  expect(shown("none")).toEqual(["To apply", "Applied", "Not interested", "Japanese"]);
  expect(shown("applied")).toEqual(["Applied", "Interviewing", "Rejected", "Ghosted", "Japanese"]);
  expect(shown("ghosted", "japanese")).toEqual(["Interviewing", "Rejected", "Ghosted", "Not Japanese"]);
  expect(shown("rejected", "extract_failed")).toEqual(["Rejected"]);
});
