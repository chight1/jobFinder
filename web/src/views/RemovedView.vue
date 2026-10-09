<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, type JobCard, type JobList } from "../api/client";
import JobTile from "../components/JobTile.vue";
import { tabCounts } from "../tabCounts";

// Removed jobs, newest first, each with the rule that removed it and the quote behind it. The rule
// filter lives in the query string (?rule=contract). Restoring puts a job back, and the same rule never
// removes it again; a code-removed job first fetches its posting, so that can take a moment.
const route = useRoute();
const router = useRouter();
const rule = computed(() => (typeof route.query.rule === "string" ? route.query.rule : ""));

const list = ref<JobList | null>(null);
const failed = ref(false);
const restoring = ref(new Set<number>());

async function load() {
  try {
    const { data } = await api.GET("/jobs", { params: { query: { view: "removed", rule: rule.value || undefined } } });
    failed.value = !data;
    if (data) list.value = data;
  } catch {
    failed.value = true; // the server is down or restarting
  }
}
watch(rule, load, { immediate: true });

const pickRule = (name: string) => router.push({ query: name ? { rule: name } : {} });

async function restore(job: JobCard) {
  restoring.value.add(job.id);
  try {
    const { data } = await api.POST("/jobs/{job_id}/restore", { params: { path: { job_id: job.id } } });
    if (data && list.value) {
      list.value.jobs = list.value.jobs.filter((other) => other.id !== job.id);
      tabCounts.value = data.counts;
    }
  } catch {
    // The server is down or restarting; clicking again retries.
  } finally {
    restoring.value.delete(job.id);
  }
}
</script>

<template>
  <header class="view-head">
    <h1>Removed <span v-if="list" class="count">{{ list.jobs.length }}</span></h1>
    <select
      :value="rule"
      aria-label="Removal rule"
      @change="pickRule(($event.target as HTMLSelectElement).value)"
    >
      <option value="">every rule</option>
      <option v-for="name in list?.rules" :key="name" :value="name">{{ name }}</option>
    </select>
  </header>

  <p v-if="failed" class="empty">The server did not answer. Is jobhunt serve running?</p>
  <template v-else-if="list">
    <p v-if="!list.jobs.length" class="empty">Nothing removed.</p>
    <JobTile v-for="job in list.jobs" :key="job.id" :job="job">
      <p class="removal">
        <strong>{{ job.removal_rule }}</strong><template v-if="job.removal_quote">: “{{ job.removal_quote }}”</template>
      </p>
      <div class="actions">
        <button type="button" :disabled="restoring.has(job.id)" @click="restore(job)">
          {{ restoring.has(job.id) ? "restoring…" : "restore" }}
        </button>
      </div>
    </JobTile>
  </template>
</template>
