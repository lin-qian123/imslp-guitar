"use strict";

const PAGE_SIZE = 18;
const FAMILY_LABELS = {
  pure: "纯吉他", strings: "弦乐", woodwinds: "木管", brass: "铜管",
  keyboard_reed: "键盘 / 自由簧", plucked: "拨弦乐器", percussion: "打击乐", mixed_chamber: "室内乐",
};
const elements = {
  search: document.querySelector("#search"),
  family: document.querySelector("#family-filter"),
  kind: document.querySelector("#kind-filter"),
  category: document.querySelector("#category-filter"),
  clear: document.querySelector("#clear"),
  share: document.querySelector("#share"),
  status: document.querySelector("#status"),
  results: document.querySelector("#results"),
  loadMore: document.querySelector("#load-more"),
  error: document.querySelector("#error"),
  familyShortcuts: document.querySelector("#family-shortcuts"),
};

const state = {
  data: null,
  categoryById: new Map(),
  familyById: new Map(),
  preparedWorks: [],
  matches: [],
  visible: PAGE_SIZE,
};

function normalize(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase()
    .replace(/[’'“”‘’《》〈〉()[\]{},，.·:;!?/\\|–—_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function makeElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function addOption(select, value, label) {
  const option = document.createElement("option");
  option.value = String(value);
  option.textContent = label;
  select.append(option);
}

function addFamilyShortcut(value, label) {
  const button = makeElement("button", "family-shortcut", label);
  button.type = "button";
  button.dataset.family = value;
  button.setAttribute("aria-pressed", "false");
  button.addEventListener("click", () => {
    elements.family.value = value;
    elements.category.value = "all";
    state.visible = PAGE_SIZE;
    update();
  });
  elements.familyShortcuts.append(button);
}

function compactNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function selectedCategoryIds(work) {
  return work.category_ids.map((id) => state.categoryById.get(id));
}

function prepareWork(item) {
  const categories = selectedCategoryIds(item);
  const searchable = [
    item.id,
    item.title_en,
    item.title_zh,
    item.composer_en,
    item.composer_zh,
    ...categories.flatMap((category) => [category.name, category.name_zh]),
  ];
  return { item, categories, fields: searchable.map(normalize), blob: normalize(searchable.join(" ")) };
}

function populateFilters() {
  addFamilyShortcut("all", "都看看");
  for (const family of state.data.families) {
    state.familyById.set(family.id, family);
    addOption(elements.family, family.id, `${family.name_zh} / ${family.name_en}`);
    addFamilyShortcut(family.id, FAMILY_LABELS[family.id] || family.name_zh);
  }
  for (const category of state.data.categories) {
    state.categoryById.set(category.id, category);
    addOption(elements.category, category.id, `${category.name}｜${category.name_zh}`);
  }
}

function updateFamilyShortcuts() {
  let selected = elements.family.value;
  if (elements.category.value !== "all") {
    selected = state.categoryById.get(Number(elements.category.value)).family;
  }
  for (const button of elements.familyShortcuts.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.family === selected));
  }
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  elements.search.value = params.get("q") || "";
  const family = params.get("family") || "all";
  const kind = params.get("kind") || "all";
  const category = params.get("category") || "all";
  for (const [select, value] of [[elements.family, family], [elements.kind, kind], [elements.category, category]]) {
    select.value = [...select.options].some((option) => option.value === value) ? value : "all";
  }
  document.querySelector("#filter-drawer").open = [family, kind, category].some((value) => value !== "all");
}

function writeUrlState() {
  const params = new URLSearchParams();
  const query = elements.search.value.trim();
  if (query) params.set("q", query);
  if (elements.family.value !== "all") params.set("family", elements.family.value);
  if (elements.kind.value !== "all") params.set("kind", elements.kind.value);
  if (elements.category.value !== "all") params.set("category", elements.category.value);
  const suffix = params.toString();
  history.replaceState(null, "", `${window.location.pathname}${suffix ? `?${suffix}` : ""}${window.location.hash}`);
}

