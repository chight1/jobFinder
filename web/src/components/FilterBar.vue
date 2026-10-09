<script setup lang="ts">
import { useRoute, useRouter } from "vue-router";
import { toggled, type FilterState } from "../filters";
import FilterMenu from "./FilterMenu.vue";

// The jobs table's filters on one line: a filter shown as dots is a row of dots like the rating dots
// on a card (each option's key is then its color, and a filled-in dot is on); every other filter is a
// dropdown checklist. A change flips one option in the query string.
defineProps<{ filters: FilterState[] }>();
const route = useRoute();
const router = useRouter();

const flip = (filter: FilterState, option: string) => router.replace({ query: toggled(filter, option, route.query) });
</script>

<template>
  <div class="filters">
    <template v-for="filter in filters" :key="filter.key">
      <div v-if="filter.look === 'dots'" class="rate" role="group" :aria-label="`${filter.label} filter`">
        <button
          v-for="option in filter.options"
          :key="option.key"
          type="button"
          class="dot"
          :class="option.key"
          :aria-pressed="option.on"
          :aria-label="`Show ${option.label.toLowerCase()}`"
          :title="`Show ${option.label.toLowerCase()}`"
          @click="flip(filter, option.key)"
        ></button>
      </div>
      <FilterMenu v-else :filter="filter" @flip="flip(filter, $event)" />
    </template>
  </div>
</template>
