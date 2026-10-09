import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

// Typed calls to the jobhunt API. schema.d.ts is generated from FastAPI's OpenAPI schema
// (`npm run types` with the server running), so a changed response model is a type error here.
export const api = createClient<paths>({ baseUrl: "/api" });

type Schemas = components["schemas"];
export type JobCard = Schemas["JobCard"];
export type JobDetail = Schemas["JobDetail"];
export type Health = Schemas["Health"];
export type Activity = Schemas["Activity"];
export type TablePage = Schemas["TablePage"];
export type JobList = Schemas["JobList"];
export type StatusSaved = Schemas["StatusSaved"];
export type SkillsPage = Schemas["SkillsPage"];
export type SkillRow = Schemas["SkillRow"];
