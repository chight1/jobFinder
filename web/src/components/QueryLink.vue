<script setup lang="ts">
import { RouterLink, type LocationQueryRaw } from "vue-router";

// A link to the same page with different filters in the query string. RouterLink alone would mark
// every query of the page as current, so the caller says which one is.
defineProps<{ query: LocationQueryRaw; current: boolean }>();
</script>

<template>
  <RouterLink v-slot="{ href, navigate }" :to="{ query }" custom>
    <a :href="href" :aria-current="current ? 'page' : undefined" @click="navigate"><slot /></a>
  </RouterLink>
</template>