function scoreMatch(prepared, query, terms) {
  if (terms.some((term) => !prepared.blob.includes(term))) return null;
  if (!query) return 4;
  const primary = prepared.fields.slice(1, 5);
  if (primary.some((field) => field === query)) return 0;
  if (primary.some((field) => field.startsWith(query))) return 1;
  if (primary.some((field) => terms.every((term) => field.includes(term)))) return 2;
  return 3;
}

function categoryPasses(category) {
  return (
    (elements.family.value === "all" || category.family === elements.family.value) &&
    (elements.kind.value === "all" || category.kind === elements.kind.value) &&
    (elements.category.value === "all" || String(category.id) === elements.category.value)
  );
}

function filteredCategories(prepared) {
  return prepared.categories.filter(categoryPasses);
}

function findMatches() {
  const query = normalize(elements.search.value);
  const terms = query.split(" ").filter(Boolean);
  const matches = [];
  for (const prepared of state.preparedWorks) {
    const categories = filteredCategories(prepared);
    if (!categories.length) continue;
    const score = scoreMatch(prepared, query, terms);
    if (score !== null) matches.push({ prepared, categories, score });
  }
  matches.sort((left, right) =>
    left.score - right.score ||
    left.prepared.item.composer_en.localeCompare(right.prepared.item.composer_en) ||
    left.prepared.item.title_en.localeCompare(right.prepared.item.title_en)
  );
  return matches;
}

function categoryChip(category) {
  const button = makeElement("button", "category-chip", category.name_zh);
  button.type = "button";
  button.title = category.name;
  button.setAttribute("aria-label", `查看 ${category.name_zh}（${category.name}）`);
  button.addEventListener("click", () => {
    elements.family.value = "all";
    elements.kind.value = "all";
    elements.category.value = String(category.id);
    document.querySelector("#filter-drawer").open = true;
    state.visible = PAGE_SIZE;
    update();
    document.querySelector("#catalog-title").scrollIntoView({ behavior: scrollBehavior() });
  });
  return button;
}

function resultCard(match, index) {
  const item = match.prepared.item;
  const article = makeElement("article", "result");
  article.style.animationDelay = `${Math.min(index, 10) * 24}ms`;

  const header = makeElement("div", "result-header");
  header.append(makeElement("span", "result-number", String(index + 1).padStart(2, "0")));
  const kinds = new Set(match.categories.map((category) => category.kind));
  header.append(makeElement("span", "result-kind", kinds.size > 1 ? "原作 / 改编" : kinds.has("original") ? "原作" : "改编"));
  article.append(header);

  const title = makeElement("div", "result-title");
  title.append(makeElement("h3", "", item.title_zh.replace(/^《|》$/g, "")));
  title.append(makeElement("p", "result-title-en", item.title_en));
  article.append(title);

  const meta = makeElement("div", "result-meta");
  const composer = makeElement("p", "result-composer", item.composer_zh);
  composer.append(makeElement("span", "", item.composer_en));
  meta.append(composer);
  const categories = makeElement("div", "category-list");
  match.categories.slice(0, 4).forEach((category) => categories.append(categoryChip(category)));
  meta.append(categories);
  if (match.categories.length > 4) {
    const extra = makeElement("details", "category-extra");
    extra.append(makeElement("summary", "", `还有 ${match.categories.length - 4} 种编制`));
    const list = makeElement("div", "category-list");
    match.categories.slice(4).forEach((category) => list.append(categoryChip(category)));
    extra.append(list);
    meta.append(extra);
  }
  article.append(meta);

  const link = makeElement("a", "source-link", "去 IMSLP 看谱");
  link.href = item.imslp_url;
  link.setAttribute("aria-label", `去 IMSLP 看 ${item.title_zh}（新窗口）`);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  article.append(link);
  return article;
}

