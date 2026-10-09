import type { JobCard } from "./api/client";

export type Status = JobCard["status"];
export type Rating = NonNullable<JobCard["rating"]>;

export const RATINGS: Rating[] = ["green", "yellow", "red"];

// The status control on a tile, in order. Setting "to apply" also has the model draft a packet.
export const STATUS_BUTTONS: { status: Exclude<Status, "none">; label: string; hint?: string }[] = [
  { status: "apply", label: "to apply", hint: "also drafts an application packet" },
  { status: "applied", label: "applied" },
  { status: "interviewing", label: "interviewing" },
  { status: "rejected", label: "rejected" },
  { status: "ghosted", label: "ghosted" },
  { status: "not_interested", label: "not interested" },
];

export const APPLIED_STATUSES: Status[] = ["apply", "applied", "interviewing", "rejected", "ghosted"];

const STATUS_NAMES: Partial<Record<Status, string>> = { apply: "to apply", not_interested: "not interested" };

export const statusName = (status: Status): string => STATUS_NAMES[status] ?? status;

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
