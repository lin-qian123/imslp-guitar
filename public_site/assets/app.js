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
  options: document.querySelector("#search-options"),
  hint: document.querySelector("#search-hint"),
  directory: document.querySelector("#category-directory"),
  back: document.querySelector("#back-to-categories"),
  title: document.querySelector("#catalog-title"),
  description: document.querySelector("#catalog-description"),
};

const state = {
  data: null,
  categoryById: new Map(),
  engine: null,
  matches: [],
  visible: PAGE_SIZE,
  suggestions: [],
  activeSuggestion: -1,
  composing: false,
  aliasesAvailable: true,
  inputTimer: null,
  openFamilies: new Set(),
};

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

function populateFilters() {
  addFamilyShortcut("all", "全部分类");
  for (const family of state.data.families) {
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

function writeUrlState(push = false) {
  const params = new URLSearchParams();
  const query = elements.search.value.trim();
  if (query) params.set("q", query);
  if (elements.family.value !== "all") params.set("family", elements.family.value);
  if (elements.kind.value !== "all") params.set("kind", elements.kind.value);
  if (elements.category.value !== "all") params.set("category", elements.category.value);
  const suffix = params.toString();
  const url = `${window.location.pathname}${suffix ? `?${suffix}` : ""}${window.location.hash}`;
  if (push && url !== `${window.location.pathname}${window.location.search}${window.location.hash}`) history.pushState(null, "", url);
  else history.replaceState(null, "", url);
}

function currentFilters() {
  return {family: elements.family.value, kind: elements.kind.value, category: elements.category.value};
}

function closeSuggestions() {
  elements.options.hidden = true;
  elements.search.setAttribute("aria-expanded", "false");
  elements.search.removeAttribute("aria-activedescendant");
  state.activeSuggestion = -1;
}

function chooseSuggestion(index) {
  const suggestion = state.suggestions[index];
  if (!suggestion) return;
  elements.search.value = suggestion.query;
  state.visible = PAGE_SIZE;
  update();
  elements.search.focus();
  closeSuggestions();
  elements.status.scrollIntoView({behavior: scrollBehavior(), block: "start"});
}

function showSuggestions() {
  if (!state.engine || state.composing || document.activeElement !== elements.search) return;
  state.suggestions = state.engine.suggest(elements.search.value, currentFilters());
  elements.options.replaceChildren();
  closeSuggestions();
  if (!state.suggestions.length) return;
  state.suggestions.forEach((suggestion, index) => {
    const option = makeElement("li", "search-option");
    option.id = `search-option-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    const text = makeElement("span", "option-copy");
    text.append(makeElement("strong", "", suggestion.label), makeElement("small", "", suggestion.detail));
    option.append(text, makeElement("span", "option-kind", suggestion.kind === "composer" ? "作曲家" : "曲目"));
    // Keep focus on the combobox so both pointer and keyboard selection work.
    option.addEventListener("pointerdown", event => event.preventDefault());
    option.addEventListener("click", () => chooseSuggestion(index));
    elements.options.append(option);
  });
  elements.options.hidden = false;
  elements.search.setAttribute("aria-expanded", "true");
}

function openCategory(category, clearQuery = false) {
  if (clearQuery) elements.search.value = "";
  elements.family.value = "all";
  elements.kind.value = "all";
  elements.category.value = String(category.id);
  document.querySelector("#filter-drawer").open = true;
  state.visible = PAGE_SIZE;
  update({push: true});
  elements.title.scrollIntoView({behavior: scrollBehavior()});
}

function renderDirectory() {
  const directory = state.engine.browse(currentFilters());
  elements.directory.replaceChildren();
  elements.status.textContent = `${compactNumber(directory.categories.length)} 个分类 · ${compactNumber(directory.workCount)} 部作品`;
  for (const family of state.data.families) {
    const categories = directory.categories.filter(category => category.family === family.id);
    if (!categories.length) continue;
    const group = makeElement("details", "category-group");
    group.dataset.family = family.id;
    group.open = state.openFamilies.has(family.id) || elements.family.value === family.id;
    group.addEventListener("toggle", () => {
      if (group.open) state.openFamilies.add(family.id);
      else state.openFamilies.delete(family.id);
    });
    const summary = makeElement("summary", "");
    const label = makeElement("span", "group-label", family.name_zh);
    label.append(makeElement("small", "", family.name_en));
    summary.append(label, makeElement("span", "group-count", `${categories.length} 个分类`));
    group.append(summary);
    const grid = makeElement("div", "category-grid");
    for (const category of categories) {
      const card = makeElement("a", "category-card");
      card.href = `?category=${category.id}#catalog`;
      card.dataset.category = String(category.id);
      card.setAttribute("aria-label", `${category.name}｜${category.name_zh}，${compactNumber(category.work_count)} 部作品`);
      const meta = makeElement("div", "category-card-meta");
      meta.append(makeElement("span", "result-kind", category.kind === "original" ? "原作" : "改编"),
        makeElement("span", "", `${compactNumber(category.work_count)} 部作品`));
      const title = category.name_zh.replace(/[·.]?(?:原作|改编)$/, "");
      card.append(meta, makeElement("h3", "", title), makeElement("p", "category-source-name", category.name),
        makeElement("span", "category-enter", "查看作品 →"));
      card.addEventListener("click", event => {
        if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        openCategory(category, true);
      });
      grid.append(card);
    }
    group.append(grid);
    elements.directory.append(group);
  }
  if (!directory.categories.length) elements.directory.append(makeElement("p", "empty", "当前筛选条件下没有分类。请调整筛选条件。"));
}

function categoryChip(category) {
  const button = makeElement("button", "category-chip", category.name_zh);
  button.type = "button";
  button.title = category.name;
  button.setAttribute("aria-label", `查看 ${category.name_zh}（${category.name}）`);
  button.addEventListener("click", () => openCategory(category));
  return button;
}

function resultCard(match, index) {
  const item = match.item;
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
    extra.append(makeElement("summary", "", `其他 ${match.categories.length - 4} 个编制分类`));
    const list = makeElement("div", "category-list");
    match.categories.slice(4).forEach((category) => list.append(categoryChip(category)));
    extra.append(list);
    meta.append(extra);
  }
  article.append(meta);

  const link = makeElement("a", "source-link", "IMSLP 作品原页");
  link.href = item.imslp_url;
  link.setAttribute("aria-label", `${item.title_zh}：IMSLP 作品原页（新窗口）`);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  article.append(link);
  return article;
}

function renderResults() {
  elements.results.replaceChildren();
  if (!state.matches.length) {
    const empty = makeElement("div", "empty");
    empty.append(makeElement("strong", "", "未找到匹配作品"));
    empty.append(makeElement("span", "", "请检查关键词，或调整编制筛选条件。"));
    if (Object.values(currentFilters()).some(value => value !== "all")) {
      const relax = makeElement("button", "relax-filters", "保留关键词，放宽编制");
      relax.type = "button";
      relax.addEventListener("click", () => {
        elements.family.value = elements.kind.value = elements.category.value = "all";
        state.visible = PAGE_SIZE;
        update();
      });
      empty.append(relax);
    }
    elements.results.append(empty);
    elements.loadMore.hidden = true;
    return;
  }
  const fragment = document.createDocumentFragment();
  state.matches.slice(0, state.visible).forEach((match, index) => fragment.append(resultCard(match, index)));
  elements.results.append(fragment);
  elements.loadMore.hidden = state.visible >= state.matches.length;
}

function update({push = false} = {}) {
  window.clearTimeout(state.inputTimer);
  closeSuggestions();
  writeUrlState(push);
  updateFamilyShortcuts();
  const browsing = GuitarSearch.catalogView(elements.search.value, currentFilters()) === "categories";
  elements.directory.hidden = !browsing;
  elements.results.hidden = browsing;
  elements.back.hidden = browsing;
  elements.title.textContent = browsing ? "乐谱分类库" : elements.category.value === "all" ? "作品检索" : state.categoryById.get(Number(elements.category.value)).name_zh;
  elements.description.textContent = browsing ? "按演奏编制浏览，原作与改编分别列出。"
    : elements.category.value === "all" ? "按相关性排列，支持中英文与常见异译名。" : state.categoryById.get(Number(elements.category.value)).name;
  if (browsing) {
    state.matches = [];
    elements.results.replaceChildren();
    elements.loadMore.hidden = true;
    elements.hint.hidden = true;
    renderDirectory();
    elements.directory.setAttribute("aria-busy", "false");
    elements.results.setAttribute("aria-busy", "false");
    return;
  }
  const response = state.engine.search(elements.search.value, currentFilters());
  state.matches = response.matches;
  const shown = Math.min(state.visible, state.matches.length);
  elements.status.textContent = `${compactNumber(state.matches.length)} 部${response.mode === "fuzzy" ? "近似" : ""}作品${shown < state.matches.length ? ` · 已显示 ${shown} 部` : ""}`;
  elements.hint.hidden = response.mode !== "fuzzy" && state.aliasesAvailable;
  elements.hint.textContent = response.mode === "fuzzy" ? "未找到精确结果，以下为近似匹配。"
    : state.aliasesAvailable ? "" : "别名表暂未载入，曲名搜索和拼写容错仍然可用。";
  renderResults();
  elements.directory.setAttribute("aria-busy", "false");
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
  window.setTimeout(() => { elements.share.textContent = "复制链接 ↗"; }, 1800);
}

function scrollBehavior() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
}