function renderResults() {
  elements.results.replaceChildren();
  if (!state.matches.length) {
    const empty = makeElement("div", "empty");
    empty.append(makeElement("strong", "", "这一首，还没翻到。"));
    empty.append(makeElement("span", "", "换个曲名或作曲家姓氏试试，也可以放宽编制。"));
    elements.results.append(empty);
    elements.loadMore.hidden = true;
    return;
  }
  const fragment = document.createDocumentFragment();
  state.matches.slice(0, state.visible).forEach((match, index) => fragment.append(resultCard(match, index)));
  elements.results.append(fragment);
  elements.loadMore.hidden = state.visible >= state.matches.length;
}

function update() {
  writeUrlState();
  updateFamilyShortcuts();
  state.matches = findMatches();
  const shown = Math.min(state.visible, state.matches.length);
  elements.status.textContent = `${compactNumber(state.matches.length)} 部作品${shown < state.matches.length ? ` · 先看 ${shown} 部` : ""}`;
  renderResults();
  elements.results.setAttribute("aria-busy", "false");
}

function clearSearch() {
  elements.search.value = "";
  elements.family.value = "all";
  elements.kind.value = "all";
  elements.category.value = "all";
  document.querySelector("#filter-drawer").open = false;
  state.visible = PAGE_SIZE;
  update();
  elements.search.focus();
}

async function copySearchLink() {
  writeUrlState();
  try {
    await navigator.clipboard.writeText(window.location.href);
    elements.share.textContent = "链接已复制 ✓";
  } catch (_error) {
    elements.share.textContent = "复制地址栏即可分享";
  }
  window.setTimeout(() => { elements.share.textContent = "分享这页 ↗"; }, 1800);
}

function scrollBehavior() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
}

function bindEvents() {
  let timer;
  document.querySelector("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    window.clearTimeout(timer);
    state.visible = PAGE_SIZE;
    update();
    elements.status.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
  });
  document.querySelectorAll("[data-query]").forEach((button) => {
    button.addEventListener("click", () => {
      elements.search.value = button.dataset.query;
      elements.family.value = "all";
      elements.kind.value = "all";
      elements.category.value = "all";
      state.visible = PAGE_SIZE;
      update();
    });
  });
  elements.search.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => { state.visible = PAGE_SIZE; update(); }, 80);
  });
  [elements.family, elements.kind, elements.category].forEach((select) => {
    select.addEventListener("change", () => { state.visible = PAGE_SIZE; update(); });
  });
  elements.clear.addEventListener("click", clearSearch);
  elements.share.addEventListener("click", copySearchLink);
  elements.loadMore.addEventListener("click", () => {
    state.visible += PAGE_SIZE;
    update();
  });
  document.addEventListener("keydown", (event) => {
    const editing = event.target.closest("input, textarea, select, [contenteditable]");
    if (event.key === "/" && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      elements.search.focus();
    }
    if (event.key === "Escape" && document.activeElement === elements.search) clearSearch();
  });
  window.addEventListener("popstate", () => {
    readUrlState();
    state.visible = PAGE_SIZE;
    update();
  });
}

async function start() {
  try {
    const response = await fetch("data/catalog.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    if (state.data.schema_version !== 1) throw new Error("unsupported catalog schema");
    populateFilters();
    state.preparedWorks = state.data.works.map(prepareWork);
    document.querySelector("#stat-works").textContent = compactNumber(state.data.summary.unique_work_count);
    document.querySelector("#stat-categories").textContent = compactNumber(state.data.summary.category_count);
    readUrlState();
    bindEvents();
    update();
  } catch (error) {
    elements.status.textContent = "曲目暂时没载入，刷新试试。";
    elements.error.hidden = false;
    elements.error.textContent = "曲目暂时没载入。请稍后刷新页面。";
    elements.results.setAttribute("aria-busy", "false");
    console.error("Catalogue loading failed", error);
  }
}

start();
