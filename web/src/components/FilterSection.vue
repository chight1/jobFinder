<script setup lang="ts">
import { computed } from "vue";
import type { FilterState } from "../filters";

// One filter in the rail: a heading that opens and closes its checklist and, closed, still says what
// is on ("Any", "No status", "3 of 7"). Ticking an option applies at once. This browser remembers
// which sections are open; storage can be blocked, and then they all start closed.
const props = defineProps<{ filter: FilterState }>();
const emit = defineEmits<{ flip: [option: string] }>();
const OPEN_KEY = "jobs.openFilters";

const summary = computed(() => {
  const on = props.filter.options.filter((o) => o.on);
  if (on.length === props.filter.options.length) return "Any";
  if (on.length === 0) return "None";
  return on.length === 1 ? on[0]!.label : `${on.length} of ${props.filter.options.length}`;
});

function openOnes(): string[] {
  try {
    return JSON.parse(localStorage.getItem(OPEN_KEY) ?? "[]");
  } catch {
    return [];
  }
}
const startsOpen = openOnes().includes(props.filter.key);

function remember(event: Event) {
  const open = (event.target as HTMLDetailsElement).open;
  const others = openOnes().filter((key) => key !== props.filter.key);
  try {
    localStorage.setItem(OPEN_KEY, JSON.stringify(open ? [...others, props.filter.key] : others));
  } catch {}
}
</script>

<template>
  <details class="filter-section" :open="startsOpen" @toggle="remember">
    <summary>
      <span class="filter-name">{{ filter.label }}</span>
      <span class="filter-summary">{{ summary }}</span>
    </summary>
    <div class="filter-options" role="group" :aria-label="`${filter.label} filter`">
      <label v-for="option in filter.options" :key="option.key">
        <input type="checkbox" :checked="option.on" @change="emit('flip', option.key)">
        {{ option.label }}
      </label>
    </div>
  </details>
</template>
