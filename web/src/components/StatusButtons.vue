<script setup lang="ts">
import { api, type JobCard, type StatusSaved } from "../api/client";
import { STATUS_BUTTONS, type Status } from "../jobs";
import { tabCounts } from "../tabCounts";

// A job's status as one segmented control: the current status is filled in, and clicking it again
// clears it. The page decides what a new status means for its list: the job stays, or leaves.
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
  <div class="actions">
    <div class="segmented" role="group" aria-label="Status">
      <button
        v-for="button in STATUS_BUTTONS"
        :key="button.status"
        type="button"
        :aria-pressed="job.status === button.status"
        :title="button.hint"
        @click="set(job.status === button.status ? 'none' : button.status)"
      >{{ button.label }}</button>
    </div>
  </div>
</template>
