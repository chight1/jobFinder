import { expect, test } from "vitest";
import type { JobCard } from "./api/client";
import { optionsOn, passes, RATING_FILTER, toggled } from "./filters";

const rated = (rating: JobCard["rating"]) => ({ rating }) as JobCard;

test("with no key in the query, every option is on", () => {
  expect([...optionsOn(RATING_FILTER, {})]).toEqual(["green", "yellow", "red", "grey"]);
  expect(passes(rated(null), [RATING_FILTER], {})).toBe(true);
});

test("a job shows when an option that is on matches it", () => {
  const query = { rating: "green,grey" };
  expect(passes(rated("green"), [RATING_FILTER], query)).toBe(true);
  expect(passes(rated(null), [RATING_FILTER], query)).toBe(true);
  expect(passes(rated("red"), [RATING_FILTER], query)).toBe(false);
  expect(passes(rated("green"), [RATING_FILTER], { rating: "" })).toBe(false); // every option off
});

test("toggling flips one option and keeps the rest of the query", () => {
  const off = toggled(RATING_FILTER, "red", { other: "x" });
  expect(off).toEqual({ other: "x", rating: "green,yellow,grey" });
  expect(toggled(RATING_FILTER, "red", off)).toEqual({ other: "x" }); // all on again: no key
});
