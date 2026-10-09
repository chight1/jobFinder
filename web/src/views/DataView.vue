<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { api, type TablePage } from "../api/client";
import QueryLink from "../components/QueryLink.vue";

// Every table, read-only: newest rows first, 50 a page, with a search across all columns. The table,
// search and page live in the query string (?table=sightings&q=Acme&page=2).
const route = useRoute();
const router = useRouter();
const text = (value: unknown) => (typeof value === "string" ? value : "");
const table = computed(() => text(route.query.table) || "jobs");
const q = computed(() => text(route.query.q));
const page = computed(() => Number(route.query.page) || 1);

const result = ref<TablePage | null>(null);
const problem = ref("");

async function load() {
  try {
    const { data, response } = await api.GET("/data/{table}", {
      params: { path: { table: table.value }, query: { q: q.value, page: page.value } },
    });
    problem.value = data ? "" : response.status === 404 ? `There is no table named ${table.value}.` : "The server could not load it.";
    if (data) result.value = data;
  } catch {
    problem.value = "The server did not answer. Is jobhunt serve running?";
  }
}
watch([table, q, page], load, { immediate: true });

const search = ref(q.value);
watch(q, (value) => (search.value = value));
const submit = () => router.push({ query: { table: table.value, q: search.value || undefined } });
const pageQuery = (n: number) => ({ table: table.value, q: q.value || undefined, page: String(n) });

// Columns holding a job id link to that job's page.
const isJob = (column: string) => (table.value === "jobs" && column === "id") || ["job_id", "job_a", "job_b"].includes(column);
</script>

<template>
  <header class="view-head">
    <h1>Data <span v-if="result" class="count">{{ result.table }}: {{ result.total }}</span></h1>
    <form class="search" @submit.prevent="submit">
      <input v-model="search" type="search" placeholder="search every column" aria-label="Search">
      <button>Search</button>
    </form>
  </header>

  <nav v-if="result" class="tables" aria-label="Tables">
    <QueryLink v-for="(n, name) in result.tables" :key="name" :query="{ table: name }" :current="name === table">
      {{ name }} <span class="tab-count">{{ n }}</span>
    </QueryLink>
  </nav>

  <p v-if="problem" class="empty">{{ problem }}</p>
  <template v-else-if="result">
    <p v-if="!result.rows.length" class="empty">
      <template v-if="q">No rows match “{{ q }}”.</template>
      <template v-else>This table is empty.</template>
    </p>
    <template v-else>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr><th v-for="column in result.columns" :key="column">{{ column }}</th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in result.rows" :key="i">
              <td v-for="(value, c) in row" :key="c">
                <RouterLink v-if="value && isJob(result.columns[c] ?? '')" :to="`/jobs/${value}`">{{ value }}</RouterLink>
                <template v-else>{{ value }}</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="result.pages > 1" class="pager">
        <RouterLink v-if="page > 1" :to="{ query: pageQuery(page - 1) }">← newer</RouterLink>
        page {{ page }} of {{ result.pages }}
        <RouterLink v-if="page < result.pages" :to="{ query: pageQuery(page + 1) }">older →</RouterLink>
      </p>
    </template>
  </template>
</template>
