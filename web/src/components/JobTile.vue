<script setup lang="ts">
import { ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { api, type JobCard } from "../api/client";
import { RATINGS, type Rating } from "../jobs";

// One job: score, title, badges, the facts that matter, where it was seen and the skills it asks for.
// The rating dots save at once; each page puts its own actions (status buttons, restore) in the slot.
const props = defineProps<{ job: JobCard; fresh?: boolean }>();
const rating = ref(props.job.rating);
watch(
  () => props.job.rating,
  (value) => (rating.value = value),
);

async function rate(color: Rating) {
  try {
    const { data } = await api.POST("/jobs/{job_id}/rating", {
      params: { path: { job_id: props.job.id } },
      body: { rating: color },
    });
    if (data) rating.value = data.rating ?? null;
  } catch {
    // The server is down or restarting; clicking again retries.
  }
}
</script>

<template>
  <article class="job" :class="{ fresh }" :data-rating="rating ?? ''">
    <div class="score">{{ Math.round(job.score ?? 0) }}</div>
    <div class="body">
      <div class="rate" role="group" aria-label="Rate this job">
        <button
          v-for="color in RATINGS"
          :key="color"
          type="button"
          class="dot"
          :class="color"
          :title="color"
          :aria-pressed="rating === color"
          @click="rate(color)"
        ></button>
      </div>
      <h2>
        <a :href="job.url" target="_blank" rel="noreferrer">{{ job.title }}</a>
        <span v-for="badge in job.badges" :key="badge" class="badge" :class="badge">{{ badge }}</span>
      </h2>
      <p class="meta">
        {{ job.company || "company not given" }}
        · {{ job.work_mode || "work mode ?" }}, {{ job.place || "place not given" }}
        · {{ job.pay }}
        · {{ job.primary_language || "language not given" }}
      </p>
      <p v-if="job.reason_line" class="reason">{{ job.reason_line }}</p>
      <p class="links">
        via {{ job.source }}
        <template v-for="other in job.others" :key="other.url">
          · <a :href="other.url" target="_blank" rel="noreferrer">{{ other.name }}</a>
        </template>
        <template v-for="same in job.probably_same" :key="same"> · probably the same as #{{ same }}</template>
        <template v-if="job.has_packet"> · <a :href="`/packets/${job.id}`">packet</a></template>
        · <a :href="`/jobs/${job.id}`">#{{ job.id }} details</a>
      </p>
      <p v-if="job.skills.length" class="skills">
        <RouterLink
          v-for="(skill, i) in job.skills"
          :key="i"
          class="chip"
          :class="{ have: skill.level }"
          :to="`/skills/${encodeURIComponent(skill.key)}`"
          :title="skill.level ? `in your profile, level ${skill.level}` : 'not in your profile'"
        >{{ skill.name }}</RouterLink>
      </p>
      <slot />
    </div>
  </article>
</template>
