import { createRouter, createWebHistory } from "vue-router";
import ActivityView from "./views/ActivityView.vue";
import DataView from "./views/DataView.vue";
import HealthView from "./views/HealthView.vue";
import JobsView from "./views/JobsView.vue";
import JobView from "./views/JobView.vue";
import PacketView from "./views/PacketView.vue";
import RemovedView from "./views/RemovedView.vue";
import SkillsView from "./views/SkillsView.vue";

declare module "vue-router" {
  interface RouteMeta {
    title: string;
  }
}

const id = (route: { params: Record<string, unknown> }) => ({ id: Number(route.params.id) });

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: "/", redirect: "/new" },
    { path: "/new", component: JobsView, props: { view: "new" }, meta: { title: "New" } },
    { path: "/all", component: JobsView, props: { view: "all" }, meta: { title: "All open" } },
    { path: "/japanese", component: JobsView, props: { view: "japanese" }, meta: { title: "Japanese" } },
    { path: "/removed", component: RemovedView, meta: { title: "Removed" } },
    { path: "/applied", component: JobsView, props: { view: "applied" }, meta: { title: "Applied" } },
    {
      path: "/not-interested",
      component: JobsView,
      props: { view: "not_interested" },
      meta: { title: "Not interested" },
    },
    { path: "/jobs/:id(\\d+)", component: JobView, props: id, meta: { title: "Job" } },
    { path: "/packets/:id(\\d+)", component: PacketView, props: id, meta: { title: "Packet" } },
    { path: "/skills", component: SkillsView, meta: { title: "Skills" } },
    {
      path: "/skills/:key",
      component: JobsView,
      props: (route) => ({ view: "skill", skill: route.params.key, japanese: route.query.japanese === "1" }),
      meta: { title: "Jobs for a skill" },
    },
    { path: "/health", component: HealthView, meta: { title: "Health" } },
    { path: "/activity", component: ActivityView, meta: { title: "Activity" } },
    { path: "/data", component: DataView, meta: { title: "Data" } },
  ],
});

router.afterEach((to) => {
  document.title = `${to.meta.title} · jobhunt`; // a page may put a more exact title once its data loads
});
