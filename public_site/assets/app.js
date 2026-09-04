"use strict";

const PAGE_SIZE = 48;
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
  for (const family of state.data.families) {
    state.familyById.set(family.id, family);
    addOption(elements.family, family.id, `${family.name_zh} / ${family.name_en}`);
  }
  for (const category of state.data.categories) {
    state.categoryById.set(category.id, category);
    addOption(elements.category, category.id, `${category.name}｜${category.name_zh}`);
  }
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  elements.search.value = params.get("q") || "";
  const family = params.get("family") || "all";
  const kind = params.get("kind") || "all";
  const category = params.get("category") || "all";
  if ([...elements.family.options].some((option) => option.value === family)) elements.family.value = family;
  if ([...elements.kind.options].some((option) => option.value === kind)) elements.kind.value = kind;
  if ([...elements.category.options].some((option) => option.value === category)) elements.category.value = category;
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
  const button = makeElement("button", "category-chip", `${category.name}｜${category.name_zh}`);
  button.type = "button";
  button.title = "只看此分类 / Filter by this category";
  button.addEventListener("click", () => {
    elements.family.value = "all";
    elements.kind.value = "all";
    elements.category.value = String(category.id);
    state.visible = PAGE_SIZE;
    update();
    document.querySelector("#catalog-title").scrollIntoView({ behavior: "smooth" });
  });
  return button;
}

function resultCard(match, index) {
  const item = match.prepared.item;
  const article = makeElement("article", "result");
  article.style.animationDelay = `${Math.min(index, 10) * 24}ms`;
  article.append(makeElement("span", "result-number", String(index + 1).padStart(3, "0")));

  const title = makeElement("div", "result-title");
  title.append(makeElement("h3", "", item.title_en));
  title.append(makeElement("p", "result-title-zh", item.title_zh));
  article.append(title);

  const meta = makeElement("div", "result-meta");
  const composer = makeElement("p", "result-composer", item.composer_en);
  composer.append(makeElement("span", "", item.composer_zh));
  meta.append(composer);
  const categories = makeElement("div", "category-list");
  match.categories.slice(0, 4).forEach((category) => categories.append(categoryChip(category)));
  if (match.categories.length > 4) {
    categories.append(makeElement("span", "category-more", `+${match.categories.length - 4}`));
  }
  meta.append(categories);
  article.append(meta);

  const link = makeElement("a", "source-link", "IMSLP 原页 / Source ↗");
  link.href = item.imslp_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  article.append(link);
  return article;
}

function renderResults() {
  elements.results.replaceChildren();
  if (!state.matches.length) {
    const empty = makeElement("div", "empty");
    empty.append(makeElement("strong", "", "没有找到匹配作品 / No match found"));
    empty.append(makeElement("span", "", "试试更短的曲名、作者姓氏或其他编制名称。"));
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
  state.matches = findMatches();
  const shown = Math.min(state.visible, state.matches.length);
  elements.status.textContent = `找到 ${compactNumber(state.matches.length)} 部作品，显示 ${compactNumber(shown)} 部 / ${compactNumber(state.matches.length)} works, ${compactNumber(shown)} shown`;
  renderResults();
}

function clearSearch() {
  elements.search.value = "";
  elements.family.value = "all";
  elements.kind.value = "all";
  elements.category.value = "all";
  state.visible = PAGE_SIZE;
  update();
  elements.search.focus();
}

async function copySearchLink() {
  writeUrlState();
  try {
    await navigator.clipboard.writeText(window.location.href);
    elements.share.textContent = "已复制 / Copied";
  } catch (_error) {
    elements.share.textContent = "请复制地址栏 / Copy address";
  }
  window.setTimeout(() => { elements.share.textContent = "复制检索链接 / Copy link"; }, 1800);
}

function bindEvents() {
  let timer;
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
    if (event.key === "/" && document.activeElement !== elements.search) {
      event.preventDefault();
      elements.search.focus();
    }
    if (event.key === "Escape" && document.activeElement === elements.search) clearSearch();
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
    document.querySelector("#stat-memberships").textContent = compactNumber(state.data.summary.category_record_count);
    readUrlState();
    bindEvents();
    update();
  } catch (error) {
    elements.status.textContent = "目录装载失败 / Catalogue unavailable";
    elements.error.hidden = false;
    elements.error.textContent = `无法装载检索数据：${error.message}`;
  }
}

start();