function bindEvents() {
  let timer;
  document.querySelector("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.composing) return;
    window.clearTimeout(timer);
    state.visible = PAGE_SIZE;
    update();
    elements.status.scrollIntoView({ behavior: scrollBehavior(), block: "start" });
  });
  elements.back.addEventListener("click", () => {
    elements.search.value = "";
    elements.category.value = "all";
    document.querySelector("#filter-drawer").open = false;
    state.visible = PAGE_SIZE;
    update({push:true});
    elements.title.scrollIntoView({behavior:scrollBehavior()});
  });
  function scheduleSearch() {
    window.clearTimeout(timer);
    closeSuggestions();
    if (state.composing) return;
    timer = state.inputTimer = window.setTimeout(() => { state.visible = PAGE_SIZE; update(); showSuggestions(); }, 130);
  }
  elements.search.addEventListener("input", scheduleSearch);
  elements.search.addEventListener("compositionstart", () => {
    state.composing = true;
    window.clearTimeout(timer);
    closeSuggestions();
  });
  elements.search.addEventListener("compositionend", () => { state.composing = false; scheduleSearch(); });
  elements.search.addEventListener("focus", showSuggestions);
  elements.search.addEventListener("blur", closeSuggestions);
  elements.search.addEventListener("keydown", event => {
    if (event.isComposing || state.composing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (elements.options.hidden) showSuggestions();
      if (elements.options.hidden) return;
      event.preventDefault();
      window.clearTimeout(timer);
      const count = state.suggestions.length;
      state.activeSuggestion = state.activeSuggestion < 0
        ? (event.key === "ArrowDown" ? 0 : count - 1)
        : (state.activeSuggestion + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
      [...elements.options.children].forEach((option, index) => option.setAttribute("aria-selected", String(index === state.activeSuggestion)));
      const active = elements.options.children[state.activeSuggestion];
      elements.search.setAttribute("aria-activedescendant", active.id);
      active.scrollIntoView({block: "nearest"});
    } else if (event.key === "Enter" && !elements.options.hidden && state.activeSuggestion >= 0) {
      event.preventDefault();
      window.clearTimeout(timer);
      chooseSuggestion(state.activeSuggestion);
    } else if (event.key === "Escape" && !elements.options.hidden) {
      event.preventDefault();
      event.stopPropagation();
      window.clearTimeout(timer);
      closeSuggestions();
    }
  });
  [elements.family, elements.kind, elements.category].forEach((select) => {
    select.addEventListener("change", () => { state.visible = PAGE_SIZE; update(); });
  });
  elements.clear.addEventListener("click", () => { window.clearTimeout(timer); clearSearch(); });
  elements.share.addEventListener("click", copySearchLink);
  elements.loadMore.addEventListener("click", () => {
    state.visible += PAGE_SIZE;
    update();
  });
  document.addEventListener("keydown", (event) => {
    if (event.isComposing || state.composing) return;
    const editing = event.target.closest("input, textarea, select, [contenteditable]");
    if (event.key === "/" && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      elements.search.focus();
    }
    if (event.key === "Escape" && document.activeElement === elements.search) {
      window.clearTimeout(timer);
      clearSearch();
    }
  });
  window.addEventListener("popstate", () => {
    readUrlState();
    state.visible = PAGE_SIZE;
    update();
  });
}

async function start() {
  try {
    const [response, aliases] = await Promise.all([
      fetch("data/catalog.json", { cache: "no-cache" }),
      fetch("data/search-aliases.json", { cache: "no-cache", signal: AbortSignal.timeout(5000) })
        .then(result => result.ok ? result.json() : null).catch(() => null),
    ]);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    if (state.data.schema_version !== 1) throw new Error("unsupported catalog schema");
    populateFilters();
    state.aliasesAvailable = aliases !== null;
    state.engine = GuitarSearch.createIndex(state.data, aliases || {});
    document.querySelector("#stat-works").textContent = compactNumber(state.data.summary.unique_work_count);
    document.querySelector("#stat-categories").textContent = compactNumber(state.data.summary.category_count);
    readUrlState();
    bindEvents();
    update();
  } catch (error) {
    elements.status.textContent = "目录载入失败";
    elements.error.hidden = false;
    elements.error.textContent = "无法载入目录数据，请稍后刷新页面。";
    elements.directory.setAttribute("aria-busy", "false");
    elements.results.setAttribute("aria-busy", "false");
    console.error("Catalogue loading failed", error);
  }
}

start();
