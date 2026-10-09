<script setup lang="ts">
import { watch } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";
import { refreshTabCounts, tabCounts } from "./tabCounts";

// The top bar. A tab with `to` is a page of this app; the others are still server-rendered pages,
// reached with a plain link until they move here.
const tabs: { name: string; to?: string; href?: string }[] = [
  { name: "New", to: "/new" },
  { name: "All open", to: "/all" },
  { name: "Japanese", to: "/japanese" },
  { name: "Removed", to: "/removed" },
  { name: "Applied", to: "/applied" },
  { name: "Not interested", to: "/not-interested" },
  { name: "Skills", to: "/skills" },
  { name: "Health", to: "/health" },
  { name: "Activity", href: "/activity" },
  { name: "Data", href: "/data" },
];

const route = useRoute();
watch(() => route.fullPath, refreshTabCounts, { immediate: true });
</script>

<template>
  <nav>
    <template v-for="tab in tabs" :key="tab.name">
      <RouterLink v-if="tab.to" :to="tab.to">
        {{ tab.name }} <span v-if="tab.name in tabCounts" class="tab-count">{{ tabCounts[tab.name] }}</span>
      </RouterLink>
      <a v-else :href="tab.href">
        {{ tab.name }} <span v-if="tab.name in tabCounts" class="tab-count">{{ tabCounts[tab.name] }}</span>
      </a>
    </template>
  </nav>
  <main>
    <!-- Keyed by path, so moving between lists starts each page fresh; a query change (a filter) does not. -->
    <RouterView :key="route.path" />
  </main>
</template>
