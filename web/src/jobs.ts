import type { LocationQuery } from "vue-router";
import type { JobCard } from "./api/client";

export type Status = JobCard["status"];
export type Rating = NonNullable<JobCard["rating"]>;

export const RATINGS: Rating[] = ["green", "yellow", "red"];

// Every status's name, in sentence case like every label in the app. A Record, so a status added to
// the API fails the build until it has a name here.
export const STATUS_LABELS: Record<Status, string> = {
  none: "No status",
  apply: "To apply",
  applied: "Applied",
  interviewing: "Interviewing",
  rejected: "Rejected",
  ghosted: "Ghosted",
  not_interested: "Drop",
};

export const statusName = (status: Status): string => STATUS_LABELS[status];

/** A test for "the job has one of these statuses". Typed, so a renamed status fails the build. */
export const statusIn =
  (...statuses: Status[]) =>
  (job: JobCard): boolean =>
    statuses.includes(job.status);

/** The jobs table narrowed to the jobs asking for one skill; Japanese ones too when asked. From the
 * jobs table itself, its query comes along, so the other filters stay as they are. */
export const skillLink = (key: string, withJapanese = false, keep: LocationQuery = {}) => ({
  path: "/jobs",
  query: { ...keep, skill: key, ...(withJapanese ? { japanese: ["japanese", "not_japanese"] } : {}) },
});

/** "unicorn" to "Unicorn": for values shown as labels. */
export const capitalized = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);

/** Today as YYYY-MM-DD in local time, to compare with follow-up dates. */
export function localToday(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Jobs that arrived while a list was open, slotted in by score. */
export function insertByScore(jobs: JobCard[], arrived: JobCard[]): JobCard[] {
  const result = [...jobs];
  const shown = new Set(jobs.map((job) => job.id));
  for (const job of arrived) {
    if (shown.has(job.id)) continue;
    const below = result.findIndex((other) => (other.score ?? 0) < (job.score ?? 0));
    result.splice(below === -1 ? result.length : below, 0, job);
    shown.add(job.id);
  }
  return result;
}
