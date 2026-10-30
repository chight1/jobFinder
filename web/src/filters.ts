import type { LocationQuery, LocationQueryRaw } from "vue-router";
import type { Schemas } from "./api/client";

// The jobs table's filters come from the API (jobfinder/web/filters.py), which says what each filter's
// options are and which are on. The page only draws them and keeps the choice in the query string:
// one parameter per option that is on (?status=applied&status=interviewing). A filter the query leaves
// out uses the API's defaults.
export type FilterState = Schemas["FilterState"];

/** The query with one option flipped. The filter's key then lists every option that is on; with none
 * on, it holds one empty value, so the API does not fall back to the defaults. */
export function toggled(filter: FilterState, option: string, query: LocationQuery): LocationQueryRaw {
  const on = filter.options.filter((o) => o.on !== (o.key === option)).map((o) => o.key);
  return { ...query, [filter.key]: on.length ? on : [""] };
}
