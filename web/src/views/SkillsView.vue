<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { api, type SkillsPage } from "../api/client";
import LevelPicker from "../components/LevelPicker.vue";
import QueryLink from "../components/QueryLink.vue";

// Every skill the jobs name, most common first, with your level beside each. The filters live in the
// query string (?scope=all&japanese=1), so a filtered view survives a reload and can be linked to.
const route = useRoute();
const scope = computed(() => (route.query.scope === "all" ? "all" : "kept"));
const japanese = computed(() => route.query.japanese === "1");
const query = (changes: { scope?: string; japanese?: boolean }) => ({
  scope: changes.scope ?? scope.value,
  japanese: (changes.japanese ?? japanese.value) ? "1" : undefined,
});

const page = ref<SkillsPage | null>(null);
const failed = ref(false);

async function load() {
  try {
    const { data } = await api.GET("/skills", {
      params: { query: { scope: scope.value, japanese: japanese.value } },
    });
    failed.value = !data;
    if (data) page.value = data;
  } catch {
    failed.value = true; // the server is down or restarting
  }
}
watch([scope, japanese], load, { immediate: true });

const newName = ref("");
const newLevel = ref(2);
async function addSkill() {
  const { response } = await api.POST("/skills/level", { body: { name: newName.value, level: newLevel.value } });
  if (!response.ok) return;
  newName.value = "";
  await load(); // the new skill shows in whichever table it belongs to
}

// The jobs asking for a skill are still a server-rendered page.
const jobsLink = (key: string) => `/skills/${encodeURIComponent(key)}${japanese.value ? "?japanese=1" : ""}`;
const percent = (share: number) => `${(share * 100).toFixed(1)}%`;
</script>

<template>
  <header class="view-head">
    <h1>
      Skills
      <span v-if="page" class="count">{{ page.skills.length }} across {{ page.job_count }} jobs</span>
    </h1>
    <nav class="tables" aria-label="Which jobs">
      <QueryLink :query="query({ scope: 'kept' })" :current="scope === 'kept'">Kept, open jobs</QueryLink>
      <QueryLink :query="query({ scope: 'all' })" :current="scope === 'all'">Every job the model read</QueryLink>
    </nav>
    <nav class="tables" aria-label="Japanese jobs">
      <QueryLink :query="query({ japanese: false })" :current="!japanese">Without Japanese jobs</QueryLink>
      <QueryLink :query="query({ japanese: true })" :current="japanese">With Japanese jobs</QueryLink>
    </nav>
  </header>

  <p v-if="failed" class="empty">The server did not answer. Is jobhunt serve running?</p>

  <form class="search add-skill" @submit.prevent="addSkill">
    <input v-model="newName" placeholder="Add a skill to your profile" aria-label="Skill name" required>
    <select v-model.number="newLevel" aria-label="Level">
      <option v-for="value in page?.levels.filter(Boolean)" :key="value" :value="value">{{ value }}</option>
    </select>
    <button>add</button>
  </form>

  <template v-if="page">
    <p v-if="!page.skills.length" class="empty">No skills yet: they come from the model's reading of each job.</p>
    <template v-else>
      <div class="table-wrap">
        <table class="data skills-table">
          <thead>
            <tr>
              <th>Skill</th><th>Jobs</th><th>Share of jobs</th><th>Main language</th><th>Your level</th>
              <th>Avg score of its jobs</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="skill in page.skills" :key="skill.key">
              <td><a :href="jobsLink(skill.key)">{{ skill.name }}</a></td>
              <td>{{ skill.jobs }}</td>
              <td>
                <span class="bar" :style="{ '--share': percent(skill.share) }"></span>
                {{ Math.round(skill.share * 100) }}%
              </td>
              <td>{{ skill.main || "" }}</td>
              <td><LevelPicker :name="skill.key" :level="skill.level" :levels="page.levels" /></td>
              <td>{{ skill.avg_score == null ? "" : Math.round(skill.avg_score) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="meta">
        Spellings are merged ("JS" counts as JavaScript). "Main language" is how many of those jobs name it as
        the primary language. Levels: 3 strong, 2 working, 1 could pick it up, — not in your profile. A change
        saves to profile.toml and rescores the jobs.
      </p>
    </template>

    <section v-if="page.unseen.length">
      <h2>In your profile, in none of these jobs</h2>
      <table class="data skills-table">
        <tbody>
          <tr v-for="skill in page.unseen" :key="skill.name">
            <td>{{ skill.name }}</td>
            <td><LevelPicker :name="skill.name" :level="skill.level" :levels="page.levels" /></td>
          </tr>
        </tbody>
      </table>
    </section>
  </template>
</template>
