import { expect, test } from "vitest";
import { toggled, type FilterState } from "./filters";

const rating: FilterState = {
  key: "rating",
  label: "Rating",
  look: "dots",
  options: [
    { key: "green", label: "Green", on: true },
    { key: "red", label: "Red", on: false },
  ],
};

test("toggling lists every option that is on and keeps the rest of the query", () => {
  expect(toggled(rating, "red", { skill: "go" })).toEqual({ skill: "go", rating: ["green", "red"] });
  expect(toggled(rating, "green", {})).toEqual({ rating: [""] }); // none on: not the defaults
});
