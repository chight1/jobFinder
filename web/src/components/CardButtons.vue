<script setup lang="ts">
import { ref } from "vue";
import type { JobCard } from "../api/client";
import { CARD_BUTTONS, type CardButton, type Undo } from "../cardButtons";

// A card's buttons as one segmented control: the ones whose rule says they show for this job.
// A press tells the page, with the undo for it.
const props = defineProps<{ job: JobCard }>();
const emit = defineEmits<{ changed: [undo?: Undo] }>();
const busy = ref(false);

async function press(button: CardButton) {
  busy.value = true;
  try {
    emit("changed", await button.click(props.job));
  } catch {
    // The server is down or restarting; clicking again retries.
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="segmented" role="group" aria-label="Actions">
    <button
      v-for="button in CARD_BUTTONS.filter((b) => b.shows(job))"
      :key="button.label"
      type="button"
      :aria-pressed="button.pressed?.(job)"
      :title="button.hint"
      :disabled="busy"
      @click="press(button)"
    >{{ button.label }}</button>
  </div>
</template>
