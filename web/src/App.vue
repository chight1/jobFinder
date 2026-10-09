<script setup lang="ts">
import { nextTick, watch } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";
import { announcement } from "./announce";
import { refreshTabCounts, tabCounts } from "./tabCounts";

// The top bar; a tab with a count shows how many records it holds.
const tabs: { name: string; to: string }[] = [
  { name: "New", to: "/new" },
  { name: "All open", to: "/all" },
  { name: "Japanese", to: "/japanese" },
  { name: "Removed", to: "/removed" },
  { name: "Applied", to: "/applied" },
  { name: "Not interested", to: "/not-interested" },
  { name: "Skills", to: "/skills" },
  { name: "Health", to: "/health" },
  { name: "Activity", to: "/activity" },
  { name: "Data", to: "/data" },
];

const route = useRoute();
watch(() => route.fullPath, refreshTabCounts, { immediate: true });

// Moving to another page changes nothing a screen reader notices, so focus goes to the new page's
// heading, which it then reads. Not on the first load: the browser handles that.
watch(
  () => route.path,
  async () => {
    await nextTick();
    const target = document.querySelector<HTMLElement>("main h1") ?? document.querySelector<HTMLElement>("main");
    target?.setAttribute("tabindex", "-1");
    target?.focus({ preventScroll: true });
  },
);
</script>

<template>
  <nav class="topbar" aria-label="Pages">
    <RouterLink v-for="tab in tabs" :key="tab.name" :to="tab.to">
      {{ tab.name }} <span v-if="tab.name in tabCounts" class="tab-count">{{ tabCounts[tab.name] }}</span>
    </RouterLink>
  </nav>
  <main>
    <!-- Keyed by path, so moving between lists starts each page fresh; a query change (a filter) does not. -->
    <RouterView :key="route.path" />
  </main>
  <div class="sr-only" aria-live="polite">{{ announcement }}</div>
</template>
