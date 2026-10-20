import { announce } from "./announce";
import { api, type JobCard } from "./api/client";
import { STATUS_LABELS, statusIn, type Status } from "./jobs";

/** What a click did and how to take it back, for the undo bar. revert says whether it worked. */
export type Undo = { message: string; revert: () => Promise<boolean> };

// Every button under a job card, in order. Each one says when it shows, whether it is pressed, and what
// a click does; the card shows the ones whose `shows` is true. A click changes the job in place and
// returns its undo (none for Restore), and the page then asks again which jobs its filters let through.
export type CardButton = {
  label: string;
  hint?: string;
  shows: (job: JobCard) => boolean;
  pressed?: (job: JobCard) => boolean;
  click: (job: JobCard) => Promise<Undo | undefined>;
};

/** Says what happened, and offers to take it back. */
function done(message: string, revert: () => Promise<boolean>): Undo {
  announce(message);
  return { message, revert };
}

/** A status button: it shows when the job has one of `after` (or the status itself, pressed, so a
 * click clears it). */
function statusButton(status: Status, after: Status[], hint?: string): CardButton {
  const isSet = statusIn(status);
  return {
    label: STATUS_LABELS[status],
    hint,
    shows: statusIn(status, ...after),
    pressed: isSet,
    click: (job) => setStatus(job, isSet(job) ? "none" : status),
  };
}

type StatusState = Pick<JobCard, "status" | "status_at" | "follow_up_on">;

async function postStatus(job: JobCard, body: StatusState | { status: Status }): Promise<boolean> {
  const { data } = await api.POST("/jobs/{job_id}/status", { params: { path: { job_id: job.id } }, body });
  if (!data) return false;
  Object.assign(job, { status: data.status, status_at: data.status_at, follow_up_on: data.follow_up_on });
  return true;
}

async function setStatus(job: JobCard, status: Status): Promise<Undo | undefined> {
  const before: StatusState = { status: job.status, status_at: job.status_at, follow_up_on: job.follow_up_on };
  if (!(await postStatus(job, { status }))) return;
  return done(
    status === "none" ? `Status cleared: ${job.title}` : `${job.title}: ${STATUS_LABELS[status]}`,
    // The earlier status goes back with its own date, not today's.
    () => postStatus(job, before.status === "none" ? { status: "none" } : before),
  );
}

async function postList(job: JobCard, japanese: boolean): Promise<boolean> {
  const { data } = await api.POST("/jobs/{job_id}/japanese", { params: { path: { job_id: job.id } }, body: { japanese } });
  if (!data) return false;
  job.stage = data.stage;
  return true;
}

async function moveList(job: JobCard, japanese: boolean): Promise<Undo | undefined> {
  if (!(await postList(job, japanese))) return;
  return done(`${job.title}: moved to the ${japanese ? "Japanese" : "main"} list`, () => postList(job, !japanese));
}

/** Put a removed job back; the same rule never removes it again. A code-removed job first fetches its
 * posting, so that can take a moment. No undo: the job is already on its way to the model. */
async function restore(job: JobCard): Promise<undefined> {
  const { data } = await api.POST("/jobs/{job_id}/restore", { params: { path: { job_id: job.id } } });
  if (!data) return;
  Object.assign(job, { stage: data.stage, removal_rule: null, removal_quote: null });
  announce(`Restored ${job.title}`);
}

/** Hide every job from this employer, now and in later runs. The Blacklist page brings them back. */
async function blacklistEmployer(job: JobCard): Promise<Undo | undefined> {
  const { data } = await api.POST("/jobs/{job_id}/blacklist", { params: { path: { job_id: job.id } } });
  if (!data) return;
  Object.assign(job, { stage: "code_removed", removal_rule: "blacklisted", removal_quote: data.company });
  return done(`Blacklisted ${data.company}: ${data.hidden} ${data.hidden === 1 ? "job" : "jobs"} hidden`, async () => {
    const { data: off } = await api.POST("/blacklist/remove", { body: { company_norm: data.company_norm } });
    return Boolean(off);
  });
}

const isBlacklisted = (job: JobCard) => job.removal_rule === "blacklisted";
// A blacklisted job comes back from the Blacklist page, with the rest of its employer's jobs.
const isRemoved = (job: JobCard) => (job.stage === "removed" || job.stage === "code_removed") && !isBlacklisted(job);

export const CARD_BUTTONS: CardButton[] = [
  statusButton("apply", ["none"], "Also drafts an application packet"),
  statusButton("applied", ["none", "apply"]),
  statusButton("interviewing", ["applied", "ghosted"]),
  statusButton("rejected", ["applied", "interviewing", "ghosted"]),
  statusButton("ghosted", ["applied", "interviewing"]),
  statusButton("not_interested", ["none", "apply"]),
  // The model decides the list at first; a move by hand stays when the job is scored again.
  { label: "Japanese", shows: (job) => job.stage === "kept", click: (job) => moveList(job, true) },
  { label: "Not Japanese", shows: (job) => job.stage === "japanese", click: (job) => moveList(job, false) },
  { label: "Restore", hint: "The rule that removed it never removes it again", shows: isRemoved, click: restore },
  {
    label: "Blacklist",
    hint: "Hide every job from this employer, now and in later runs",
    shows: (job) => Boolean(job.company) && !isBlacklisted(job),
    click: blacklistEmployer,
  },
];
