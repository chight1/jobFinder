<script setup lang="ts">
import { ref } from "vue";
import { RouterLink } from "vue-router";
import { api, type JobDetail } from "../api/client";
import { capitalized, statusName } from "../jobs";

// One job in full: its state, score parts, the model's facts and evidence, every place it was seen,
// its model calls and duplicate checks, and the description.
const props = defineProps<{ id: number }>();
const job = ref<JobDetail | null>(null);
const problem = ref("");

async function load() {
  try {
    const { data, response } = await api.GET("/jobs/{job_id}", { params: { path: { job_id: props.id } } });
    if (data) {
      job.value = data;
      document.title = `Job #${data.id} · jobFinder`;
    } else {
      problem.value = response.status === 404 ? `There is no job #${props.id}.` : "The server could not load this job.";
    }
  } catch {
    problem.value = "The server did not answer. Is jobfinder serve running?";
  }
}
load();

const day = (iso: string | null) => iso?.slice(0, 10) ?? "";
const minute = (iso: string | null) => iso?.slice(0, 16).replace("T", " ") ?? "";

/** A fact as text: lists joined, objects as JSON. null shows as a muted "null" in the template. */
function factText(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return String(value);
}
</script>

<template>
  <p v-if="problem" class="empty">{{ problem }}</p>
  <template v-else-if="job">
    <header class="view-head">
      <h1>
        <a v-if="job.url" :href="job.url" target="_blank" rel="noreferrer">{{ job.title }}</a>
        <template v-else>Job</template>
        <span class="count">#{{ job.id }}</span>
      </h1>
    </header>
    <p class="meta">
      {{ job.company }}<template v-if="job.location"> · {{ job.location }}</template>
      <span v-for="badge in job.badges" :key="badge" class="badge" :class="badge">{{ capitalized(badge) }}</span>
    </p>

    <section>
      <h2>State</h2>
      <dl class="kv">
        <dt>Stage</dt>
        <dd>{{ job.stage }}</dd>
        <template v-if="job.removal_rule">
          <dt>Removed by</dt>
          <dd>
            <strong>{{ job.removal_rule }}</strong><template v-if="job.removal_quote">: “{{ job.removal_quote }}”</template>
          </dd>
        </template>
        <template v-if="job.restored_rules.length">
          <dt>Restored from</dt>
          <dd>{{ job.restored_rules.join(", ") }}</dd>
        </template>
        <dt>Rating</dt>
        <dd>
          <template v-if="job.rating">
            <span class="dot inline" :class="job.rating" aria-pressed="true"></span> {{ capitalized(job.rating) }}
          </template>
          <span v-else class="muted">Not rated</span>
        </dd>
        <dt>Status</dt>
        <dd>
          {{ statusName(job.status) }}<template v-if="job.status_at"> since {{ day(job.status_at) }}</template>
          <template v-if="job.follow_up_on"> · follow up {{ job.follow_up_on }}</template>
        </dd>
        <dt>First seen</dt>
        <dd>{{ minute(job.first_seen_at) }}<template v-if="job.listed_at"> · listed {{ minute(job.listed_at) }}</template></dd>
        <dt>Queue order</dt>
        <dd>prescore {{ job.prescore }}</dd>
        <dt>Dedupe key</dt>
        <dd>{{ job.dedupe_key.join(" · ") }}</dd>
      </dl>
    </section>

    <section v-if="job.score != null">
      <h2>Score {{ job.score.toFixed(1) }}</h2>
      <dl class="kv">
        <template v-for="(value, part) in job.score_parts" :key="part">
          <dt>{{ part }}</dt>
          <dd>{{ value.toFixed(2) }}</dd>
        </template>
      </dl>
      <p v-if="job.reason_line" class="reason">{{ job.reason_line }}</p>
    </section>

    <section>
      <h2>Facts from the model</h2>
      <template v-if="Object.keys(job.facts).length">
        <dl class="kv">
          <template v-for="(value, name) in job.facts" :key="name">
            <dt>{{ name }}</dt>
            <dd><span v-if="value === null" class="muted">null</span><template v-else>{{ factText(value) }}</template></dd>
          </template>
        </dl>
        <template v-if="Object.keys(job.evidence).length">
          <h3>Evidence</h3>
          <dl class="kv">
            <template v-for="(quote, name) in job.evidence" :key="name">
              <dt>{{ name }}</dt>
              <dd>“{{ quote }}”</dd>
            </template>
          </dl>
        </template>
      </template>
      <p v-else class="empty">
        Not extracted<template v-if="job.stage === 'extract_failed'">: the model gave no valid answer</template>.
      </p>
    </section>

    <section>
      <h2>Seen at <span class="count">{{ job.sightings.length }}</span></h2>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>Source</th><th>Title</th><th>Company</th><th>Location</th><th>First seen</th><th>Last seen</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in job.sightings" :key="s.id">
              <td>
                <a :href="s.url" target="_blank" rel="noreferrer">{{ s.source }}</a><template v-if="s.primary"> (primary)</template>
              </td>
              <td>{{ s.title }}</td>
              <td>{{ s.company }}</td>
              <td>{{ s.location }}</td>
              <td>{{ day(s.first_seen_at) }}</td>
              <td>{{ day(s.last_seen_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="Object.keys(job.source_fields).length" class="meta">
        Source fields: {{ JSON.stringify(job.source_fields) }}
      </p>
    </section>

    <section>
      <h2>Model calls <span class="count">{{ job.model_calls.length }}</span></h2>
      <ul v-if="job.model_calls.length" class="plain">
        <li v-for="(call, i) in job.model_calls" :key="i">
          {{ call.at.slice(0, 19).replace("T", " ") }} · {{ call.kind }} · {{ call.seconds?.toFixed(1) ?? "?" }} s ·
          <template v-if="call.ok">ok</template><span v-else class="flag">{{ call.error || "failed" }}</span>
        </li>
      </ul>
      <p v-else class="empty">None yet.</p>
      <p v-for="pair in job.pairs" :key="`${pair.job_a}-${pair.job_b}`" class="meta">
        Possible duplicate: <RouterLink :to="`/jobs/${pair.job_a}`">#{{ pair.job_a }}</RouterLink> /
        <RouterLink :to="`/jobs/${pair.job_b}`">#{{ pair.job_b }}</RouterLink>
        · {{ pair.verdict || "waiting for the model" }}<template v-if="pair.decided_by"> ({{ pair.decided_by }})</template>
      </p>
    </section>

    <section>
      <h2>Description</h2>
      <pre v-if="job.description" class="packet">{{ job.description }}</pre>
      <p v-else class="empty">No description stored.</p>
    </section>
  </template>
</template>
