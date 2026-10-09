<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import { announce } from "../announce";
import { api, type JobCard, type JobList } from "../api/client";
import CardButtons from "../components/CardButtons.vue";
import FilterBar from "../components/FilterBar.vue";
import JobTile from "../components/JobTile.vue";
import { passes, RATING_FILTER, type Filter } from "../filters";
import { APPLIED_STATUSES, insertByScore, localToday, statusIn, statusName } from "../jobs";
import { refreshTabCounts } from "../tabCounts";

type ListView = "new" | "all" | "japanese" | "applied" | "not_interested" | "skill";
const props = defineProps<{ view: ListView; skill?: string; japanese?: boolean }>();

// What each list shows. `keeps` is the rule for a job on it: a job a button changes so the rule no
// longer holds leaves the list. A `live` list adds jobs as the model finishes them, without a reload.
// `filters` are the ones its filter bar offers.
type ListConfig = {
  title: string;
  empty: string;
  keeps: (job: JobCard) => boolean;
  live?: boolean;
  filters?: Filter[];
};
const open = statusIn("none");
const onMainList = (job: JobCard) => open(job) && (job.stage === "kept" || job.stage === "extract_failed");
const VIEWS: Record<ListView, ListConfig> = {
  new: { title: "New", empty: "Nothing new.", keeps: onMainList, live: true },
  all: { title: "All open", empty: "Nothing here.", keeps: onMainList, live: true, filters: [RATING_FILTER] },
  japanese: { title: "Japanese", empty: "No Japanese jobs yet.", keeps: (job) => open(job) && job.stage === "japanese" },
  applied: { title: "Applied", empty: "Nothing applied for yet.", keeps: statusIn(...APPLIED_STATUSES) },
  not_interested: { title: "Not interested", empty: "Nothing marked not interested.", keeps: statusIn("not_interested") },
  skill: { title: `Jobs asking for ${props.skill}`, empty: "No open jobs ask for it.", keeps: open },
};
const config = VIEWS[props.view];
const filters = config.filters ?? [];
const route = useRoute();
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

// The jobs the filters let through; a rating changed on a card applies at once.
const shown = computed(() => list.value?.jobs.filter((job) => passes(job, filters, route.query)) ?? []);

// A button changed a job: it leaves the list unless the list's rule still holds.
function changed() {
  if (list.value) list.value.jobs = list.value.jobs.filter(config.keeps);
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
    <h1>
      {{ config.title }}
      <span v-if="list" class="count">
        {{ shown.length }}<template v-if="shown.length < list.jobs.length"> of {{ list.jobs.length }}</template>
      </span>
    </h1>
    <FilterBar v-if="filters.length" :filters="filters" />
    <button v-if="view === 'new'" type="button" @click="markSeen">Mark all seen</button>
  </header>

  <p v-if="failed" class="empty">The server did not answer. Is jobhunt serve running?</p>
  <template v-else-if="list">
    <p v-if="!list.jobs.length" class="empty">{{ config.empty }}</p>
    <p v-else-if="!shown.length" class="empty">No jobs match the filters.</p>
    <JobTile v-for="job in shown" :key="job.id" :job="job" :fresh="fresh.has(job.id)">
      <p v-if="job.status !== 'none'" class="status-line">
        <strong>{{ statusName(job.status) }}</strong> since {{ job.status_at?.slice(0, 10) }}
        <template v-if="job.follow_up_on">
          · follow up <span :class="{ due: job.follow_up_on <= today }">{{ job.follow_up_on }}</span>
        </template>
      </p>
      <div class="actions">
        <CardButtons :job="job" @changed="changed" />
      </div>
    </JobTile>
  </template>
</template>
