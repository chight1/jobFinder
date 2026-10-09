import { createRouter, createWebHistory } from "vue-router";
import JobsView from "./views/JobsView.vue";
import RemovedView from "./views/RemovedView.vue";
import SkillsView from "./views/SkillsView.vue";

declare module "vue-router" {
  interface RouteMeta {
    title: string;
  }
}

// Pages move here from the server-rendered site one at a time; App.vue links to the rest.
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
    { path: "/skills", component: SkillsView, meta: { title: "Skills" } },
    {
      path: "/skills/:key",
      component: JobsView,
      props: (route) => ({ view: "skill", skill: route.params.key, japanese: route.query.japanese === "1" }),
      meta: { title: "Jobs for a skill" },
    },
  ],
});

router.afterEach((to) => {
  document.title = `${to.meta.title} · jobhunt`;
});
