import { announce } from "./announce";
import { api, type JobCard } from "./api/client";
import { STATUS_LABELS, statusIn, type Status } from "./jobs";
import { tabCounts } from "./tabCounts";

// Every button under a job card, in order. Each one says when it shows, whether it is pressed, and what
// a click does; the card shows the ones whose `shows` is true. A click changes the job in place, and
// the list it is on decides whether it still belongs there.
export type CardButton = {
  label: string;
  hint?: string;
  shows: (job: JobCard) => boolean;
  pressed?: (job: JobCard) => boolean;
  click: (job: JobCard) => Promise<void>;
};

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

async function setStatus(job: JobCard, status: Status) {
  const { data } = await api.POST("/jobs/{job_id}/status", { params: { path: { job_id: job.id } }, body: { status } });
  if (!data) return;
  Object.assign(job, { status: data.status, status_at: data.status_at, follow_up_on: data.follow_up_on });
  tabCounts.value = data.counts;
  announce(status === "none" ? `Status cleared: ${job.title}` : `${job.title}: ${STATUS_LABELS[status]}`);
}

async function moveList(job: JobCard, japanese: boolean) {
  const { data } = await api.POST("/jobs/{job_id}/japanese", { params: { path: { job_id: job.id } }, body: { japanese } });
  if (!data) return;
  job.stage = data.stage;
  tabCounts.value = data.counts;
  announce(`${job.title}: moved to the ${japanese ? "Japanese" : "main"} list`);
}

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
];
