<script setup lang="ts">
import { onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { api, type Activity } from "../api/client";
import { browserZone, clockTime } from "../time";

// What is running right now, queue progress, the latest web requests and model calls, and the end of the
// log. It asks the server again every 5 seconds while the page is open.
const POLL_MS = 5_000;
const activity = ref<Activity | null>(null);
const failed = ref(false);

let timer: number | undefined;
async function refresh() {
  try {
    const { data } = await api.GET("/activity");
    failed.value = !data;
    if (data) activity.value = data;
  } catch {
    failed.value = true; // the server is down or restarting; the next check tries again
  }
  timer = window.setTimeout(refresh, POLL_MS);
}
refresh();
onUnmounted(() => window.clearTimeout(timer));

const jobId = (detail: string) => detail.slice(1); // a model call's detail is "#123"
const zone = browserZone();
</script>

<template>
  <header class="view-head">
    <h1>Activity</h1>
    <span class="meta">updates every 5 seconds</span>
  </header>

  <p v-if="failed && !activity" class="empty">The server did not answer. Is jobhunt serve running?</p>
  <template v-if="activity">
    <section>
      <h2>Running now</h2>
      <ul v-if="activity.running.length" class="plain">
        <li v-for="item in activity.running" :key="item.worker" :class="{ flag: item.stale }">
          <strong>{{ item.worker }}</strong>: {{ item.doing }} · {{ item.age }}
          <template v-if="item.stale"> · no update in a long time; it may be stuck</template>
        </li>
      </ul>
      <p v-else class="empty">Nothing is running.</p>
    </section>

    <section>
      <h2>Progress</h2>
      <p>
        {{ activity.progress.queue }} jobs waiting for the model
        · {{ activity.progress.extracted_last_hour }} extracted in the last hour
        <template v-if="activity.progress.seconds_per_job">
          · {{ Math.round(activity.progress.seconds_per_job) }} s per job
        </template>
        <template v-if="activity.progress.time_to_clear">
          · about {{ activity.progress.time_to_clear }} of model time to clear the queue
        </template>
        · {{ activity.progress.requests_last_hour }} web requests in the last hour
      </p>
    </section>

    <section>
      <h2>Latest events</h2>
      <div v-if="activity.feed.length" class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Time ({{ zone }})</th>
              <th>Kind</th>
              <th>Who</th>
              <th>Result</th>
              <th>Seconds</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(event, i) in activity.feed" :key="i" :class="{ flagged: event.bad }">
              <td>{{ clockTime(event.at, zone) }}</td>
              <td>{{ event.kind }}</td>
              <td>{{ event.who }}</td>
              <td>{{ event.what }}</td>
              <td>{{ (event.seconds ?? 0).toFixed(1) }}</td>
              <td class="detail">
                <RouterLink v-if="event.kind === 'model' && event.detail" :to="`/jobs/${jobId(event.detail)}`">
                  {{ event.detail }}
                </RouterLink>
                <template v-else>{{ event.detail }}</template>
                <template v-if="event.error"> · {{ event.error }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="empty">No requests or model calls yet.</p>
    </section>

    <section>
      <h2>Log</h2>
      <pre v-if="activity.log.length" class="packet log">{{ activity.log.join("\n") }}</pre>
      <p v-else class="empty">The log is empty.</p>
    </section>
  </template>
</template>
