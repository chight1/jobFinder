<script setup lang="ts">
import { onUnmounted, ref } from "vue";
import { announce } from "../announce";
import { api, type JobCard, type JobList, type StatusSaved } from "../api/client";
import JobTile from "../components/JobTile.vue";
import StatusButtons from "../components/StatusButtons.vue";
import { APPLIED_STATUSES, insertByScore, localToday, statusName, type Status } from "../jobs";
import { refreshTabCounts } from "../tabCounts";

type ListView = "new" | "all" | "japanese" | "applied" | "not_interested" | "skill";
const props = defineProps<{ view: ListView; skill?: string; japanese?: boolean }>();

// What each list shows. `holds` are the statuses its jobs can have: a job given another status leaves
// the list. A `live` list adds jobs as the model finishes them, without a reload.
const VIEWS: Record<ListView, { title: string; empty: string; holds: Status[]; live?: boolean }> = {
  new: { title: "New", empty: "Nothing new.", holds: ["none"], live: true },
  all: { title: "All open", empty: "Nothing here.", holds: ["none"], live: true },
  japanese: { title: "Japanese", empty: "No Japanese jobs yet.", holds: ["none"] },
  applied: { title: "Applied", empty: "Nothing applied for yet.", holds: APPLIED_STATUSES },
  not_interested: { title: "Not interested", empty: "Nothing marked not interested.", holds: ["not_interested"] },
  skill: { title: `Jobs asking for ${props.skill}`, empty: "No open jobs ask for it.", holds: ["none"] },
};
const config = VIEWS[props.view];
const POLL_MS = 10_000;

const list = ref<JobList | null>(null);
const failed = ref(false);
const fresh = ref(new Set<number>());
const today = localToday();

function fetchJobs(after?: string) {
  if (props.view === "skill") {
    return api.GET("/skills/{key}/jobs", {
      params: { path: { key: props.skill ?? "" }, query: { japanese: props.japanese } },
    });
  }
  return api.GET("/jobs", { params: { query: { view: props.view, after } } });
}

async function load() {
  try {
    const { data } = await fetchJobs();
    failed.value = !data;
    if (data) list.value = data;
  } catch {
    failed.value = true; // the server is down or restarting
  }
}

// A live list asks every 10 seconds for the jobs listed since its last answer.
let timer: number | undefined;
async function poll() {
  try {
    const shown = list.value;
    const { data } = shown ? await fetchJobs(shown.now) : { data: undefined };
    if (shown && data) {
      const arrived = data.jobs.filter((job) => !shown.jobs.some((other) => other.id === job.id));
      shown.jobs = insertByScore(shown.jobs, arrived);
      shown.now = data.now;
      for (const job of arrived) fresh.value.add(job.id);
      if (arrived.length) {
        announce(`${arrived.length} new job${arrived.length === 1 ? "" : "s"}`);
        await refreshTabCounts();
      }
    }
  } catch {
    // The server is down or restarting; the next check tries again.
  }
  timer = window.setTimeout(poll, POLL_MS);
}

load();
if (config.live) timer = window.setTimeout(poll, POLL_MS);
onUnmounted(() => window.clearTimeout(timer));

function statusSaved(job: JobCard, reply: StatusSaved) {
  if (!list.value) return;
  if (config.holds.includes(reply.status)) {
    Object.assign(job, { status: reply.status, status_at: reply.status_at, follow_up_on: reply.follow_up_on });
  } else {
    list.value.jobs = list.value.jobs.filter((other) => other.id !== job.id);
  }
  announce(reply.status === "none" ? `Status cleared: ${job.title}` : `${job.title}: ${statusName(reply.status)}`);
}

async function markSeen() {
  const { response } = await api.POST("/seen");
  if (!response.ok || !list.value) return;
  list.value.jobs = [];
  announce("Marked all seen");
  await refreshTabCounts();
}
</script>

<template>
  <header class="view-head">
    <h1>{{ config.title }} <span v-if="list" class="count">{{ list.jobs.length }}</span></h1>
    <button v-if="view === 'new'" type="button" @click="markSeen">Mark all seen</button>
  </header>

  <p v-if="failed" class="empty">The server did not answer. Is jobhunt serve running?</p>
  <template v-else-if="list">
    <p v-if="!list.jobs.length" class="empty">{{ config.empty }}</p>
    <JobTile v-for="job in list.jobs" :key="job.id" :job="job" :fresh="fresh.has(job.id)">
      <p v-if="job.status !== 'none'" class="status-line">
        <strong>{{ statusName(job.status) }}</strong> since {{ job.status_at?.slice(0, 10) }}
        <template v-if="job.follow_up_on">
          · follow up <span :class="{ due: job.follow_up_on <= today }">{{ job.follow_up_on }}</span>
        </template>
      </p>
      <StatusButtons :job="job" @saved="statusSaved(job, $event)" />
    </JobTile>
  </template>
</template>
