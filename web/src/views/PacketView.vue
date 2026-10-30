<script setup lang="ts">
import { ref } from "vue";
import { api } from "../api/client";

// The application packet drafted for a job marked apply, as the model wrote it.
const props = defineProps<{ id: number }>();
const text = ref<string | null>(null);
const problem = ref("");

async function load() {
  try {
    const { data, response } = await api.GET("/packets/{job_id}", { params: { path: { job_id: props.id } } });
    if (data) text.value = data.text;
    else problem.value = response.status === 404 ? "No packet for this job yet." : "The server could not load it.";
  } catch {
    problem.value = "The server did not answer. Is jobfinder serve running?";
  }
}
load();
</script>

<template>
  <header class="view-head"><h1>Packet {{ id }}</h1></header>
  <p v-if="problem" class="empty">{{ problem }}</p>
  <pre v-else-if="text !== null" class="packet">{{ text }}</pre>
</template>
