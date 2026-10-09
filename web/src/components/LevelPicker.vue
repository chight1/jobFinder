<script setup lang="ts">
import { ref, watch } from "vue";
import { api } from "../api/client";

// Your level in one skill, saved to profile.toml as soon as you pick it. Saving also rescores every
// job, so the picker is disabled until the server answers, and goes back to the saved level on failure.
const props = defineProps<{ name: string; level: number; levels: number[] }>();
const saved = ref(props.level);
const choice = ref(props.level);
const saving = ref(false);

watch(
  () => props.level,
  (level) => {
    saved.value = level;
    choice.value = level;
  },
);

async function save() {
  saving.value = true;
  try {
    const { response } = await api.POST("/skills/level", { body: { name: props.name, level: choice.value } });
    if (response.ok) saved.value = choice.value;
  } catch {
    // The server is down or restarting; picking again retries.
  } finally {
    choice.value = saved.value;
    saving.value = false;
  }
}
</script>

<template>
  <span class="level">
    <select
      v-model.number="choice"
      :data-level="saved"
      :disabled="saving"
      :aria-label="`Your level in ${name}`"
      @change="save"
    >
      <option v-for="value in levels" :key="value" :value="value">{{ value || "—" }}</option>
    </select>
  </span>
</template>
