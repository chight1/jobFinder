import { createRouter, createWebHistory } from "vue-router";
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
    { path: "/", redirect: "/skills" },
    { path: "/skills", component: SkillsView, meta: { title: "Skills" } },
  ],
});

router.afterEach((to) => {
  document.title = `${to.meta.title} · jobhunt`;
});
