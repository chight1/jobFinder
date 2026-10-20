<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink } from "vue-router";
import { announce } from "../announce";
import { api, type JobCard } from "../api/client";
import type { Undo } from "../cardButtons";
import { capitalized, RATINGS, skillLink, type Rating } from "../jobs";

// One job as a card. Every card has the same layout, so the eye learns where each thing is:
// the score in its box; title, then company and pay; one line of facts (where, language and your
// level in it, years asked); the skills; the page's own actions (the slot); then where it was seen
// and a link to the details. The rating dots save at once; the page then hears of the change, with
// its undo, since its filters may no longer let the job through.
const props = defineProps<{ job: JobCard; fresh?: boolean }>();
const emit = defineEmits<{ changed: [undo: Undo] }>();

/** Picking a dot's color again clears it, so one post sets a color or clears it. */
async function postRating(color: Rating): Promise<boolean> {
  const { data } = await api.POST("/jobs/{job_id}/rating", {
    params: { path: { job_id: props.job.id } },
    body: { rating: color },
  });
  if (data) props.job.rating = data.rating ?? null;
  return Boolean(data);
}

async function rate(color: Rating) {
  const before = props.job.rating;
  try {
    if (!(await postRating(color))) return;
    const message = props.job.rating ? `Rated ${props.job.rating}: ${props.job.title}` : `Rating cleared: ${props.job.title}`;
    announce(message);
    // Unrated before: posting the new color again clears it. Rated before: posting the old one sets it.
    emit("changed", { message, revert: () => postRating(before ?? color) });
  } catch {
    // The server is down or restarting; clicking again retries.
  }
}

// The posting's link to the clipboard; the icon turns to a check for a moment.
const copied = ref(false);
async function copyLink() {
  try {
    await navigator.clipboard.writeText(props.job.url);
    copied.value = true;
    announce("Link copied");
    setTimeout(() => (copied.value = false), 1500);
  } catch {
    announce("Could not copy the link");
  }
}

const workMode = computed(() => {
  const mode = props.job.work_mode;
  return mode ? mode[0]!.toUpperCase() + mode.slice(1) : "Work mode unknown";
});
const levelTitle = computed(() =>
  props.job.language_level ? `Your level: ${props.job.language_level} of 3` : "Not in your profile",
);
</script>

<template>
  <article class="job" :class="{ fresh }" :data-rating="job.rating ?? ''">
    <div class="score" title="score"><span class="sr-only">Score </span>{{ Math.round(job.score ?? 0) }}</div>
    <div class="body">
      <header class="job-head">
        <div class="job-title">
          <h2>
            <button
              type="button"
              class="copy-link"
              :title="copied ? 'Copied' : 'Copy link'"
              :aria-label="copied ? 'Link copied' : 'Copy link'"
              @click="copyLink"
            >
              <svg v-if="!copied" viewBox="0 0 16 16" aria-hidden="true">
                <rect x="5" y="5" width="8.5" height="8.5" rx="1.5" />
                <path d="M3 10.5V3.5A1.5 1.5 0 0 1 4.5 2h6" />
              </svg>
              <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" /></svg>
            </button>
            <a :href="job.url" target="_blank" rel="noreferrer">{{ job.title }}</a>
            <span v-for="badge in job.badges" :key="badge" class="badge" :class="badge">{{ capitalized(badge) }}</span>
          </h2>
          <p class="company-line">
            <span class="company">{{ job.company || "Company not given" }}</span>
            <span v-if="job.pay_usd" class="pay" :title="job.pay">{{ job.pay_usd }}</span>
            <span v-else class="pay none">Pay not listed</span>
          </p>
        </div>
        <div class="rate" role="group" aria-label="Rate this job">
          <button
            v-for="color in RATINGS"
            :key="color"
            type="button"
            class="dot"
            :class="color"
            :title="`Rate ${color}`"
            :aria-label="`Rate ${color}`"
            :aria-pressed="job.rating === color"
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
          :title="job.years_over ? 'Asks for more years than you have' : 'Years of experience asked'"
        >{{ job.years_asked }} yrs<span v-if="job.years_over" class="sr-only">, more than you have</span></span>
      </p>

      <p v-if="job.skills.length" class="skills">
        <RouterLink
          v-for="(skill, i) in job.skills"
          :key="i"
          class="chip"
          :data-level="skill.level"
          :to="skillLink(skill.key)"
          :title="skill.level ? `In your profile, level ${skill.level}` : 'Not in your profile'"
        >{{ skill.name }}<span v-if="skill.level" class="sr-only">, your level {{ skill.level }}</span></RouterLink>
      </p>

      <slot />

      <footer class="job-foot">
        <span class="sources">
          Via {{ job.source }}
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
