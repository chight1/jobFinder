<script setup lang="ts">
import { ref } from "vue";
import { announce } from "../announce";
import { api, type BlacklistEntry } from "../api/client";

// Employers blacklisted from a job card. Removing one brings its jobs back where they were.
const entries = ref<BlacklistEntry[] | null>(null);
const failed = ref(false);
const busy = ref<string | null>(null);

async function load() {
  try {
    const { data } = await api.GET("/blacklist");
    failed.value = !data;
    if (data) entries.value = data;
  } catch {
    failed.value = true; // the server is down or restarting
  }
}
load();

async function remove(entry: BlacklistEntry) {
  busy.value = entry.company_norm;
  try {
    const { data } = await api.POST("/blacklist/remove", { body: { company_norm: entry.company_norm } });
    if (!data) return;
    announce(`${entry.company} is off the blacklist: ${data.restored} ${data.restored === 1 ? "job" : "jobs"} back`);
    await load();
  } catch {
    // The server is down or restarting; clicking again retries.
  } finally {
    busy.value = null;
  }
}

const day = (iso: string) => iso.slice(0, 10);
</script>

<template>
  <header class="view-head"><h1>Blacklist</h1></header>

  <p v-if="failed" class="empty">The server did not answer. Is jobfinder serve running?</p>
  <template v-else-if="entries">
    <p v-if="!entries.length" class="empty">No employers blacklisted. A job card's Blacklist button adds one.</p>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr><th>Employer</th><th>Added</th><th>Jobs hidden</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in entries" :key="entry.company_norm">
            <td>{{ entry.company }}</td>
            <td>{{ day(entry.added_at) }}</td>
            <td>{{ entry.hidden }}</td>
            <td>
              <button type="button" :disabled="busy === entry.company_norm" @click="remove(entry)">Remove</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
