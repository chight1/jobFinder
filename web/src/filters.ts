import type { LocationQuery } from "vue-router";
import type { JobCard } from "./api/client";

// Filters for a job list. A filter is a set of on/off options, each with its own rule for the jobs it
// matches. A job passes a filter when an option that is on matches it, and it shows when it passes
// every filter. Every option starts on, so nothing is hidden until a click.
//
// The state lives in the query string, one key per filter listing the options that are on:
// ?rating=green,grey. No key means every option is on, so a plain link shows everything.
//
// A filter shows as a strip of text buttons, or, with `dots`, as circles like the rating dots on a card;
// each option's key is then its color (.dot.green).
export type FilterOption = { key: string; label: string; matches: (job: JobCard) => boolean };
export type Filter = { key: string; label: string; options: FilterOption[]; dots?: boolean };

export const RATING_FILTER: Filter = {
  key: "rating",
  label: "Rating",
  dots: true,
  options: [
    { key: "green", label: "Green", matches: (job) => job.rating === "green" },
    { key: "yellow", label: "Yellow", matches: (job) => job.rating === "yellow" },
    { key: "red", label: "Red", matches: (job) => job.rating === "red" },
    { key: "grey", label: "Not rated", matches: (job) => job.rating === null },
  ],
};

/** The keys of the filter's options that are on. */
export function optionsOn(filter: Filter, query: LocationQuery): Set<string> {
  const value = query[filter.key];
  if (typeof value !== "string") return new Set(filter.options.map((option) => option.key));
  return new Set(value.split(",").filter((key) => filter.options.some((option) => option.key === key)));
}

/** Whether a job shows under every filter. */
export function passes(job: JobCard, filters: Filter[], query: LocationQuery): boolean {
  return filters.every((filter) => {
    const on = optionsOn(filter, query);
    return filter.options.some((option) => on.has(option.key) && option.matches(job));
  });
}

/** The query with one option flipped. With every option on again, the key goes away. */
export function toggled(filter: Filter, option: string, query: LocationQuery): LocationQuery {
  const on = optionsOn(filter, query);
  if (!on.delete(option)) on.add(option);
  const rest = { ...query };
  delete rest[filter.key];
  if (on.size === filter.options.length) return rest;
  return { ...rest, [filter.key]: filter.options.filter((o) => on.has(o.key)).map((o) => o.key).join(",") };
}
