<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import type { FilterState } from "../filters";

// One filter as a button that says what is on ("Status: No status", "Any", "3 of 7"), opening a
// checklist of its options. Ticking one applies at once; a click outside or Esc closes it.
const props = defineProps<{ filter: FilterState }>();
const emit = defineEmits<{ flip: [option: string] }>();

const open = ref(false);
const root = ref<HTMLElement | null>(null);
const button = ref<HTMLButtonElement | null>(null);

const summary = computed(() => {
  const on = props.filter.options.filter((o) => o.on);
  if (on.length === props.filter.options.length) return "Any";
  if (on.length === 0) return "None";
  return on.length === 1 ? on[0]!.label : `${on.length} of ${props.filter.options.length}`;
});

function closeOnOutside(event: PointerEvent) {
  if (open.value && !root.value?.contains(event.target as Node)) open.value = false;
}
function closeOnEscape() {
  open.value = false;
  button.value?.focus();
}
onMounted(() => document.addEventListener("pointerdown", closeOnOutside));
onUnmounted(() => document.removeEventListener("pointerdown", closeOnOutside));
</script>

<template>
  <div ref="root" class="filter" @keydown.esc="closeOnEscape">
    <button ref="button" type="button" :aria-expanded="open" @click="open = !open">
      {{ filter.label }}: {{ summary }} <span aria-hidden="true">▾</span>
    </button>
    <div v-if="open" class="filter-menu" role="group" :aria-label="`${filter.label} filter`">
      <label v-for="option in filter.options" :key="option.key">
        <input type="checkbox" :checked="option.on" @change="emit('flip', option.key)">
        {{ option.label }}
      </label>
    </div>
  </div>
</template>
