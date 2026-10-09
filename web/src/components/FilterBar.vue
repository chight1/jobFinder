<script setup lang="ts">
import { useRoute, useRouter } from "vue-router";
import { optionsOn, toggled, type Filter } from "../filters";

// A list's filters, each as a strip of on/off buttons or a row of dots. A click flips one option in
// the query string; a filled-in dot or pressed button is on.
defineProps<{ filters: Filter[] }>();
const route = useRoute();
const router = useRouter();

const flip = (filter: Filter, option: string) => router.replace({ query: toggled(filter, option, route.query) });
</script>

<template>
  <div class="filters">
    <div
      v-for="filter in filters"
      :key="filter.key"
      :class="filter.dots ? 'rate' : 'segmented'"
      role="group"
      :aria-label="`${filter.label} filter`"
    >
      <template v-for="option in filter.options" :key="option.key">
        <button
          v-if="filter.dots"
          type="button"
          class="dot"
          :class="option.key"
          :aria-pressed="optionsOn(filter, route.query).has(option.key)"
          :aria-label="`Show ${option.label.toLowerCase()}`"
          :title="`Show ${option.label.toLowerCase()}`"
          @click="flip(filter, option.key)"
        ></button>
        <button
          v-else
          type="button"
          :aria-pressed="optionsOn(filter, route.query).has(option.key)"
          @click="flip(filter, option.key)"
        >{{ option.label }}</button>
      </template>
    </div>
  </div>
</template>
