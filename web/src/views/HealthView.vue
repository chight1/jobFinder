<script setup lang="ts">
import { ref } from "vue";
import { api, type Health } from "../api/client";

// How each source and the model are doing, and the last backup. "Back up now" and "turn back on"
// act in place; a backup commits and pushes the data, so it can take a few seconds.
const health = ref<Health | null>(null);
const failed = ref(false);
const backingUp = ref(false);

async function load() {
  try {
    const { data } = await api.GET("/health");
    failed.value = !data;
    if (data) health.value = data;
  } catch {
    failed.value = true; // the server is down or restarting
  }
}
load();

async function backUp() {
  backingUp.value = true;
  try {
    const { data } = await api.POST("/backup");
    if (data && health.value) health.value.backup = data;
  } catch {
    // The server is down or restarting; clicking again retries.
  } finally {
    backingUp.value = false;
  }
}

async function turnBackOn(id: string) {
  try {
    const { response } = await api.POST("/sources/{source_id}/enable", { params: { path: { source_id: id } } });
    if (response.ok) await load();
  } catch {
    // The server is down or restarting; clicking again retries.
  }
}

const minute = (iso: string | null) => iso?.slice(0, 16).replace("T", " ") ?? "";
</script>

<template>
  <header class="view-head"><h1>Health</h1></header>

  <p v-if="failed" class="empty">The server did not answer. Is jobhunt serve running?</p>
  <template v-else-if="health">
    <section>
      <h2>Model</h2>
      <p>
        <template v-if="health.seconds_per_job">{{ health.seconds_per_job.toFixed(1) }} s per job</template>
        <template v-else>No extractions measured yet</template>
        · {{ health.queue }} queued
        <template v-if="health.nights != null"> · {{ health.nights.toFixed(1) }} nights to clear</template>
        <template v-for="flag in health.model_flags" :key="flag"> · <span class="flag">{{ flag }}</span></template>
      </p>
      <p v-if="health.last_run">
        Last run {{ minute(health.last_run.started_at) }}: {{ health.last_run.fetched }} requests,
        {{ health.last_run.new_sightings }} new sightings, {{ health.last_run.new_jobs }} new jobs,
        {{ health.last_run.removed_by_code }} removed by code, {{ health.last_run.extracted }} extracted,
        {{ health.last_run.removed_after_extraction }} removed after extraction, {{ health.last_run.failed }} failed,
        {{ health.last_run.closed }} closed.
      </p>
      <p>
        Boards:
        <template v-if="Object.keys(health.boards).length">
          {{ Object.entries(health.boards).map(([tier, n]) => `${tier} ${n}`).join(" · ") }}
        </template>
        <template v-else>None yet</template>
      </p>
    </section>

    <section>
      <h2>Backup</h2>
      <p>
        <template v-if="health.backup.at">
          Last backup {{ minute(health.backup.at) }} UTC:
          <span :class="{ flag: health.backup.status?.startsWith('failed') }">{{ health.backup.status }}</span>
        </template>
        <template v-else>No backup yet.</template>
        · to {{ health.backup.folder }}
      </p>
      <button type="button" :disabled="backingUp" @click="backUp">
        {{ backingUp ? "Backing up…" : "Back up now" }}
      </button>
    </section>

    <section>
      <h2>Sources</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Source</th><th>Last run</th><th>Status</th><th>Requests</th><th>Parsed</th><th>New jobs</th>
              <th>Errors</th><th>7-night avg</th><th>Backoff</th><th>Flags</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in health.sources" :key="s.id" :class="{ flagged: s.flags.length }">
              <td>{{ s.name }}<template v-if="!s.enabled"> (off)</template></td>
              <td>{{ minute(s.last_run_at) }}</td>
              <td>{{ s.last_status ?? "" }}</td>
              <td>{{ s.requests }}</td>
              <td>{{ s.parsed }}</td>
              <td>{{ s.new_jobs }}</td>
              <td>{{ s.errors }}</td>
              <td>{{ s.average == null ? "—" : s.average.toFixed(1) }}</td>
              <td>{{ minute(s.backoff_until) }}<template v-if="s.min_delay_s"> · gap {{ s.min_delay_s }}s</template></td>
              <td>
                <template v-for="flag in s.flags" :key="flag"><span class="flag">{{ flag }}</span> </template>
                <button v-if="s.fail_streak" type="button" @click="turnBackOn(s.id)">Turn back on</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </template>
</template>
