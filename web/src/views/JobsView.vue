<script setup lang="ts">
import { computed, onUnmounted, ref, shallowRef, watch } from "vue";
import { type LocationQuery, RouterLink, useRoute, useRouter } from "vue-router";
import { announce } from "../announce";
import { api, type JobList, type JobsQuery } from "../api/client";
import type { Undo } from "../cardButtons";
import CardButtons from "../components/CardButtons.vue";
import FilterBar from "../components/FilterBar.vue";
import JobTile from "../components/JobTile.vue";
import UndoBar from "../components/UndoBar.vue";
import { insertByScore, localToday, statusName } from "../jobs";

// The jobs table: every job, narrowed by filters, best first. The API holds the filters and says which
// options are on; the choice lives in the query string, and this browser remembers the last one, so a bare
// /jobs (the Jobs tab) opens where the candidate left off. The page adds jobs as the model finishes them, and asks
// again after a button changes a job, since the job may no longer pass the filters; the undo bar then
// offers to take the change back. The list is best first, or the jobs changed last first.
const route = useRoute();
const router = useRouter();
const POLL_MS = 10_000;
const SAVED_KEY = "jobs.query";

const list = ref<JobList | null>(null);
const failed = ref(false);
const fresh = ref(new Set<number>());
const today = localToday();

const SORTS = [
  { key: "score", label: "Best first" },
  { key: "changed", label: "Recently changed" },
] as const;
const sort = computed(() => (route.query.sort === "changed" ? "changed" : "score"));
const sortBy = (key: (typeof SORTS)[number]["key"]) =>
  router.replace({ query: { ...route.query, sort: key === "score" ? undefined : key } });

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

// Storage can be blocked (a private window, say); the page then just starts from the defaults.
function remember() {
  try {
    localStorage.setItem(SAVED_KEY, JSON.stringify(route.query));
  } catch {}
}
function remembered(): LocationQuery {
  try {
    return JSON.parse(localStorage.getItem(SAVED_KEY) ?? "{}");
  } catch {
    return {};
  }
}

const saved = Object.keys(route.query).length ? {} : remembered();
if (Object.keys(saved).length) router.replace({ query: saved }); // the watch below loads it
else {
  remember();
  load();
}
watch(() => route.query, () => {
  remember();
  load();
});

// The last change made from a card, for the undo bar; a change without an undo (Restore) clears it.
const undo = shallowRef<Undo | null>(null);
function changed(change?: Undo) {
  undo.value = change ?? null;
  load();
}
function undoDone(undone: boolean) {
  undo.value = null;
  if (undone) load();
}

// Every 10 seconds, ask for the jobs listed since the last answer. Sorted by changes, new jobs would
// land at the bottom anyway, so they wait for the next load.
let timer: number | undefined;
async function poll() {
  try {
    const shown = sort.value === "score" ? list.value : null;
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

// The first three postings' links, one per line, to paste somewhere in one go.
const TOP_LINKS = 3;
const copiedTop = ref(false);
async function copyTopLinks() {
  const urls = (list.value?.jobs ?? []).slice(0, TOP_LINKS).map((job) => job.url);
  try {
    await navigator.clipboard.writeText(urls.join("\n"));
    copiedTop.value = true;
    announce(`Copied ${urls.length} ${urls.length === 1 ? "link" : "links"}`);
    setTimeout(() => (copiedTop.value = false), 1500);
  } catch {
    announce("Could not copy the links");
  }
}

const withoutSkill = () => {
  const { skill: _, ...rest } = route.query;
  return { query: rest };
};
</script>

<template>
  <!-- Filters in a rail on the left; the results bar above the list says how many and holds what
       acts on the list as a whole. On a narrow window the rail sits on top. -->
  <div class="jobs-layout">
    <aside class="filter-rail" aria-label="Filters">
      <FilterBar v-if="list" :filters="list.filters" />
      <RouterLink v-if="route.query.skill" class="chip" :to="withoutSkill()" :aria-label="`Stop filtering by ${route.query.skill}`">
        Skill: {{ route.query.skill }} ×
      </RouterLink>
    </aside>

    <div class="jobs-main">
      <header class="results-bar">
        <h1>
          Jobs
          <span v-if="list" class="count">
            {{ list.total }}<template v-if="list.jobs.length < list.total">, best {{ list.jobs.length }} shown</template>
          </span>
        </h1>
        <div class="results-actions">
          <div class="segmented" role="group" aria-label="Sort">
            <button
              v-for="option in SORTS"
              :key="option.key"
              type="button"
              :aria-pressed="sort === option.key"
              @click="sortBy(option.key)"
            >{{ option.label }}</button>
          </div>
          <button type="button" :disabled="!list?.jobs.length" @click="copyTopLinks">
            {{ copiedTop ? "Copied" : `Copy top ${TOP_LINKS} links` }}
          </button>
        </div>
      </header>

      <p v-if="failed" class="empty">The server did not answer. Is jobfinder serve running?</p>
      <template v-else-if="list">
        <p v-if="!list.jobs.length" class="empty">No jobs match the filters.</p>
        <JobTile v-for="job in list.jobs" :key="job.id" :job="job" :fresh="fresh.has(job.id)" @changed="changed">
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
            <CardButtons :job="job" @changed="changed" />
          </div>
        </JobTile>
      </template>
    </div>
  </div>
  <UndoBar :undo="undo" @done="undoDone" />
</template>
