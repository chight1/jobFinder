<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { announce } from "../announce";
import type { Undo } from "../cardButtons";

// The last change made from a card, with a button to take it back. It goes after 10 seconds, or when
// the next change replaces it. The clock waits while the pointer or focus is on it, so it never
// vanishes under a hand reaching for it.
const props = defineProps<{ undo: Undo | null }>();
const emit = defineEmits<{ done: [undone: boolean] }>();
const SHOW_MS = 10_000;
const busy = ref(false);

let timer: number | undefined;
const pause = () => window.clearTimeout(timer);
function resume() {
  pause();
  timer = window.setTimeout(() => emit("done", false), SHOW_MS);
}
watch(() => props.undo, (undo) => (undo ? resume() : pause()), { immediate: true });
onUnmounted(pause);

async function revert(undo: Undo) {
  busy.value = true;
  try {
    if (await undo.revert()) {
      announce(`Undone: ${undo.message}`);
      emit("done", true);
    }
  } catch {
    // The server is down or restarting; clicking again retries.
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div v-if="undo" class="undo-bar" @mouseenter="pause" @mouseleave="resume" @focusin="pause" @focusout="resume">
    <span>{{ undo.message }}</span>
    <button type="button" :disabled="busy" @click="revert(undo)">Undo</button>
    <button type="button" class="dismiss" aria-label="Dismiss" title="Dismiss" @click="emit('done', false)">×</button>
  </div>
</template>
