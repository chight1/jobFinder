<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { announce } from "../announce";
import { api, type JobCard } from "../api/client";
import { RATINGS, type Rating } from "../jobs";

// One job as a card. Every card has the same layout, so the eye learns where each thing is:
// the score in its box; title, then company and pay; one line of facts (where, language and your
// level in it, years asked); the skills; the page's own actions (the slot); then where it was seen
// and a link to the details. The rating dots save at once.
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
    if (data) {
      rating.value = data.rating ?? null;
      announce(data.rating ? `Rated ${data.rating}` : "Rating cleared");
    }
  } catch {
    // The server is down or restarting; clicking again retries.
  }
}

const workMode = computed(() => {
  const mode = props.job.work_mode;
  return mode ? mode[0]!.toUpperCase() + mode.slice(1) : "Work mode unknown";
});
const levelTitle = computed(() =>
  props.job.language_level ? `your level: ${props.job.language_level} of 3` : "not in your profile",
);
</script>

<template>
  <article class="job" :class="{ fresh }" :data-rating="rating ?? ''">
    <div class="score" title="score"><span class="sr-only">Score </span>{{ Math.round(job.score ?? 0) }}</div>
    <div class="body">
      <header class="job-head">
        <div class="job-title">
          <h2>
            <a :href="job.url" target="_blank" rel="noreferrer">{{ job.title }}</a>
            <span v-for="badge in job.badges" :key="badge" class="badge" :class="badge">{{ badge }}</span>
          </h2>
          <p class="company-line">
            <span class="company">{{ job.company || "Company not given" }}</span>
            <span v-if="job.pay_usd" class="pay" :title="job.pay">{{ job.pay_usd }}</span>
            <span v-else class="pay none">pay not listed</span>
          </p>
        </div>
        <div class="rate" role="group" aria-label="Rate this job">
          <button
            v-for="color in RATINGS"
            :key="color"
            type="button"
            class="dot"
            :class="color"
            :title="color"
            :aria-label="`Rate ${color}`"
            :aria-pressed="rating === color"
            @click="rate(color)"
          ></button>
        </div>
      </header>

      <p class="facts">
        <span class="fact">{{ workMode }}<template v-if="job.place"> · {{ job.place }}</template></span>
        <span v-if="job.primary_language" class="fact">
          {{ job.primary_language }}
          <span class="level-bars" :data-level="job.language_level" :title="levelTitle" aria-hidden="true">
            <i v-for="n in 3" :key="n" :class="{ on: n <= job.language_level }"></i>
          </span>
          <span class="sr-only">, {{ levelTitle }}</span>
        </span>
        <span
          v-if="job.years_asked != null"
          class="fact"
          :class="{ over: job.years_over }"
          :title="job.years_over ? 'asks for more years than you have' : 'years of experience asked'"
        >{{ job.years_asked }} yrs<span v-if="job.years_over" class="sr-only">, more than you have</span></span>
      </p>

      <p v-if="job.skills.length" class="skills">
        <RouterLink
          v-for="(skill, i) in job.skills"
          :key="i"
          class="chip"
          :data-level="skill.level"
          :to="`/skills/${encodeURIComponent(skill.key)}`"
          :title="skill.level ? `in your profile, level ${skill.level}` : 'not in your profile'"
        >{{ skill.name }}<span v-if="skill.level" class="sr-only">, your level {{ skill.level }}</span></RouterLink>
      </p>

      <slot />

      <footer class="job-foot">
        <span class="sources">
          via {{ job.source }}
          <template v-if="job.others.length">
            · also
            <template v-for="(other, i) in job.others" :key="other.url">
              <a :href="other.url" target="_blank" rel="noreferrer">{{ other.name }}</a>{{ i < job.others.length - 1 ? ", " : "" }}
            </template>
          </template>
          <template v-if="job.probably_same.length">
            · probably the same as
            <template v-for="(same, i) in job.probably_same" :key="same">
              <RouterLink :to="`/jobs/${same}`">another job</RouterLink>{{ i < job.probably_same.length - 1 ? ", " : "" }}
            </template>
          </template>
          <template v-if="job.has_packet"> · <RouterLink :to="`/packets/${job.id}`">packet</RouterLink></template>
        </span>
        <RouterLink class="details" :to="`/jobs/${job.id}`" :aria-label="`Details for ${job.title}`">
          Details <span aria-hidden="true">→</span>
        </RouterLink>
      </footer>
    </div>
  </article>
</template>
