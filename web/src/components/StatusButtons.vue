<script setup lang="ts">
import { api, type JobCard, type StatusSaved } from "../api/client";
import { STATUS_BUTTONS, type Status } from "../jobs";
import { tabCounts } from "../tabCounts";

// The status buttons on a tile. The page decides what a new status means for its list: the job
// stays and shows it, or leaves.
const props = defineProps<{ job: JobCard }>();
const emit = defineEmits<{ saved: [reply: StatusSaved] }>();

async function set(status: Status) {
  try {
    const { data } = await api.POST("/jobs/{job_id}/status", {
      params: { path: { job_id: props.job.id } },
      body: { status },
    });
    if (!data) return;
    tabCounts.value = data.counts;
    emit("saved", data);
  } catch {
    // The server is down or restarting; clicking again retries.
  }
}
</script>

<template>
  <div class="actions status">
    <button
      v-for="button in STATUS_BUTTONS"
      :key="button.status"
      type="button"
      :disabled="job.status === button.status"
      @click="set(button.status)"
    >{{ button.label }}</button>
    <button v-if="job.status !== 'none'" type="button" @click="set('none')">clear status</button>
  </div>
</template>
