<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import { announce } from "../announce";
import { api, type JobList, type JobsQuery } from "../api/client";
import CardButtons from "../components/CardButtons.vue";
import FilterBar from "../components/FilterBar.vue";
import JobTile from "../components/JobTile.vue";
import { insertByScore, localToday, statusName } from "../jobs";

// The jobs table: every job, narrowed by filters, best first. The API holds the filters and says which
// options are on; the choice lives in the query string, so a view survives a reload. The page adds jobs
// as the model finishes them, and asks again after a button changes a job, since the job may no longer
// pass the filters.
const route = useRoute();
const POLL_MS = 10_000;

const list = ref<JobList | null>(null);
const failed = ref(false);
const fresh = ref(new Set<number>());
const today = localToday();

const fetchJobs = (after?: string) =>
  api.GET("/jobs", { params: { query: { ...(route.query as JobsQuery), after } } });

async function load() {
  try {
    const { data } = await fetchJobs();
    failed.value = !data;
    if (data) list.value = data;
  } catch {
    failed.value = true; // the server is down or restarting
  }
}
watch(() => route.query, load, { immediate: true });

// Every 10 seconds, ask for the jobs listed since the last answer.
let timer: number | undefined;
async function poll() {
  try {
    const shown = list.value;
    const { data } = shown ? await fetchJobs(shown.now) : { data: undefined };
    if (shown && data) {
      const arrived = data.jobs.filter((job) => !shown.jobs.some((other) => other.id === job.id));
      shown.jobs = insertByScore(shown.jobs, arrived);
      shown.total += arrived.length;
      shown.now = data.now;
      for (const job of arrived) fresh.value.add(job.id);
      if (arrived.length) announce(`${arrived.length} new job${arrived.length === 1 ? "" : "s"}`);
    }
  } catch {
    // The server is down or restarting; the next check tries again.
  }
  timer = window.setTimeout(poll, POLL_MS);
}
timer = window.setTimeout(poll, POLL_MS);
onUnmounted(() => window.clearTimeout(timer));

const withoutSkill = () => {
  const { skill: _, ...rest } = route.query;
  return { query: rest };
};
</script>

<template>
  <header class="view-head">
    <h1>
      Jobs
      <span v-if="list" class="count">
        {{ list.total }}<template v-if="list.jobs.length < list.total">, best {{ list.jobs.length }} shown</template>
      </span>
    </h1>
    <FilterBar v-if="list" :filters="list.filters" />
    <RouterLink v-if="route.query.skill" class="chip" :to="withoutSkill()" :aria-label="`Stop filtering by ${route.query.skill}`">
      Skill: {{ route.query.skill }} ×
    </RouterLink>
  </header>

  <p v-if="failed" class="empty">The server did not answer. Is jobhunt serve running?</p>
  <template v-else-if="list">
    <p v-if="!list.jobs.length" class="empty">No jobs match the filters.</p>
    <JobTile v-for="job in list.jobs" :key="job.id" :job="job" :fresh="fresh.has(job.id)" @changed="load">
      <p v-if="job.removal_rule" class="removal">
        Removed: <strong>{{ job.removal_rule }}</strong><template v-if="job.removal_quote">: “{{ job.removal_quote }}”</template>
      </p>
      <p v-if="job.status !== 'none'" class="status-line">
        <strong>{{ statusName(job.status) }}</strong> since {{ job.status_at?.slice(0, 10) }}
        <template v-if="job.follow_up_on">
          · follow up <span :class="{ due: job.follow_up_on <= today }">{{ job.follow_up_on }}</span>
        </template>
      </p>
      <div class="actions">
        <CardButtons :job="job" @changed="load" />
      </div>
    </JobTile>
  </template>
</template>
