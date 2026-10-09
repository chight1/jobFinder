<script setup lang="ts">
import { ref, watch } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";
import { api } from "./api/client";

// The top bar. A tab with `to` is a page of this app; the others are still server-rendered pages,
// reached with a plain link until they move here.
const tabs: { name: string; to?: string; href?: string }[] = [
  { name: "New", href: "/new" },
  { name: "All open", href: "/all" },
  { name: "Japanese", href: "/japanese" },
  { name: "Removed", href: "/removed" },
  { name: "Applied", href: "/applied" },
  { name: "Not interested", href: "/not-interested" },
  { name: "Skills", to: "/skills" },
  { name: "Health", href: "/health" },
  { name: "Activity", href: "/activity" },
  { name: "Data", href: "/data" },
];

const counts = ref<Record<string, number>>({});
const route = useRoute();
watch(
  () => route.fullPath,
  async () => {
    const { data } = await api.GET("/tabs");
    if (data) counts.value = data;
  },
  { immediate: true },
);
</script>

<template>
  <nav>
    <template v-for="tab in tabs" :key="tab.name">
      <RouterLink v-if="tab.to" :to="tab.to">
        {{ tab.name }} <span v-if="tab.name in counts" class="tab-count">{{ counts[tab.name] }}</span>
      </RouterLink>
      <a v-else :href="tab.href">
        {{ tab.name }} <span v-if="tab.name in counts" class="tab-count">{{ counts[tab.name] }}</span>
      </a>
    </template>
  </nav>
  <main>
    <RouterView />
  </main>
</template>
